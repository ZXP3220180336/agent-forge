# 工具生命周期范式：有状态连接池 + 钩子 vs LangChain 无状态

> **状态**：✅ 已采纳
> **决策日期**：2026-08-17
> **涉及模块**：`app/integration/tools/base.py`（生命周期钩子）· `app/integration/tools/tool_service.py`（shutdown）· `app/integration/tools/loader.py`（外部工具钩子消费）· `app/integration/tools/builtin/web_browse.py`（连接池回收）· `app/container.py`（关闭接线）
> **关联文档**：[tools.md](../../../docs/integration_doc/tools_doc/tools.md) · [tool_service.md](../../../docs/integration_doc/tools_doc/tool_service.md) · [external.md](../../../docs/integration_doc/tools_doc/external.md)

---

## Context

**LangChain 无状态工具范式（调研记录）**

- `BaseTool`（langchain_core）**没有 on_load / on_unload 生命周期钩子**——这是刻意设计：工具被要求设计为**无状态**，即实例不跨调用持有可变资源（连接 / 缓存 / 会话），每次调用是纯粹的「输入 → 处理 → 输出」
- **实现方式**：资源在 `_run` / `_arun` 内部按调用创建与释放——典型为 `async with httpx.AsyncClient() as client:`（或 `aiohttp.ClientSession()`），`async with` 保证异常 / 取消时也正确释放；纯逻辑工具则无任何 `with`
- **收益**：① 零生命周期管理（不持有资源 → 宿主无需管理工具资源）；② 天然并发安全（无共享可变状态 → 无竞态）；③ 可重试 / 幂等（无残留状态干扰）；④ 可自由动态增删（卸载即丢弃实例，无泄漏）
- **代价**：每次调用新建连接 → TCP + SSL 握手开销，放弃连接池复用

**分水岭判断**

| | 无状态（每次调用建/毁） | 有状态（跨调用持连接/子进程/定时器） |
| --- | --- | --- |
| 需要生命周期钩子？ | 不需要 | **必须**（on_load 建 / on_unload 释放） |
| 生命周期管理成本 | 零 | 需钩子 + 宿主协调 |

**本项目场景**

- 工具为**能力调用类**（搜索 / 网页抓取 / HTTP / 文件 / 命令），连接池复用有价值（免握手、高频调用场景收益真实）
- 生命周期钩子机制已就位：`BaseTool.on_load / on_unload / health_check` + `ExternalToolLoader` 在外部工具加载 / 卸载时消费
- 工业实践（agentflow Cleanup / Cordis 可逆副作用）：**内置工具生命周期随应用**（宿主 shutdown 统一清理，关闭链含超时上下文），**外部工具随插件生命周期**（loader 卸载触发 on_unload）

## Decision

**保留「有状态连接池 + 生命周期钩子」范式（不采用 LangChain 无状态范式），并补齐内置工具的生命周期回收链路。**

- **外部工具**：`ExternalToolLoader` 卸载时调 `on_unload()`（已实现，幂等）——对齐 Cordis「每个注册有 disposer」契约
- **内置工具**：随应用生命周期，`ToolService.shutdown()` 遍历已注册工具调 `on_unload()`（try/except 单工具失败不阻断；幂等可重复调用）
- **关闭顺序**：`container.shutdown` **最前**调 `tool_service.shutdown()`——`on_unload` 可能依赖 redis / LLM，须在基础设施关闭前执行（对齐 agentflow 关闭清理链）
- **web_browse** 实现 `on_unload()` 关闭全局 httpx client（连接池随应用关闭优雅回收，防连接泄漏）
- **无状态范式边界**：不整体采用，但保留其适用性——纯逻辑工具（无跨调用资源）默认即无状态，无需钩子；是否用连接池取决于「复用收益 vs 生命周期成本」

## Consequences

- **正面**：连接池复用（免 TCP/SSL 握手）；内外工具统一到同一套生命周期钩子语义（外部工具卸载走 loader、内置工具关闭走 container.shutdown）；资源完整回收对齐工业契约（防连接 / 句柄泄漏）；为未来高频工具调用留路
- **负面**：工具需理解并正确实现生命周期钩子（非零心智成本）；`shutdown` 遍历有一次性开销（应用关闭时执行一次，可忽略）；有状态工具需注意共享状态的并发安全（本项目连接池无共享可变状态，天然安全）
