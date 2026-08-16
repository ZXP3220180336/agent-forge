# ClientManager 设计文档

> **模块**：`app/integration/llm/client.py`
> **更新日期**：2026-08-16
> **职责**：全局共享 `AsyncOpenAI` client 实例，支持多模型 key 隔离
> **状态**：✅ 已实现

---

## 📋 目录

- [ClientManager 设计文档](#clientmanager-设计文档)
  - [📋 目录](#-目录)
  - [设计目标](#设计目标)
  - [核心概念解释](#核心概念解释)
    - [两层存储](#两层存储)
    - [懒加载（Lazy Loading）](#懒加载lazy-loading)
    - [参数透传（白名单）](#参数透传白名单)
    - [优雅关闭与关闭追踪](#优雅关闭与关闭追踪)
  - [架构总览](#架构总览)
  - [组件详解](#组件详解)
    - [ClientManager — 连接池管理器](#clientmanager--连接池管理器)
    - [\_OPENAI\_CLIENT\_KWARGS — 参数白名单](#_openai_client_kwargs--参数白名单)
    - [\_build\_proxied\_client — 代理客户端构建](#_build_proxied_client--代理客户端构建)
    - [关闭追踪机制（\_pending\_closes / \_closing\_tasks）](#关闭追踪机制_pending_closes--_closing_tasks)
  - [执行流程](#执行流程)
    - [获取 client（懒加载）](#获取-client懒加载)
    - [热切换关闭（register\_config 旧 client）](#热切换关闭register_config-旧-client)
    - [关闭指定 client（close\_client）](#关闭指定-clientclose_client)
    - [统一关闭（close\_all）](#统一关闭close_all)
  - [配置项清单](#配置项清单)
  - [边界情况](#边界情况)
  - [设计决策](#设计决策)
  - [问题记录](#问题记录)

---

## 设计目标

1. **连接池复用**：避免每次请求创建新 `AsyncOpenAI` 实例导致的 TCP 握手和 SSL 开销
2. **多 key 隔离**：`main` / `reasoning` / `fast` 各自独立配置，按需获取
3. **懒加载**：配置注册后不立即创建 client，第一次使用时才实例化
4. **优雅关闭**：应用退出或热切换配置时，主动关闭底层 httpx 连接池；**无运行事件循环时依赖 `close_all()` 显式调用**（见「核心概念解释·优雅关闭与关闭追踪」）

---

## 核心概念解释

### 两层存储

```
_configs: dict[str, dict]    # 配置存储（key → {api_key, base_url, model, ...}）
_instances: dict[str, AsyncOpenAI]  # client 缓存（key → AsyncOpenAI）
```

两层的含义：

- **注册配置**只写 `_configs`，不创建 client
- **获取 client**时检查 `_instances`，未命中则从 `_configs` 构建

分离的意义：配置变更（热切换）写入 `_configs` 并清空 `_instances` 中对应旧实例（下次 `get_client` 用新配置重建）；`remove(key)` **同时移除配置与 client 实例**（不关闭连接，仅清理引用）。

### 懒加载（Lazy Loading）

配置注册时不创建 client，首次 `get_client` 才实例化——避免应用启动即建立所有模型的连接，只在真正使用某个 key 时创建对应 client。

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

### 参数透传（白名单）

`register_config` 接收 `**extra`，`get_client` 只提取 `_OPENAI_CLIENT_KWARGS` 中定义的字段传给 `AsyncOpenAI`：

```python
_OPENAI_CLIENT_KWARGS = {
    "api_key", "organization", "base_url", "timeout",
    "max_retries", "default_headers", "default_query",
    "http_client", "websocket_client",
}
```

超出这个集合的额外参数（如 `model`、`proxy_url`）存储在 `_configs` 中供其他方法使用，但不会透传给 `AsyncOpenAI`。

### 优雅关闭与关闭追踪

`AsyncOpenAI` 内部持有 `httpx.AsyncClient` 维护 TCP keep-alive 连接池。关闭 client 即释放连接池（发送 FIN）。关闭动作的**时机**取决于 `register_config` 热切换时是否处于运行事件循环：

- **有运行事件循环**：后台异步关闭（`asyncio.ensure_future(old.close())`），task 记录到 `_closing_tasks`，由 `close_all()` 统一等待
- **无运行事件循环**（纯注册阶段）：无法创建 task，旧 client 登记到 `_pending_closes`，由 `close_all()` 统一关闭

两条路径最终都会被 `close_all()` 等待，区别是「关闭动作何时开始」。二者都不会自我关闭，必须由 `close_all()` 显式触发，否则旧连接池泄漏。

---

## 架构总览

```
Container.initialize() / LLMService / 外部调用方
        │
        ▼
    ClientManager（类级单例，跨请求共享）
    ├── _configs         配置存储（key → 配置 dict）
    ├── _instances       client 缓存（key → AsyncOpenAI）
    ├── _pending_closes  无 loop 阶段积累的待关闭旧 client
    └── _closing_tasks   有 loop 后台关闭旧 client 的 task
        │
        ├───────────────► AsyncOpenAI（OpenAI SDK，底层 httpx 连接池）
        │                        └─ 可选代理（_build_proxied_client → httpx.AsyncClient）
        ▼
    LLMService（Facade）经 ClientManager.get_client(key) 获取 client 发起调用
```

| 层 | 组件 | 职责 |
| --- | --- | --- |
| 存储层 | `_configs` | 配置注册表（key → 配置），`register_config` 写入 |
| 缓存层 | `_instances` | 已实例化 client 缓存，`get_client` 懒加载写入 |
| 关闭追踪 | `_pending_closes` | 无事件循环时旧 client 待关闭列表 |
| 关闭追踪 | `_closing_tasks` | 有事件循环时后台关闭 task 列表 |
| 底层 | `AsyncOpenAI` | OpenAI SDK 客户端（httpx 连接池） |

---

## 组件详解

### ClientManager — 连接池管理器

```python
class ClientManager:
    _instances: ClassVar[dict[str, AsyncOpenAI]] = {}
    _configs: ClassVar[dict[str, dict[str, Any]]] = {}
    _pending_closes: ClassVar[list[AsyncOpenAI]] = []
    _closing_tasks: ClassVar[list[asyncio.Task]] = []

    @classmethod
    def register_config(cls, key, api_key, base_url, model, **extra) -> None
    @classmethod
    def get_client(cls, key="main") -> AsyncOpenAI
    @classmethod
    def get_model(cls, key="main") -> str
    @classmethod
    def get_config(cls, key="main") -> dict
    @classmethod
    def list_keys(cls) -> list[str]
    @classmethod
    async def close_all(cls) -> None
    @classmethod
    async def close_client(cls, key) -> None
    @classmethod
    def remove(cls, key) -> None
    @classmethod
    def _on_closing_task_done(cls, task: asyncio.Task) -> None
```

**类变量即单例存储**：全部为 `ClassVar`，任何模块通过 `ClientManager.xxx` 访问的都是同一份状态——保证 `main` / `reasoning` / `fast` 三个 client 在进程内唯一，跨请求复用连接池。

**方法契约要点**：`get_client` / `get_model` / `get_config` 对未注册 key 抛 `ValueError`；`get_config` 返回配置**副本**（dict 拷贝）；`get_client` 对缺失字段有默认值兜底（`api_key=""`、`base_url="https://api.openai.com/v1"`）。

### _OPENAI_CLIENT_KWARGS — 参数白名单

`get_client` 从配置中筛选交集的字段传给 `AsyncOpenAI`，白名单外的参数（`model`、`proxy_url`）只存 `_configs` 不透传。**设计意图**：`register_config` 接收业务语义配置（`model` 供 `get_model` 用、`proxy_url` 供代理构建），`AsyncOpenAI` 构造函数只认固定参数集——白名单隔离两套字段，避免非法参数透传给 SDK 抛 TypeError。

### _build_proxied_client — 代理客户端构建

构建带代理的 `httpx.AsyncClient`，作为 `AsyncOpenAI(http_client=...)` 的自定义传输。**httpx 延迟导入**（`import httpx` 在函数内）——未安装 httpx 且未配置代理时，模块加载不受影响；配置了代理但未安装时抛 `ImportError("使用代理需要安装 httpx")`。

### 关闭追踪机制（_pending_closes / _closing_tasks）

**`_pending_closes`**：无运行事件循环时（纯注册阶段，如 `Container.initialize` 之前），`register_config` 无法 fire-and-forget 关闭旧 client，登记到此列表由 `close_all` 统一关闭（可追踪）。

**`_closing_tasks`**：有运行事件循环时，`asyncio.ensure_future(old.close())` 返回的 task 记录到此列表。必须追踪的原因：

1. 无引用的 task 在事件循环先关闭、task 未完成时产生 "Task was destroyed but it is pending" 警告，旧连接池关闭时机不可控
2. task 异常无人消费产生 "Task exception was never retrieved"
3. `close_all()` 需 `asyncio.gather(*_closing_tasks, return_exceptions=True)` 等待其完成，避免与后台关闭并行竞态

**完成回调清理**：每个后台 close task 挂 `add_done_callback(_on_closing_task_done)`——task 完成后**自动从 `_closing_tasks` 移除**（多次热切换不累积已完成 task 引用）+ **消费 task 异常记 `logger.warning`**（后台关闭失败不静默，与 `close_all` 对 `_instances`/`_pending_closes` 逐 client 关闭失败记日志对称）；task 被**取消**则不消费异常（取消非失败，正常静默）。`close_all` 的 `gather` 在参数展开时已快照列表，回调移除不破坏等待。

---

## 执行流程

### 获取 client（懒加载）

```
get_client("main")
  ├─ "main" in _instances ?
  │    ├─ 是 → 返回缓存 client
  │    └─ 否 → "main" in _configs ?
  │            ├─ 否 → raise ValueError("Client key 'main' 未注册")
  │            └─ 是 → 从 _configs 读取配置
  │                    ├─ 筛选 _OPENAI_CLIENT_KWARGS 交集字段
  │                    ├─ 默认值兜底：api_key=""、base_url="https://api.openai.com/v1"
  │                    ├─ 有 proxy_url → _build_proxied_client(proxy_url)（转 http_client）
  │                    └─ AsyncOpenAI(**client_kwargs) → 存入 _instances 并返回
```

### 热切换关闭（register_config 旧 client）

```
register_config(key, ...)
  ├─ _configs[key] = {api_key, base_url, model, **extra}
  ├─ old = _instances.pop(key, None)
  │    ├─ old is None → 无旧实例，结束
  │    └─ old is not None →
  │         ├─ 有运行事件循环 → task = ensure_future(old.close())
  │         │    └─ task.add_done_callback(_on_closing_task_done) → _closing_tasks.append(task)
  │         └─ 无运行事件循环 → _pending_closes.append(old)
  └─ 下次 get_client 用新配置创建新实例
```

### 关闭指定 client（close_client）

```
close_client("main")
  ├─ client = _instances.pop("main", None)
  ├─ client is None → 无实例，结束
  └─ client is not None → await client.close()（关闭该 key 连接池并移除）
```

### 统一关闭（close_all）

```
close_all()
  ├─ 先等待后台 task：asyncio.gather(*_closing_tasks, return_exceptions=True)
  │    └─ _closing_tasks.clear()（异常隔离，不中断）
  ├─ 快照遍历 _instances：for client in list(_instances.values())
  │    └─ 逐个 close（try/except Exception + logger.warning 隔离）
  ├─ _instances.clear()
  ├─ 快照遍历 _pending_closes：逐个 close（同上，异常隔离）
  └─ _pending_closes.clear()
```

**关键设计**：先快照再逐个关闭（`list()` 隔离迭代与字典修改，`await` 让出事件循环控制权时并发 `register_config`/`close_client` 修改字典不会抛 `RuntimeError`）；单个 `close()` 异常用日志隔离，不中断其余清理。

---

## 配置项清单

`register_config` 的参数（来自 `Container.initialize()` 读 settings 后调用）：

| 参数 | 类型 | 说明 | 透传给 AsyncOpenAI |
| --- | --- | --- | --- |
| `key` | str | 配置标识（`main` / `reasoning` / `fast`） | - |
| `api_key` | str | API 密钥 | 是 |
| `base_url` | str | API 端点（如 DeepSeek 兼容端点） | 是 |
| `model` | str | 模型名（供 `get_model` 使用） | 否 |
| `organization` | str | 组织 ID | 是 |
| `timeout` | float | 请求超时（秒） | 是 |
| `max_retries` | int | SDK 内部重试次数 | 是 |
| `default_headers` | dict | 默认请求头 | 是 |
| `default_query` | dict | 默认查询参数 | 是 |
| `http_client` | httpx.AsyncClient | 自定义 HTTP 客户端（代理等） | 是 |
| `websocket_client` | - | WebSocket 客户端 | 是 |
| `proxy_url` | str | 代理地址（触发 `_build_proxied_client`） | 否（转 http_client） |

---

## 边界情况

1. **多次注册同一 key**：先关闭旧 client，再覆盖配置，下次 `get_client` 创建新实例
2. **未注册 key**：`get_client` / `get_model` / `get_config` 抛出 `ValueError`
3. **无事件循环时注册**：`asyncio.get_running_loop()` 判无运行循环 → 旧 client 放入 `_pending_closes` 由 `close_all()` 统一关闭（可追踪）。**注意**：`_pending_closes` 中的旧 client 不会自我关闭——若 `close_all()` 未被调用（应用未启动事件循环、或测试 tearDown/独立脚本未显式关闭），旧 httpx 连接池将泄漏，必须在这些场景显式 `await ClientManager.close_all()`（见「核心概念解释·优雅关闭与关闭追踪」）
4. **并发 get_client**：Python GIL + dict 操作原子性，首次创建在锁外可能有重复创建，但 `AsyncOpenAI` 本身是线程安全的，覆盖旧实例即可
5. **代理 client 的生命周期**：`_build_proxied_client` 创建的 `httpx.AsyncClient` 由 `AsyncOpenAI` 接管关闭，无需单独管理
6. **close_all 迭代期间并发修改**：先 `list()` 快照再逐个关闭，`await client.close()` 让出事件循环控制权时并发 `register_config()`/`close_client()` 修改字典不会抛 `RuntimeError`
7. **单个 close 异常隔离**：`client.close()` 抛异常（连接池关闭失败）用 `try/except Exception` + `logger.warning` 隔离，不中断其余 client 与 `_pending_closes` 的关闭

---

## 设计决策

> 连接池管理决策（懒加载 + 主动关闭 + 热切换关闭追踪，Context → Decision → Consequences）已归档至 [ADR LLM-ADR-004](../../../adr/integration/llm/2026-08-01-client-pool-lazy-close-tracking.md)。
> Q3（`**extra` 参数白名单）为问题记录，完整生命周期见 [LLM-033](../../../issues/integration/llm/2026-08-09-client-kwargs-whitelist.md)。
---

## 问题记录

> 审核发现的问题已提取归档，完整生命周期（发现 → 分析 → 修复 → 验证 → 教训）见：

- [热切换旧 client 静默忽略 + 后台关闭 task 无引用/累积/失败静默（连接关闭追踪演进）](../../../issues/integration/llm/2026-08-09-client-close-tracking.md)
- [close_all 迭代共享字典（并发修改 RuntimeError / 异常中断整批）](../../../issues/integration/llm/2026-08-11-close-all-iteration-safety.md)
- [`**extra` 参数被静默吞没（get_client 参数透传白名单）](../../../issues/integration/llm/2026-08-09-client-kwargs-whitelist.md)
