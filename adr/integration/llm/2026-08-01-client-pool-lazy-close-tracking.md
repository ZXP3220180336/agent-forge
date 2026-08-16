# 连接池管理：懒加载 + 主动关闭 + 热切换关闭追踪

> **状态**：✅ 已采纳
> **决策日期**：2026-08-01（初建确立；关闭追踪 08-09/08-11 增强）
> **涉及模块**：`app/integration/llm/client.py`（`ClientManager`）
> **关联文档**：[client.md](../../../docs/integration_doc/llm_doc/client.md) · [llm.md](../../../docs/integration_doc/llm_doc/llm.md)

---

## Context

- `AsyncOpenAI` 内部持有 `httpx.AsyncClient` 维护 **TCP keep-alive 连接池**——只清理引用不会发送 FIN，端口/socket 资源要等 GC；长驻进程（FastAPI Server）多次热重配（`register_config`）会积累旧 client 连接池。
- 关闭动作的时机选择：依赖 GC vs 主动关闭；有无运行事件循环的差异（`asyncio.ensure_future` 只在有 loop 时可用）。
- 初次实现 `register_config` 只做 `cls._instances.pop(key, None)`——新配置进来只是弹出旧引用，旧连接泄漏到 GC。

## Decision

**`ClientManager` 采用懒加载 + 全局缓存；长驻进程主动关闭；热切换时旧 client 按有无运行事件循环分派关闭并全程追踪。**

1. **懒加载 + 全局缓存**：`get_client` 首次按 key 创建 `AsyncOpenAI`，后续复用——避免每次新建的握手开销。
2. **长驻进程主动关闭**：`close_client(key)`（先 `await client.close()` 再弹出，立即释放连接池）用于 Server 运行时热切换、优雅关闭；`remove(key)`（仅弹出依赖 GC）仅用于进程退出、测试临时使用。**长驻进程应始终使用 `close_client()`**——只清理引用不会发送 FIN。
3. **热切换关闭旧 client**：`register_config` 弹出旧 client 后，按有无运行事件循环分派：
   - **无 loop**（纯注册阶段 / 同步测试 / 独立脚本）：进入 `_pending_closes`，由 `close_all()` 统一关闭——**不再静默忽略**，避免旧连接池泄漏且可追踪；
   - **有 loop**：后台 `asyncio.ensure_future(old.close())` 存入 `_closing_tasks`——无引用 task 在事件循环先关闭时产生 pending 警告、异常无人消费，必须追踪 + 完成回调清理 + 异常消费；`close_all()` 先 `gather(*_closing_tasks, return_exceptions=True)` 等待清空，再快照遍历 `_instances`/`_pending_closes` 逐个关闭（避免与后台关闭并行竞态）。
   - 用 `asyncio.get_running_loop()` **显式判断**（而非依赖 `ensure_future` 抛异常）区分有/无 loop。
4. **`close_all()` 释放时机约定**：正常路径 `Container.shutdown()`（FastAPI lifespan 关闭事件）调用；兜底路径——无 lifespan 场景（测试 tearDown、独立脚本）必须显式 `await ClientManager.close_all()`，测试 `autouse` fixture 清理时不只 `clear()` 字典，还应关闭 `_pending_closes` 中旧 client。

## Consequences

- **正面**：长驻进程多次热重配不累积泄漏；关闭动作全程可追踪（`_pending_closes` / `_closing_tasks`）；有/无事件循环两种上下文都有明确关闭路径。
- **负面**：`_pending_closes` 中的旧 client **不会自我关闭**——若 `close_all()` 未被调用（无 lifespan 的测试/脚本），httpx 连接池残留；关闭时机的正确性依赖调用方遵守「必须显式 close_all」的约定。
