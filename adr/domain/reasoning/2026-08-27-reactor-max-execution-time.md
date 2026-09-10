# ReAct 策略总时间上限（max_execution_time）

> 日期：2026-08-27 ｜ 层级：domain/reasoning + domain/agent

## Context

- 对标文档 [react_benchmark.md](../../../docs/domain_doc/reasoning_doc/react_benchmark.md) 确认核心必备项「时间上限」缺失：整个 ReAct 循环无总时长护栏（单工具超时由 ToolService 兜底，但多轮 LLM + 工具可无限拉长）。
- `settings.agent_timeout = 300` 已存在但从未被注入使用——正好作为配置来源。
- 工业级参照（LangChain `max_execution_time`）语义为「循环总 wall-clock 上限」，非单轮预算。
- Python `asyncio` 官方契约明确：`timeout` 会取消当前 task，并在上下文外转为 `TimeoutError`；取消协程应以 `try/finally` 可靠清理，`timeout_at` 接受事件循环的绝对时钟。该机制能在到期时请求取消，不能保证同步阻塞或吞取消代码在同一时刻返回；内部主动终止与外层 task 取消仍需错开（[Coroutines and Tasks](https://docs.python.org/3/library/asyncio-task.html)）。

## Decision

1. **`ReActStrategy.execute()` 新增 `max_execution_time: float | None = None` 标量参数**（None=不设限，向后兼容）。
2. **实现**：用 `asyncio.timeout_at()` 包业务 for 循环，`max_execution_time` 是该循环的超时取消触发点。传给 LLM 网关的内部绝对 deadline 提前 `min(1s, 总时长 × 10%)`，供集成层关闭流、结算预留和记录终止状态。不识别内部 deadline、但配合 task 取消的调用会在外层 timeout 到期时停止。
3. **部分成果**：每轮 LLM 生成开始时即绑定 `current_result`。类型化 deadline 或硬超时终止时，当前轮已有 content、reasoning 或 tool calls 则优先用当前轮组装 outcome，否则回退到上一完成轮。异常携带的 usage 优先于 `current_result.usage`，只归账一次。
4. **收尾语义**：超时对齐 `max_iterations` 兜底模式降级，`error` 记录超时原因；同时判别「真超时 vs 生成器被 finalizer 关闭」（慢消费者场景，`asyncio.current_task()` 与进入 timeout 时是否同一 task），避免 `RuntimeError: async generator ignored GeneratorExit`。`_finalize_timeout`、错误 handler 与 done 事件生成位于 timeout scope 外，是不再发起 LLM/工具副作用的领域尾部。
5. **注入链**：`container.agent_params` 加 `max_execution_time: settings.agent_timeout` → chat.py 构造 AgentContext 传入 → executor.py 透传给 execute。**不进 request schema**——定位为全局资源护栏（与 temperature / max_tokens 同模式），不暴露逐请求覆盖。

## Consequences

- ✅ 生产默认 300s 业务循环超时护栏生效（原本无总时长限制）；单轮更短约束由 `llm` 分级超时（connect/read 等，见 LLM-ADR-014）` / `tool_timeout=30` 兜底，互不冲突。
- ✅ 超时优雅降级：保留最新可见部分成果、已完成轮次的工具调用记录和可得 usage，`error` 记录超时。
- ✅ 内部终止与外层硬超时不再同刻竞争，正常终止路径获得有界资源清理时间。
- ⚠️ 行为变更：`agent_timeout` 从「从未使用」变为全局生效。
- ⚠️ LLM 的可用业务时间比 timeout 触发点少最多 1 秒；该取舍换取 close/settle 优先完成的窗口。
- 📌 清理窗口是尽力而为；清理超过窗口时，外层 timeout 会取消当前 task。Integration 的 `finally` 继续接管未终态资源。
- 📌 `max_execution_time` 不承诺整个 async generator 在该秒数内绝对返回：同步阻塞、吞取消扩展点，以及 timeout scope 外的领域结果组装都可能形成尾部延迟。该尾部不得再发起 LLM、工具、整流、续接或 fallback 请求。
- 📌 边界：deadline 恰好落在生成器产出 SSE 事件后、消费者拉取前时，取消落消费者侧、无降级 done 事件——流式生成器 + timeout 的结构性边界，实际窗口毫秒级。
