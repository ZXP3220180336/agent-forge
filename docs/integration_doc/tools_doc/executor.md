# 执行调度器（ToolExecutor）说明文档

> **更新日期**：2026-08-17
> **模块**：`app/integration/tools/executor.py`
> **职责**：工具执行编排 —— 信号量 / 参数校验接入 / 超时 / 重试 / 结果截断 / 审计 / 统计 / 钩子 / per-tool 串行化
> **状态**：✅ 已实现
> **工业级对照**：对齐工业界「执行调度器」（网关统一分发、并发池 concurrency-safe/exclusive 屏障、超时重试）；本组件在信号量内完成全部执行期横切关注点

---

## 📋 目录

- [执行调度器（ToolExecutor）说明文档](#执行调度器toolexecutor说明文档)
  - [📋 目录](#-目录)
  - [设计目标](#设计目标)
  - [核心概念解释](#核心概念解释)
    - [工具级并发信号量](#工具级并发信号量)
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
2. **并发安全**：工具级信号量限制单任务并发；`concurrency_safe=False` 工具同实例内串行化
3. **可靠执行**：超时保护 + 渐进式指数退避重试，错误归一化后返回
4. **全路径审计**：成功 / 失败 / 未注册 / 校验失败 / 超时均留痕
5. **人工审批可插拔**：`requires_approval` 工具经 `ApprovalGate` 确认（默认放行），未来接真实审批仅换注入实现

## 核心概念解释

### 工具级并发信号量

`_tool_semaphore = asyncio.Semaphore(max_concurrent_tools)`（默认 3）限制**单任务内**最大并发工具调用数，保护 GPU / 服务器资源。`async with` 天然保证异常 / 取消时释放，不挂死占坑。

### per-tool 串行化锁

`concurrency_safe=False` 的工具（writeFile / code_exec）经 `_tool_lock(name)` 惰性创建的 `asyncio.Lock` 串行化——同工具同一实例内不会并发执行（防写覆盖 / 子进程交错）。锁仅同 executor 实例内生效；`unregister` 时 `prune_tool_lock` 清理。

### 重试与超时

`asyncio.wait_for(tool.execute(...), timeout)` 包裹每次尝试；重试循环 `range(max_retries)`（默认 3，含首次；**至少执行 1 次**——`max_retries=0` 视为「不重试跑一次」，clamp 到 1），失败后退避 `retry_delay * 2^attempt`（1s / 2s / 4s）。参数校验失败 / 未注册 / JSON 解析失败**不重试**（直接返回）。

**超时优先级（调用方显式 > 工具自声明 > 全局配置）**：`execute(timeout=...)` 显式传入最高优先；否则用工具声明的 `BaseTool.timeout`（如 code_exec 60s / readFile 5s）；两者均缺省时用全局 `tool_timeout`（默认 30s）。工具按自身耗时特征声明默认值，编排层可按需覆盖。

**错误码**：各失败路径返回结构化 `error_code`——未注册→`NOT_REGISTERED` / 参数 JSON 解析→`JSON_PARSE` / 校验→`VALIDATION` / 审批拒绝→`REJECTED` / 超时→`TIMEOUT` / 未捕获异常→`UNKNOWN`；工具业务失败透传其业务码（默认 `None`）。错误码与 `error` 中文归因并存：前者供审计聚合与证据链可审计性，后者供 LLM 修正（见 [tools.md](tools.md) `ErrorCode`）。

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
├── _tool_semaphore      → 工具级并发信号量
└── _tool_locks          → per-tool 串行化锁（concurrency_safe=False）
```

组件全部构造期注入（`ToolService` 装配，可自定义替换），信号量构造期创建（与事件循环绑定）。

## 执行流程

```text
execute(name, parameters, timeout, max_retries, retry_delay)
  async with _tool_semaphore                # 工具级并发信号量
    1. 查工具：未注册 → 审计（保留原始名，risk 兜底 L0）→ 返回 "工具 '...' 未注册"
    2. 解析执行参数：timeout = 调用方显式 or tool.timeout（自声明）or 全局 tool_timeout
       · max_retries = 调用方显式 or 全局 tool_max_retries
    3. 参数解析：str → json.loads（失败 → 审计 → 返回 "参数 JSON 解析失败: {e}"）
       · 结果必须为**字符串键 dict**——数组/标量/null（LLM 误输出）或非 str 键 dict → 审计 → 返回 JSON_PARSE（避免 `**parameters` 抛 TypeError 逃逸）
    4. jsonschema 校验：issues = tool.validation_issues(**parameters)
       · 非空 → 审计 → 返回 "参数验证失败: {归因列表}"        # 可归因，非 kwargs 转储
    5. 人工审批：if tool.requires_approval → await approval_gate.request(name, parameters)
       · 拒绝 → 审计 → 返回 "工具调用被拒绝：等待人工审批"（默认 AutoApprovalGate 放行）
    6. 执行（concurrency_safe=False 时 per-tool 锁串行化）：
       重试循环 for attempt in range(max_retries)：
       · asyncio.wait_for(tool.execute(**parameters), timeout)
       · 成功 → ResultProcessor.truncate_result(result, tool.max_output_length)
              → 填 execution_time / retry_count → 统计 → 钩子 → 返回
       · 返回失败 / 超时 / 异常 → 记 error（normalize_error）→ 统计
       · attempt < max_retries-1 → 退避 asyncio.sleep(retry_delay * 2^attempt)
    7. 审计最终结果（成功 / 失败均 1 条）→ 返回
```

**统计记录时机**：每次真实尝试（成功 / 失败 / 超时 / 异常）`stats.record` 一次；**钩子触发**：仅 `result.success` 时 `hooks.run`；**审计**：每次 execute 退出点 1 条最终结果（不做 per-attempt）。

## 对外接口

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `execute` | `async (name, parameters, timeout=None, max_retries=None, retry_delay=1.0) -> ToolResult` | 信号量内执行完整流程（唯一公共入口） |
| `prune_tool_lock` | `(name: str) -> None` | 注销工具时清理 per-tool 锁（由 ToolService.unregister 调用） |

`_execute_impl` / `_execute_with_retry` / `_tool_lock` / `_audit` 为私有实现，不对外暴露。

## 边界情况

1. **未注册工具**：审计保留原始工具名，风险级兜底 L0，返回 `"工具 '...' 未注册"`
2. **参数为 JSON 字符串**：解析失败返回失败结果而非抛异常
3. **per-tool 锁仅同实例内串行**：容器单例满足生产；测试 / 多实例场景锁不跨实例
4. **并发下统计**：同步字典更新（无锁），多任务并发时统计为尽力而为
5. **审计 / 钩子失败**：不影响工具执行结果（尽力而为）
6. **成功路径截断先于统计 / 钩子**：钩子看到的 `result.content` 为截断后内容
7. **审批拒绝**：`requires_approval` 工具被 gate 拒绝 → 返回 `"工具调用被拒绝：等待人工审批"`，工具不执行，审计 1 条

## 配置项清单

| 配置 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `max_concurrent_tools` | int | 3 | 工具级并发信号量（`agent_max_concurrent_tools` 注入） |
| `tool_timeout` | int | 30 | 单次执行超时（秒）；优先级：调用方显式 > 工具自声明 `timeout` > 本配置 |
| `tool_max_retries` | int | 3 | 最大执行次数（含首次） |

## 测试状态

`tests/unit/test_tool_executor_components.py`（8 用例）：校验失败归因 / 成功截断 / `concurrency_safe` 串行化与并行 / 审计各路径（成功 / 未注册 / 校验失败 / 工具失败）。既有 `test_tools.py`（信号量 / 基本执行 / 异常释放）+ `test_agent.py`（并行顺序）为回归护栏。

## 设计决策

- 执行期横切关注点收敛到 executor + per-tool 串行化 + 全路径审计 → [ADR](../../../adr/integration/tools/2026-08-17-six-component-alignment.md) · [ADR](../../../adr/integration/tools/2026-08-17-risk-levels-audit-no-enforcement.md)

## 相关文档

- [工具模块接口文档](tools.md)（ToolService.execute 入口）
- [validator.md](validator.md) · [result_processor.md](result_processor.md) · [security.md](security.md)（校验 / 截断 / 审计接入点）
- [registry.md](registry.md)（查工具依赖）· [stats.md](stats.md)（统计记录）· [tool_service.md](tool_service.md)（Facade 装配）
- [TOOLS-006 问题记录](../../../issues/integration/tools/2026-08-19-executor-json-non-dict.md)（参数 JSON 非 dict 校验）
- [TOOLS-007 问题记录](../../../issues/integration/tools/2026-08-19-executor-retry-count-semantics.md)（retry_count 口径统一）
- [TOOLS-008 问题记录](../../../issues/integration/tools/2026-08-19-executor-max-retries-zero.md)（max_retries=0 零执行）
