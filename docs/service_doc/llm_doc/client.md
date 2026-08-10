# ClientManager 设计文档

> **模块**：`app/services/llm/client.py`
> **职责**：全局共享 `AsyncOpenAI` client 实例，支持多模型 key 隔离

---

## 设计目标

1. **连接池复用**：避免每次请求创建新 `AsyncOpenAI` 实例导致的 TCP 握手和 SSL 开销
2. **多 key 隔离**：`main` / `reasoning` / `fast` 各自独立配置，按需获取
3. **懒加载**：配置注册后不立即创建 client，第一次使用时才实例化
4. **优雅关闭**：应用退出或热切换配置时，主动关闭底层 httpx 连接池；**无运行事件循环时依赖 `close_all()` 显式调用**（见「`_pending_closes` 的释放时机」）

---

## 核心设计

### 两层存储

```
_configs: dict[str, dict]    # 配置存储（key → {api_key, base_url, model, ...}）
_instances: dict[str, AsyncOpenAI]  # client 缓存（key → AsyncOpenAI）
```

两层的含义：

- **注册配置**只写 `_configs`，不创建 client
- **获取 client**时检查 `_instances`，未命中则从 `_configs` 构建

### 懒加载流程

```
register_config("main", api_key=..., base_url=..., model=...)
    └─ 只写 _configs，不创建 client

get_client("main")  ← 首次调用
    ├─ _instances 无缓存
    ├─ 从 _configs 读取配置
    ├─ 筛选 AsyncOpenAI 合法参数（_OPENAI_CLIENT_KWARGS）
    ├─ 可选代理（proxy_url → httpx.AsyncClient）
    └─ AsyncOpenAI(**kwargs) → 存入 _instances 并返回

get_client("main")  ← 后续调用
    └─ 直接返回 _instances 中缓存的 client
```

### 参数透传

`register_config` 接收 `**extra`，`get_client` 只提取 `_OPENAI_CLIENT_KWARGS` 中定义的字段传给 `AsyncOpenAI`：

```python
_OPENAI_CLIENT_KWARGS = {
    "api_key", "organization", "base_url", "timeout",
    "max_retries", "default_headers", "default_query",
    "http_client", "websocket_client",
}
```

超出这个集合的额外参数（如 `model`、`proxy_url`）存储在 `_configs` 中供其他方法使用，但不会透传给 `AsyncOpenAI`。

---

## 讨论与决策

### Q1: 只清理引用 vs 主动关闭连接

| 操作                | 行为                                             | 适用场景                      |
| ------------------- | ------------------------------------------------ | ----------------------------- |
| `remove(key)`       | 仅从字典中弹出，不关闭连接，依赖 GC 回收         | 进程退出、测试临时使用        |
| `close_client(key)` | 先 `await client.close()` 再弹出，立即释放连接池 | Server 运行时热切换、优雅关闭 |

**关键区别**：`AsyncOpenAI` 内部持有 `httpx.AsyncClient` 维护 TCP keep-alive 连接池。只清理引用不会发送 FIN，端口和 socket 资源要等 GC。在长驻进程（FastAPI Server）中应始终使用 `close_client()`。

### Q2: `register_config` 热切换时要不要关闭旧 client？

**要。** 当初次实现时，`register_config` 只做 `cls._instances.pop(key, None)`——新配置进来只是弹出旧引用，旧连接泄漏到 GC。

修复方案（2026-08-09 增强：无 loop 不再静默忽略，进入 `_pending_closes` 追踪）：

```python
old = cls._instances.pop(key, None)
if old is not None:
    try:
        asyncio.get_running_loop()  # 仅判断是否有运行中事件循环
    except RuntimeError:
        cls._pending_closes.append(old)  # 无 loop：登记待关闭，close_all 统一关
    else:
        asyncio.ensure_future(old.close())  # 有 loop：后台异步关闭
```

- 使用 `asyncio.ensure_future` 而非 `await`，因为 `register_config` 不是 async 方法，有运行循环时后台关闭不阻塞注册
- 用 `asyncio.get_running_loop()` 显式判断而非依赖 `ensure_future` 抛异常：无运行循环（纯注册阶段，如 AppState.initialize 之前）时旧 client 放入 `_pending_closes` 列表，由 `close_all()` 统一关闭——**不再静默忽略**，避免旧连接池泄漏且可追踪

**`_pending_closes` 的释放时机**：无运行事件循环阶段积累的旧 client 不会自我关闭，必须由 `close_all()` 显式触发，否则旧 `AsyncOpenAI` 的 httpx 连接池泄漏。`close_all()` 的调用方约定：

- **正常路径**：`AppState.shutdown()`（FastAPI lifespan 关闭事件）调用 `ClientManager.close_all()`，应用退出即统一关闭 `_instances` 与 `_pending_closes`
- **兜底路径**：无 lifespan 的场景（测试 tearDown、独立脚本）必须显式 `await ClientManager.close_all()`——测试用 `autouse` fixture 清理状态时不只要 `clear()` 字典，还应关闭 `_pending_closes` 中的旧 client，否则测试进程内连接池残留

### Q3: `**extra` 被静默吞没了

旧实现中 `get_client` 只取了 `api_key` 和 `base_url`，调用方传入的 `organization`、`timeout`、`max_retries` 等参数虽存入了 `_configs`，但永远不会传给 `AsyncOpenAI`。

修复：定义 `_OPENAI_CLIENT_KWARGS` 白名单，`get_client` 从配置中筛选交集的字段。

---

## 对外接口

| 方法                                                      | 同步/异步 | 说明                                 |
| --------------------------------------------------------- | --------- | ------------------------------------ |
| `register_config(key, api_key, base_url, model, **extra)` | 同步      | 注册配置；有运行循环则旧 client 后台异步关闭，无则入 `_pending_closes` 追踪 |
| `get_client(key)`                                         | 同步      | 获取 / 创建 client（懒加载）         |
| `get_model(key)`                                          | 同步      | 获取配置中的模型名                   |
| `get_config(key)`                                         | 同步      | 获取完整配置副本                     |
| `list_keys()`                                             | 同步      | 列出所有已注册 key                   |
| `close_all()`                                             | 异步      | 关闭所有 client + 待关闭列表，并清理 |
| `close_client(key)`                                       | 异步      | 关闭并移除指定 client                |
| `remove(key)`                                             | 同步      | 仅移除引用，不关闭连接               |

---

## 边界情况

1. **多次注册同一 key**：先关闭旧 client，再覆盖配置，下次 `get_client` 创建新实例
2. **未注册 key**：`get_client` / `get_model` / `get_config` 抛出 `ValueError`
3. **无事件循环时注册（2026-08-09 修复）**：`asyncio.get_running_loop()` 判无运行循环 → 旧 client 放入 `_pending_closes` 由 `close_all()` 统一关闭，**不再静默忽略**（修复前旧连接池泄漏）。**注意**：`_pending_closes` 中的旧 client 不会自我关闭——若 `close_all()` 未被调用（应用未启动事件循环、或测试 tearDown/独立脚本未显式关闭），旧 httpx 连接池将泄漏，必须在这些场景显式 `await ClientManager.close_all()`（见「`_pending_closes` 的释放时机」）
4. **并发 get_client**：Python GIL + dict 操作原子性，首次创建在锁外可能有重复创建，但 `AsyncOpenAI` 本身是线程安全的，覆盖旧实例即可
5. **代理 client 的生命周期**：`_build_proxied_client` 创建的 `httpx.AsyncClient` 由 `AsyncOpenAI` 接管关闭，无需单独管理
