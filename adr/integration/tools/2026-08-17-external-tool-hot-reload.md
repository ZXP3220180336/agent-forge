# 外部工具热加载（内嵌式可信插件档 + 分层对齐工业蓝图）

> **状态**：✅ 已采纳
> **决策日期**：2026-08-17
> **涉及模块**：`app/integration/tools/loader.py`（ExternalToolLoader）· `app/integration/tools/base.py`（生命周期钩子）· `app/integration/tools/tool_service.py`（execute 惰性检查入口）· `app/integration/tools/executor.py`（prune 竞态修复）
> **关联文档**：[external.md](../../../docs/integration_doc/tools_doc/external.md) · [tools.md](../../../docs/integration_doc/tools_doc/tools.md)

---

## Context

- `app/integration/tools/external/` 自工具模块重构起预留「外部工具」空包，一直无实现；用户确认落地
- 工业级热插拔（调研）：主流框架（LangChain / AutoGen / CrewAI）标准 = **动态注册 API + 下次调用生效**；MCP 用 `tools/list` + `list_changed` 通知（跨进程，本项目不引入）；「修改保存即生效」工业界普遍不承诺（JVM HotSwap 仅方法体、生产用蓝绿/滚动发布）；开发期热更新靠 uvicorn --reload
- 本项目已天然满足「下次调用生效」：`get_openai_tools()` 每轮实时导出，外部工具注册后下一轮 Agent 即可调用——缺的仅是「从磁盘发现工具并纳入注册中心」的加载器
- 工业蓝图将热插拔分为内嵌式 / 独立服务式两档：内嵌式适配「内部可信 / 轻量 / 性能要求高」——正是本项目场景

## Decision

**引入 `ExternalToolLoader`，按工业蓝图「内嵌式可信插件」档落地，独立服务式能力留升级路径。**

- **感知机制 = execute 惰性检查**：`ToolService.execute` 入口调 `maybe_refresh()` 对比目录签名（文件集 + mtime/size），变化才重扫。**无后台任务**（后台轮询是非典型做法且引入任务生命周期）。文件新增 / 修改 / 删除 → 下一次工具调用生效
- **生命周期钩子补齐**：`BaseTool.on_load()` / `on_unload()` / `health_check()`（非抽象，默认 no-op）。`on_load` 加载注册前调用；`on_unload` 注销前调用（强制资源回收）；`health_check` 预留巡检
- **同名拒绝**：与已注册工具（builtin / 其他 external）重名 → 跳过 + warning，builtin 权威
- **全链路留痕**：loader 加载 / 重载 / 卸载 / 冲突 / 失败结构化日志，配合 `tool_call` 调用审计
- **执行细节**：importlib 动态导入（模块名：合法 stem 用真实包名、非法用 sha1 哈希）；exec_module 前先插入 `sys.modules`（自引用/相对导入）；`to_thread` 包裹（防阻塞事件循环）；文件级原子性（多工具部分失败全回滚）；重载 nuke-and-repave
- **executor 竞态修复**：`prune_tool_lock` 跳过仍在持有的锁——在飞 execute 持锁时重载，新实例复用同一把锁，串行化不破坏
- **配置**：外部工具容器不知其存在，**自行读环境变量**；loader 不加 `config_provider` 扩展点

**明确不做（升级路径，触发条件见下）**：后台轮询 / 原子无损（引用计数 + 版本化实例）/ 元数据与实现分离 + 懒加载 / 多版本灰度回滚 / 沙箱隔离（子进程 / WASM / Sidecar）/ health_check 自动巡检。

## Consequences

- **正面**：零重启动态加载，对齐工业标准「变更 → 下次调用生效」；无后台任务（无生命周期管理成本）；外部工具自动获得 executor 全量横切关注点（校验 / 超时 / 重试 / 截断 / 审计 / 并发）；生命周期钩子保证资源可回收；全链路可观测
- **负面**：`external/` 任意 .py 被加载即执行任意代码（信任边界，只放受信任工具）；重载为 nuke-and-repave——文件改坏期间工具暂不可用（修复即恢复）；`on_unload` 与在飞 execute 不保证互斥（正常 uvicorn 停止先停收请求，可接受）；跨文件 `from . import helper` 存在传递性陈旧（建议单文件自包含）
- **升级路径**：原子无损（出现「重载窗口不可接受」）→ 引用计数 + 版本化实例；元数据分离（工具 > 数百）→ 元数据注册中心 + 懒加载；沙箱（不可信第三方工具）→ 子进程 / WASM / Sidecar；health_check 巡检（插件数量上升）→ 巡检器 + 异常隔离
