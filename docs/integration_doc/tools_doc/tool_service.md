# ToolService 工具服务说明文档

> **更新日期**：2026-09-19
> **模块**：`app/integration/tools/tool_service.py`
> **文档定位**：ToolService 独立说明 —— 工具系统的对外统一入口（容器 + 执行 + 统计 + 钩子 + 内置工具装配 + 选择 + 校验 + 截断 + 审计）。
> 状态与验证见 [ALIGNMENT](../../ALIGNMENT.md)。

---

## 📋 目录

- [模块概述](#模块概述)
- [核心类与方法](#核心类与方法)
- [关键实现详解](#关键实现详解)
- [使用示例](#使用示例)
- [配置关联](#配置关联)
- [相关文档](#相关文档)

---

## 模块概述

### 定位与职责

ToolService 是**工具系统的对外统一入口**（Facade），聚合六大子组件 + 辅助组件：

1. **工具容器**：注册 / 注销 / 查询 / 列表 / 按风险与分类过滤（Registry）
2. **Schema 导出**：`get_openai_tools()` 经选择器选出注入子集 / `get_openai_responses()` 导出全部已启用工具
3. **工具执行**：`execute()` 带参数校验（jsonschema 归因）、自动重试（指数退避）、超时保护、结果截断、审计留痕、人工审批拦截、并发控制
4. **执行统计**：`ToolStats` 记录调用次数 / 成功率 / 平均耗时 / 最后调用时间
5. **钩子机制**：工具执行成功后运行扩展钩子（异步 / 同步皆可，钩子失败不影响工具执行）
6. **内置工具装配**：`init_default_tools()` 用 importlib 扫描 `builtin` 包，幂等注册
7. **外部工具热加载**：`execute` 入口惰性检查 `external/` 目录，变化即重扫（无常驻扫描，临时刷新由 loader 接管，见 [external.md](external.md)）
8. **生命周期回收**：`shutdown()` 停止新调用，排空刷新和真实工具工作后才调用 `on_unload`；未完成时明确抛错并保留依赖。

### 组件装配

```text
ToolService（Facade，唯一对外入口，实现 ToolGateway）
├── ToolRegistry        注册中心：容器 + Schema 导出 + 元数据查询
├── ToolSelector        选择器：选注入子集（默认全量注入）
├── ParameterValidator  校验器：jsonschema 严格校验 + 错误归因
├── ToolAdmission       共享准入：全局/单运行容量、有界排队、取消可中断等待
├── ToolExecutionSupervisor  真实任务/线程、有界清理和迟回值接管
├── ToolExecutor        调度器：准入 / 重试 / 超时 / 截断 / 审计编排
├── ResultProcessor     结果处理器：head+tail 截断 + 错误归一化
├── ToolAuditor         安全审计：风险分级 + 审计留痕（日志，不拦截）
├── ApprovalGate        审批通道：requires_approval 工具执行前确认（默认放行）
├── ToolStatsCollector  统计
├── ExecutionHooks      钩子
├── ToolAssembler       内置工具装配
└── ExternalToolLoader  外部工具热加载（execute 惰性检查 + 生命周期钩子）
```

- `container` 持有单例实例，`get_tool_service` 依赖注入返回同一实例（见 [集成层总览](../README.md)）
- 新组件（selector / validator / result_processor / auditor / approval_gate）均可构造期注入自定义实现，缺省用内置默认

### 设计原则

1. **共享准入**：`ToolAdmission` 同时维护全局与单运行在途上限，多个 Agent 共用同一进程内容量
2. **有界等待**：全局/单运行排队有上限，取消和 deadline 可中断等待；队列溢出不进入工具重试
3. **Permit 幂等释放**：Supervisor 在真实工作结束后释放许可；start 未成功时仍由 Executor 归还
4. **幂等装配**：`init_default_tools` 按**注册 key（实例 `tool.name`）**判断是否已存在，重复调用不重复注册
5. **审计常开**：审计默认启用（不设 settings 开关），L2 起 WARNING 级别便于 ops 检索

模型导出及正式调用遵守[能力启用边界](execution.md#当前启用边界)。注册容器可保留未启用工具；当前只读集合进入两种模型协议，直接调用写入/未知/强制审计能力返回 REJECTED。

## 核心类与方法

> `ToolStats` / `ToolStatsCollector` 定义见 [stats.md](stats.md)。

### `ToolService` 方法

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `register` | `(tool: BaseTool) -> None` | 注册工具；重名抛 `ValueError`，参数定义非法抛 `SchemaError`；注册成功后初始化统计 |
| `unregister` | `(name: str) -> bool` | 注销工具及其统计与 per-tool 锁 |
| `get` | `(name: str) -> BaseTool \| None` | 获取工具实例 |
| `is_tool_active` | `(tool: BaseTool) -> bool` | 该工具是否仍有在途调用或由 Supervisor 持有的真实执行；loader 据此延后卸载旧版本 |
| `list_tools` | `() -> list[str]` | 列出全部已注册工具名 |
| `list_by_risk` | `(risk_level: RiskLevel) -> list[BaseTool]` | 按风险等级过滤（预留管理界面） |
| `list_by_category` | `(category: str) -> list[BaseTool]` | 按功能域过滤（预留管理界面） |
| `get_openai_tools` | `() -> list[dict]` | OpenAI Tool Schema（启用过滤后经选择器） |
| `get_openai_responses` | `() -> list[dict]` | OpenAI Response Schema（全部已启用工具） |
| `execute` | `(name, parameters, timeout=None, max_retries=None, retry_delay=1.0, *, call, facts) -> ToolResult` | `call/facts` 必填；入口先预登记调用事实并复查运行控制，再按需做外部工具惰性检查，最后委托 Executor |
| `get_stats` | `(name=None) -> dict \| ToolStats \| None` | 单工具或全量统计 |
| `get_all_stats_summary` | `() -> dict` | 总调用 / 总成功 / 总失败 / 总成功率 / 各工具详情 |
| `add_execution_hook` | `(hook: Callable) -> None` | 注册执行钩子 `async def hook(tool_name, parameters, result)` |
| `init_default_tools` | `() -> list[str]` | 注册全部内置工具（幂等），返回新增**类名**列表 |
| `refresh_external_tools` | `async () -> None` | 手动触发外部工具重扫（加载新增 / 重载修改 / 卸载删除） |
| `shutdown` | `async () -> None` | 有界排空刷新和真实执行后卸载；未完成抛 ToolShutdownIncompleteError，保留依赖；成功关闭幂等 |

## 关键实现详解

### 执行编排（委托 ToolExecutor）

`execute()` 完整执行流程（共享准入 → 校验 → 重试 → 截断 → 审计 → 串行化）见 [executor.md](executor.md)，本处只列 Facade 视角要点：

- ToolAdmission 限制全局/单运行并发，Permit 成功转交 Supervisor 后仅在真实完成时释放
- 参数校验失败返回**可归因错误**（jsonschema，见 [validator.md](validator.md)）
- 成功结果 head+tail 统一截断（`tool.max_output_length`，见 [result_processor.md](result_processor.md)）
- 每次 `execute()` 审计 1 条最终结果，覆盖全路径（见 [security.md](security.md)）
- 次数预算只给出执行上界；适配器还须通过 `can_retry` 明确本次失败可安全重复，默认只执行一次
- 统计每次真实尝试记录一次，钩子仅成功路径触发；非关键观测失败或超时不覆盖工具结果（见 [stats.md](stats.md)）

### 内置工具装配 `init_default_tools`

```python
def init_default_tools(self) -> list[str]:
    """注册全部内置工具（幂等），返回新增类名列表。"""
    return self._assembler.assemble(self._registry, self._stats)
```

装配逻辑位于 `ToolAssembler.assemble`（[assembler.py](../../../app/integration/tools/assembler.py)）：扫描 builtin 包 → 实例化 → 幂等注册（按实例 `tool.name` 判断，`stats.init` 双写）→ 返回新增类名。

- `builtin.__all__` 由 `_discover_tools()` 自动扫描生成（见 [builtin.md](builtin_doc/builtin.md)）
- **幂等判断用实例 `tool.name`**（如 `"search"`）而非类名（`SearchTool`），避免重复注册
- 单工具实例化失败仅跳过该工具并记 warning（不影响其余注册与启动，`ToolAssembler` 内 try/except）

### 外部工具热加载

`execute` 入口先调 `ExternalToolLoader.maybe_refresh()`：对比 `external/` 目录签名，变化才重扫（加载 / 重载 / 卸载）。机制 / 生命周期钩子 / 约定见 [external.md](external.md)，本处只列 Facade 视角要点：

- 惰性检查对齐工业标准「变更 → 下次调用生效」，无常驻扫描任务；临时刷新任务有独立 Owner，在途旧实例的卸载会延后
- 外部工具自动获得 executor 全量横切关注点（校验 / 超时 / 重试 / 截断 / 审计 / 并发 / 审批）
- `refresh_external_tools()` 手动触发同语义重扫（供未来管理接口）
- **冷启动可见**：container 启动时主动 `refresh_external_tools()` 扫描一次，外部工具对 LLM 的 `get_openai_tools()` 注入立即可见（`execute` 惰性检查只覆盖运行期增量）

### 生命周期回收 `shutdown`

`Container.shutdown` 先调用工具服务，再关闭基础设施。工具服务立即关闭新调用入口，依次等待 loader 刷新事务、Executor 的准备/退避与真实任务退出，之后才卸载工具。整个调用在一个工具关闭窗口内等待；超出窗口抛 `ToolShutdownIncompleteError`，资源收尾任务继续由服务持有，Container 不会继续关闭仍可能被使用的数据库、Redis 或 LLM 依赖。

成功关闭幂等，不重复卸载；关闭任务失败后可再次尝试。外层等待被硬取消也不会放弃已启动的关闭任务。线程不能被协程取消强制停止，宿主强退属于后续切片，不能将有界返回解释为整个进程已退出。当前控制和所有权见 [Supervisor](execution.md)，既有范式来源见 [TOOLS-ADR-006](../../../adr/integration/tools/2026-08-17-tool-lifecycle-paradigm.md)，后继生命周期约束见 [TOOLS-ADR-008](../../../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md)。

### 边缘情况

- `parameters` 传 JSON 字符串解析失败 → 返回失败结果而非抛异常
- 并发下统计为同步字典更新（无锁），统计为尽力而为
- 统计 / 审计 / 钩子失败不影响工具结果；异步钩子和审计受 ToolService 的观察预算约束，钩子取得独立结果快照

---

## 使用示例

```python
# 执行已启用的内置工具（注册清单见 tools.md，此处以 search 为例）
result = await container.tool_service.execute(
    name="search",
    parameters={"query": "良率 RCA 案例"},
    timeout=30,
    max_retries=3,
    call=call_context,
    facts=batch_collector,
)
print(result.success, result.content, result.execution_time, result.retry_count)

# 获取 OpenAI 格式工具列表（注入 LLM tools 参数，经启用过滤后，选择器默认返回全部候选）
tools = container.tool_service.get_openai_tools()

# 按风险等级查询（预留管理界面）
dangerous = container.tool_service.list_by_risk(RiskLevel.L2_DANGEROUS)

# 注册自定义钩子（钩子失败不影响工具执行）
async def log_hook(tool_name, parameters, result):
    print(f"[hook] {tool_name} -> {result.success}")
container.tool_service.add_execution_hook(log_hook)

# 统计摘要
summary = container.tool_service.get_all_stats_summary()

# 内置工具装配（幂等，Container 启动时调用）
registered = container.tool_service.init_default_tools()

# 手动触发外部工具重扫（execute 已惰性自动检查，此为显式触发）
await container.tool_service.refresh_external_tools()
```

> 实际执行调用方：`app/domain/reasoning/react.py`，由 ReActAgent 及 Planner/Reflection 子 ReAct 间接消费；chat 路由创建 run 身份并把 `tool_service` 注入 Agent。

---

## 配置关联

相关配置集中在 `app/config/settings.py`（详见 [config 文档](../../config_doc/config.md) 工具配置 / Agent 并发控制节）。

> Container 将 `tool_max_concurrent_executions`、`tool_max_concurrent_executions_per_run`、两级 pending 上限和 `tool_admission_timeout_seconds` 注入 ToolService，由它构造唯一的 `ToolAdmission` 并注入 ToolExecutor；另显式构造冻结 `ToolExecutionSettings` 注入清理、观察、接管容量和关闭参数；`timeout` / `max_retries` 为调用方可覆盖的执行上限，是否重试仍由适配器安全声明决定。截断上限经内置工具 `register_config` 注入后由 `max_output_length` 属性暴露。

---

## 相关文档

- [集成层总览](../README.md)（ToolService 的定位）
- [工具模块接口文档](tools.md)（BaseTool / ToolResult / 内置工具）
- [外部工具热加载](external.md)（ExternalToolLoader：execute 惰性检查 / 生命周期钩子 / 编写约定）
- 子组件：[executor.md](executor.md) · [registry.md](registry.md) · [validator.md](validator.md) · [result_processor.md](result_processor.md) · [security.md](security.md) · [selector.md](selector.md) · [stats.md](stats.md)
- [ReAct 推理流程](../../domain_doc/reasoning_doc/react.md)（`ReActStrategy.execute_tool_calls` 并行执行，本模块上游调用方）
- [架构设计](../../project/architecture.md) · [配置说明](../../config_doc/config.md)
