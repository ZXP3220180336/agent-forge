# 请求构建期校验取舍：不建集中校验层，超长上下文裁剪显式告警

> **状态**：✅ 已采纳（2026-09-02）
> **决策日期**：2026-09-02
> **涉及层级**：跨层（application「上下文管理」+ api「chat 路由」）——根目录横切 ADR（本仓库首个，配套 `adr/README.md` 根级索引，见文末）
> **关联文档**：[context.md](../docs/application_doc/context_doc/context.md) · [chat.py](../app/api/routes/chat.py) · [context_manager.py](../app/application/context/context_manager.py)

---

## Context

以生产级 Agent 流式框架的「请求构建期校验」要求为参照审计本项目后，两个待决策点：

1. **是否新增构建期集中强参数校验层**（本地拦截模型名/消息结构/Token 长度，不靠服务端 400）；
2. **超长上下文被静默裁剪**是否需显式化——用户粘贴大量数据时，历史消息被悄悄丢弃且无感知。

审计事实：

- 请求构建输入**全部来自受信内部组装**：prompt 来自模板、历史来自会话 DB、tool_defs 来自工具注册表；用户唯一可控的 `request.message` 已有 Pydantic 类型校验挡在 API 层。
- 未拦截的坏请求已被下游兜住：`classify_error` 把 4xx 判 NON_RETRYABLE（不空转重试）、structured 对 `response_format` 400 有特判降级、非流式归一 `LLMAPIError`。
- 超长上下文在 `ContextManager.build_messages` 被 **静默** 裁剪（丢最早历史），`context_manager.py` 无任何日志；ContextManager 为**进程级共享单例**，不能依赖实例状态传请求级信息。

备选方案：

- **A 拒绝式校验**：超长直接返回 422「消息超长」。语义干净，但对「工程师粘贴大批数据」场景不友好（拒绝即阻断）。
- **B 裁剪 + 显式告警**：保留裁剪降级，同时日志 + SSE `agent_info` 事件告知「已裁剪 N 条历史」。
- **C 维持静默**：现状，裁剪无感知。

---

## Decision

1. **不建「构建期集中强参数校验层」**（Option 0）。理由：本仓库请求构建无跨信任边界输入（受信组装 + 入口 Pydantic），集中校验等于把「已兜住的错误」换处重抛，纯重复；生产框架的强校验是多租户 SaaS 面向不可信输入的必要防线，非本单用户/引擎场景所需。
2. **超长上下文裁剪走 Option B：裁剪降级保留 + 显式告警**。用户可见告警（SSE `agent_info` 事件，流首产出）+ 运维可查（WARNING 日志含 session_id / 丢弃条数 / 预算 / 裁剪后 token）。
3. **告警信号经返回值携带，不依赖单例状态**。`ContextManager.build_messages` 返回契约由 `(messages, total_tokens)` 扩为 `(messages, total_tokens, truncated_history: int)`（丢弃的历史消息条数，0=未裁剪）——ContextManager 是共享单例，请求级截断信息只能走返回值，杜绝并发串扰。
4. **不采纳 Option A（拒绝式）**：粘贴数据的工程师场景下拒绝即阻断体验，裁剪 + 明示更符合产品主链路（多轮历史可丢、当前输入不可拒）；单条 user 消息本身超窗的极端残留由服务端 400 NON_RETRYABLE 兜底（见 Consequences）。

---

## Consequences

- ✅ 超长上下文裁剪从「静默」变「可感知」：客户端流首收到 `agent_info`「上下文超限，已裁剪最早 N 条历史」；运维日志含 session/条数/预算，可定位丢历史导致的回答偏差。
- ✅ 不引入集中校验层 → 无重复劳动、无新故障面；代码面最小（context 返回 + 路由 yield + 日志）。
- ⚠️ 返回契约微调：`build_messages` 调用方解包从二元改三元（生产 1 处 + 测试 2 处），`total_tokens` 在 chat 路由仍弃用。
- ⚠️ 残留边界：历史全丢后**单条超长 user 消息仍可能超出窗口**——本决策不解决（拒绝或单条级分段属后续问题），暂由服务端 400 NON_RETRYABLE 兜底（错误路径不雪崩）。
- 📌 升级路径（按产品导向暂不实现）：集中强参数校验层；拒绝式校验（Option A）；上下文摘要压缩（`build_messages` docstring 策略第 4 条尚未实现，仍为硬丢）；`request_id`/TraceID 贯穿等其余生产级异常链差距（各自独立决策，见流式异常链审计）——其中分级超时（[LLM-ADR-014](integration/llm/2026-09-03-connection-establishment-phase.md)）与半流续接（[LLM-ADR-015](integration/llm/2026-09-03-mid-stream-continuation.md)）已独立立项落地。
- 📌 本 ADR 置于 `adr/` 根目录并自建 `adr/README.md` 索引——建立「根级横切 ADR」新约定（原约定为跨层决策归主决策模块子目录；根级目录专收不归属单一模块的横切决策，并强制登记索引防失联）。
