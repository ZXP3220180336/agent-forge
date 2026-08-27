# ReAct 策略总时间上限（max_execution_time）

> 日期：2026-08-27 ｜ 层级：domain/reasoning + domain/agent

## Context

- 对标文档 [react_benchmark.md](../../../docs/domain_doc/reasoning_doc/react_benchmark.md) 确认核心必备项「时间上限」缺失：整个 ReAct 循环无总时长护栏（单工具超时由 ToolService 兜底，但多轮 LLM + 工具可无限拉长）。
- `settings.agent_timeout = 300` 已存在但从未被注入使用——正好作为配置来源。
- 工业级参照（LangChain `max_execution_time`）语义为「循环总 wall-clock 上限」，非单轮预算。

## Decision

1. **`ReActStrategy.execute()` 新增 `max_execution_time: float | None = None` 标量参数**（None=不设限，向后兼容）。
2. **实现**：`async with asyncio.timeout(max_execution_time)` 包整个 for 循环（`asyncio.timeout(None)` 即不设限，无需 nullcontext 分支）；超时对齐 `max_iterations` 兜底模式降级（用 last_result 组装 outcome，`error` 记录超时原因），并判别「真超时 vs 生成器被 finalizer 关闭」（慢消费者场景，`asyncio.current_task()` 与进入 timeout 时是否同一 task）避免 `RuntimeError: async generator ignored GeneratorExit`。
3. **注入链**：`container.agent_params` 加 `max_execution_time: settings.agent_timeout` → chat.py 构造 AgentContext 传入 → executor.py 透传给 execute。**不进 request schema**——定位为全局资源护栏（与 temperature / max_tokens 同模式），不暴露逐请求覆盖。

## Consequences

- ✅ 生产默认 300s 总时间护栏生效（原本无总时长限制）；单轮更短约束由 `llm_timeout=60` / `tool_timeout=30` 兜底，互不冲突。
- ✅ 超时优雅降级：有 content 算部分成功，`error` 记录超时；已完成轮次的工具调用记录保留（部分进度证据）。
- ✅ 新增 4 单元测试（首轮超时 / 中途超时保留进度 / 宽松上限不影响 / None 不触发）。
- ⚠️ 行为变更：`agent_timeout` 从「从未使用」变为全局生效。
- 📌 边界：deadline 恰好落在生成器产出 SSE 事件后、消费者拉取前时，取消落消费者侧、无降级 done 事件——流式生成器 + timeout 的结构性边界，实际窗口毫秒级。
