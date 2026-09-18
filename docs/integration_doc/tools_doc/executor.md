# 执行调度器（ToolExecutor）说明文档

> **更新日期**：2026-09-17
> **模块**：`app/integration/tools/executor.py`
> **职责**：工具执行编排 —— 共享准入 / 参数校验 / 超时 / 重试 / 结果截断 / 审计 / 统计 / 钩子 / per-tool 串行化
> 状态与验证见 [ALIGNMENT](../../ALIGNMENT.md)。
> **工业级对照**：对齐工业界「执行调度器」（网关统一分发、共享准入、concurrency-safe/exclusive 屏障、超时重试）；本组件在 Permit 内完成全部执行期横切关注点

---

## 📋 目录

- [执行调度器（ToolExecutor）说明文档](#执行调度器toolexecutor说明文档)
  - [📋 目录](#-目录)
  - [设计目标](#设计目标)
  - [核心概念解释](#核心概念解释)
    - [共享工具准入](#共享工具准入)
    - [per-tool 串行化锁](#per-tool-串行化锁)
    - [重试与超时](#重试与超时)
    - [组件接入点](#组件接入点)
  - [架构总览](#架构总览)
  - [执行流程](#执行流程)
  - [对外接口](#对外接口)
  - [边界情况](#边界情况)
  - [配置项清单](#配置项清单)
  - [测试状态](#测试状态)
  - [设计决策](#设计决策)
  - [相关文档](#相关文档)

---

## 设计目标

1. **执行期横切关注点单点收敛**：参数校验、超时、重试、结果截断、审计、统计、钩子、并发控制、人工审批全部由 executor 编排，工具本身只实现业务逻辑
2. **并发安全**：共享准入限制全局/单运行并发；`concurrency_safe=False` 工具同实例内串行化
3. **可靠执行**：超时保护；仅在适配器显式确认本次失败可安全重复且次数未耗尽时渐进式退避重试
4. **全路径审计**：成功 / 失败 / 未注册 / 校验失败 / 超时 / 审批拒绝 / 准入拒绝均留痕
5. **人工审批可插拔**：`requires_approval` 工具经 `ApprovalGate` 确认（默认放行），未来接真实审批仅换注入实现

## 核心概念解释

### 共享工具准入

`ToolAdmission` 同时限制所有运行的全局在途数和单运行在途数，并维护按运行轮转的有界等待队列。准入等待可被取消或 deadline 中断；队列满或准入预算耗尽返回 `CAPACITY_EXCEEDED`，不会进入工具重试。取得的 `ToolPermit` 在 Executor 的 `finally` 中幂等释放。

### per-tool 串行化锁

`concurrency_safe=False` 的工具（writeFile / code_exec）经 `_tool_lock(name)` 惰性创建的 `asyncio.Lock` 串行化——同工具同一实例内不会并发执行（防写覆盖 / 子进程交错）。锁仅同 executor 实例内生效；`unregister` 时 `prune_tool_lock` 清理。

### 重试与超时

`asyncio.wait_for(tool.execute(...), timeout)` 包裹每次尝试；重试循环上界为 `range(max_retries)`（`max_retries` 实为**最大执行次数**，含首次；`max_retries=0` clamp 为一次）。每个 attempt 建立独立身份并发布 RUNNING→终局事实。失败后只有 `BaseTool.can_retry(result_or_error)` 显式返回 True 且仍有次数时，才退避 `retry_delay * 2^attempt`。默认 `can_retry=False`，次数余额不会单独授权重复执行；参数校验失败、未注册、JSON 解析失败直接返回。

全局控制按取消→绝对 deadline→run_stop 检查。执行前命中时先保留 NOT_STARTED 事实再抛 shared 类型化异常；执行返回后先发布结果事实再复查，取消不会抹除已发生结果。事实发布顺序固定为 Integration 自有深副本→Domain sink；sink 编程错误会置位该 run 的 stop 信号并原样传播。

真实调用返回后即退出调用异常分类范围。成功结果的展示截断在副本上执行；截断失败保留原结果，不触发工具重放。统计、Hook、审计属于非关键观测，失败不覆盖结果；异步 Hook 与审计受单次观察预算约束，Hook 只能接收独立快照。

**全败归因**：重试耗尽后的最终失败归因 = **最近一次尝试**——业务失败记录其业务码；后续超时 / 异常会**覆盖**更早的业务失败（最终 `error_code` 反映真正的最后一次失败，避免把最终超时归因到早前业务错误）。

**超时优先级（调用方显式 > 工具自声明 > 全局配置）**：`execute(timeout=...)` 显式传入最高优先；否则用工具声明的 `BaseTool.timeout`（如 code_exec 60s / readFile 5s）；两者均缺省时用全局 `tool_timeout`（默认 30s）。工具按自身耗时特征声明默认值，编排层可按需覆盖。

**错误码**：各失败路径返回结构化 `error_code`（七码定义见 [tools.md](tools.md) `ErrorCode`）；工具业务失败透传其业务码（默认 `None`）。错误码与 `error` 中文归因并存：前者供审计聚合与证据链可审计性，后者供 LLM 修正。

### 组件接入点

- **校验**：`tool.validation_issues()`（jsonschema 全量归因），见 [validator.md](validator.md)
- **截断**：成功分支 `ResultProcessor.truncate_result(result, tool.max_output_length)`，见 [result_processor.md](result_processor.md)
- **审计**：每次 execute 退出点 1 条 `tool_call` 事件，见 [security.md](security.md)
- **审批**：`requires_approval=True` 工具经 `ApprovalGate.request(name, parameters)` 确认，默认 `AutoApprovalGate` 放行（见 [security.md](security.md)）

## 架构总览

```text
ToolExecutor（依赖注入，无 settings 直接依赖）
├── registry             → 查工具（未注册 → 失败返回）
├── validator            → jsonschema 参数校验（默认 ParameterValidator）
├── result_processor     → head+tail 截断（默认 ResultProcessor）
├── auditor              → 审计留痕（默认 ToolAuditor）
├── approval_gate        → 人工审批确认（默认 AutoApprovalGate 放行）
├── stats                → 统计记录（ToolStatsCollector）
├── hooks                → 成功通知（ExecutionHooks）
├── _admission           → 全局/单运行共享准入与有界排队
└── _tool_locks          → per-tool 串行化锁（concurrency_safe=False）
```

组件全部构造期注入（`ToolService` 装配，可自定义替换），准入器状态由所属事件循环维护。

## 执行流程

```text
execute(name, parameters, timeout, max_retries, retry_delay, *, call, facts)
  0. 保存 NOT_STARTED 快照；检查 cancel → deadline → run_stop
  1. ToolAdmission.acquire(call)            # 全局/单运行准入与可中断等待
     · 队列满/准入超时 → 审计 → CAPACITY_EXCEEDED，未执行且不重试
     · 取消/期限 → 抛类型化控制异常（不写审计，由上层终态承接）
  2. 查工具：未注册 → 审计（保留原始名，risk 兜底 L0）→ 返回 "工具 '...' 未注册"
  3. 解析执行参数：timeout = 调用方显式 or tool.timeout（自声明）or 全局 tool_timeout
       · max_retries = 调用方显式 or 全局 tool_max_retries
    4. 参数解析：str → json.loads（失败 → 审计 → 返回 "参数 JSON 解析失败: {e}"）
       · 结果必须为**字符串键 dict**——数组/标量/null（LLM 误输出）或非 str 键 dict → 审计 → 返回 JSON_PARSE（避免 `**parameters` 抛 TypeError 逃逸）
    5. jsonschema 校验：issues = tool.validation_issues(**parameters)
       · 非空 → 审计 → 返回 "参数验证失败: {归因列表}"        # 可归因，非 kwargs 转储
    6. 人工审批：if tool.requires_approval → await approval_gate.request(name, parameters)
       · 拒绝 → 审计 → 返回 "工具调用被拒绝：等待人工审批"（默认 AutoApprovalGate 放行）
    7. 执行（concurrency_safe=False 时 per-tool 锁串行化）：
       重试循环 for attempt in range(max_retries)：
       · 创建 attempt_id，发布 RUNNING
       · asyncio.wait_for(tool.execute(**parameters), timeout)
       · 成功 → 接管原结果并填 execution_time / retry_count
              → 在副本上截断 → 统计 → 返回处理后结果或原结果
       · 返回失败 / 超时 / 异常 → 记 error（normalize_error）→ 统计
       · can_retry=True 且 attempt 尚有余额 → 退避 asyncio.sleep(retry_delay * 2^attempt)
    8. 在剩余观察预算内先审计最终结果；成功时再通知快照 Hook
    9. 发布 operation 终局或 UNKNOWN/PENDING 事实；复查控制 → 返回或类型化终止
```

**统计记录时机**：每次真实尝试（成功 / 失败 / 超时 / 异常）`stats.record` 一次，准入拒绝不是真实尝试、不记统计；**钩子触发**：仅 `result.success` 时 `hooks.run`；**审计**：每次 execute 正常退出点 1 条最终结果（不做 per-attempt），含准入拒绝；cancel / deadline / run_stop 的类型化控制异常不是正常退出点，不写审计，由上层终态承接。

## 对外接口

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `execute` | `async (name, parameters, timeout=None, max_retries=None, retry_delay=1.0, *, call, facts) -> ToolResult` | 强制携运行上下文与事实入口，在共享准入 Permit 内执行完整流程 |
| `close` | `async () -> None` | 终止工具准入并撤回排队调用；由 `ToolService.shutdown` 调用，不可逆 |
| `check_abort` | `(call) -> None` | cancel→deadline→run_stop 同步复查；命中抛 shared 类型化异常 |
| `prune_tool_lock` | `(name: str) -> None` | 注销工具时清理 per-tool 锁（由 ToolService.unregister 调用） |

`_execute_impl` / `_execute_with_retry` / `_tool_lock` / `_audit` 为私有实现，不对外暴露。

## 边界情况

1. **未注册工具**：审计保留原始工具名，风险级兜底 L0，返回 `"工具 '...' 未注册"`
2. **参数为 JSON 字符串**：解析失败返回失败结果而非抛异常
3. **per-tool 锁仅同实例内串行**：容器单例满足生产；测试 / 多实例场景锁不跨实例
4. **并发下统计**：同步字典更新（无锁），多任务并发时统计为尽力而为
5. **统计 / 审计 / 钩子失败**：不影响工具执行结果；异步 Hook 和审计共享观察预算（审计优先），同步 Hook 必须非阻塞；吞取消回调的独立接管归 C-02 Piece ④
6. **局部 timeout**：attempt 事实为 UNKNOWN/PENDING，不能据 asyncio task 被取消推定底层线程结束；真实句柄与迟回事实接管归 Piece ④
7. **成功路径截断先于统计 / 钩子**：钩子看到的 `result.content` 为截断后内容
8. **审批拒绝**：`requires_approval` 工具被 gate 拒绝 → 返回 `"工具调用被拒绝：等待人工审批"`，工具不执行，审计 1 条
9. **准入拒绝**：队列满或准入等待耗尽 → 返回 `CAPACITY_EXCEEDED`，工具不执行、`NOT_STARTED` 事实、审计 1 条；审计耗时自 execute 入口起算，含排队等待，工具元数据取注册表实际声明（未注册时兜底 L0）

## 配置项清单

配置键的完整定义与默认值见 [配置参考](../../config_doc/config.md)；本节仅记录与本组件相关的行为。

| 配置 | 说明 |
| --- | --- |
| `tool_max_concurrent_executions` | 所有运行共享的全局在途上限 |
| `tool_max_concurrent_executions_per_run` | 单运行在途上限 |
| `tool_max_pending_calls` / `tool_max_pending_calls_per_run` | 全局/单运行等待队列上限 |
| `tool_admission_timeout_seconds` | 单次准入等待上限 |
| `tool_timeout` | 单次执行超时（秒）；优先级：调用方显式 > 工具自声明 `timeout` > 本配置 |
| `tool_max_retries` | 最大执行次数（含首次） |
| `tool_observation_timeout` | 每次调用的非关键异步 Hook/审计共享边界 |

## 测试状态

`tests/unit/test_tool_admission.py` 覆盖全局容量、单运行隔离、轮转公平、队列上限、取消、准入超时、撤回/关闭转换与「撤回与放行同刻不泄漏 Permit」竞态；`tests/unit/test_tool_executor_components.py` 覆盖参数/并发/审计/超时/计数与最近失败归因，并新增准入拒绝的 `CAPACITY_EXCEEDED` + 终局事实 + 审计、成功后处理失败不重放、默认禁止重试、显式安全重试、判断异常停止、Hook 超时和快照隔离、统计及审计失败不覆盖结果。既有 `test_tools.py`、`test_tool_hooks.py`、`test_tool_audit.py` 和 Agent 工具编排测试作为回归护栏；实际数量以测试收集结果为准。

## 设计决策

- 执行期横切关注点收敛到 executor + per-tool 串行化 + 全路径审计 → [ADR](../../../adr/integration/tools/2026-08-17-six-component-alignment.md) · [ADR](../../../adr/integration/tools/2026-08-17-risk-levels-audit-no-enforcement.md)

## 相关文档

- [工具模块接口文档](tools.md)（ToolService.execute 入口）
- [validator.md](validator.md) · [result_processor.md](result_processor.md) · [security.md](security.md)（校验 / 截断 / 审计接入点）
- [registry.md](registry.md)（查工具依赖）· [stats.md](stats.md)（统计记录）· [tool_service.md](tool_service.md)（Facade 装配）
- [TOOLS-006 问题记录](../../../issues/integration/tools/2026-08-19-executor-json-non-dict.md)（参数 JSON 非 dict 校验）
- [TOOLS-007 问题记录](../../../issues/integration/tools/2026-08-19-executor-retry-count-semantics.md)（retry_count 口径统一）
- [TOOLS-008 问题记录](../../../issues/integration/tools/2026-08-19-executor-max-retries-zero.md)（max_retries=0 零执行）
- [TOOLS-015 问题记录](../../../issues/integration/tools/2026-08-19-executor-to-thread-cancel.md)（to_thread 超时不可取消语义）
- [TOOLS-050 问题记录](../../../issues/integration/tools/2026-09-14-executor-postprocessing-retry.md)（调用后处理不重放 + 安全重试准入）
