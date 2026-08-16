# LLM-001 流式 create 阶段失败被 ReActAgent 静默吞掉

> **状态**：✅ 已修复（2026-08-16）
> **优先级**：P0（合并前必修）
> **来源**：2026-08-16 Integration 层 LLM 模块工业级审核（重要项 1）
> **涉及模块**：`app/integration/llm/streaming_rectifier.py`（主因）· `app/domain/ports/llm_gateway.py`（契约）· `app/domain/agent/executor.py`（消费方）
> **关联文档**：[llm.md](../../../docs/integration_doc/llm_doc/llm.md) · [streaming_rectifier.md](../../../docs/integration_doc/llm_doc/streaming_rectifier.md) · [domain_doc/README.md](../../../docs/domain_doc/README.md)

---

## 问题描述

### 现象

流式调用中 create 阶段抛出的异常（4xx / 认证失败 / 熔断开启等不可恢复错误）被 `StreamingRectifier.rectified_stream` 转成 SSE error 事件后 `return`；而 `ReActAgent` 消费事件流时只透传事件、不解析事件类型。最终 `StreamResult` 是空的（`content=""`、`finish_reason=None`），被 Agent 当成「LLM 未生成有效输出」，空转重试直至 `max_iterations`。

### 影响

1. **功能正确性**：不可恢复错误被静默吞掉，ReAct 空转最多 `max_iterations` 轮，最终错误信息不准确（返回「LLM 未返回任何结果」而非真实的 401 / 熔断原因）。
2. **成本**：空转的每一轮都真实调用 LLM（含计费），故障时成本被放大。
3. **可观测性**：真实失败原因丢失，排障困难。

### 根因

错误信号在 LLM 层产出（SSE error 事件）后**没有被 Agent 编排层消费**——事件流接口的「失败」与「成功但空」在消费端不可区分，编排层把前者当后者处理。

---

## 工业级参照（为什么这是真问题）

| 框架 | 对应做法 | 参照点 |
| --- | --- | --- |
| **OpenAI Agents SDK** | `RunResultStreaming.run_loop_exception` 属性，流结束后调用方显式 `if result.run_loop_exception: raise` | PR [#2931](https://github.com/openai/openai-agents-python/pull/2931) 修复的正是「run loop 早期异常被静默吞掉、调用方拿到 clean-looking 但 broken 的 result」——与本问题同构 |
| **LangGraph** | 空响应必须显式计数为失败，不重置错误计数器，重试耗尽后浮出底层错误 | [官方文档](https://docs.langchain.com/oss/python/langgraph/streaming.md) 记录的坑：provider 返回 200 但空响应被当「成功」→ 静默重试到 recursion limit 才暴露真实错误 |
| **Vercel AI SDK** | SSE 数据流协议把 `{"type":"error","errorText":"..."}` 作为一等事件类型，客户端按 type 分发 | [协议](https://github.com/bjelkenhed/ai-elements/blob/main/AI_SDK_SSE_FORMAT.md) 定义完整生命周期（start→delta→finish→[DONE]） |
| **Anthropic Agent SDK** | `AssistantMessage.error` 携带类型化错误；`TaskNotificationMessage.status` 显式标记 completed/failed/stopped | [流式输出文档](https://code.claude.com/docs/en/agent-sdk/streaming-output) |

**共性模式**：Result 对象携带显式错误信号 + 编排层显式消费失败（不静默重试）+ SSE error 事件为一等公民。

---

## 修复方案（含决策取舍）

**决策**：给 `StreamResult` 增加 `error` 字段（失败标记）+ `ReActAgent` 检查短路；**保留**现有 SSE error 事件（前端兼容）。而非「NON_RETRYABLE 向上抛」。

**取舍理由**：

1. async_generate 是事件流接口，抛异常会中断事件流，且被 `BaseAgent.run()` 的 `except Exception` 兜成「Agent 运行异常」，丢失 LLM 失败的具体语义；
2. `StreamResult` 已是「单轮 LLM 结果载体」，加 `error` 字段与 OpenAI `run_loop_exception` / Anthropic `AssistantMessage.error` 直接同构，语义自然、向后兼容（默认 `None`）；
3. SSE error 事件继续产出，前端/客户端可感知，双通道各司其职。

**语义边界**（保证不误伤现有行为）：

- 正常空回（`finish_reason="stop"` 且 content 空）→ `error=None` → 保持现有「空输出重试」逻辑，**不被误判为失败**；
- 整流成功路径（中断后重试成功）→ `error=None`（只有最终放弃才 mark）；
- `StreamResult.error` 默认 `None`，现有所有调用方零影响。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/domain/ports/llm_gateway.py` | `StreamResult` 增加 `error: str \| None = None`（领域契约） | — |
| `app/integration/llm/streaming_rectifier.py` | 三个失败出口 mark `context.result.error`（create 异常 / 迭代放弃 / 用户取消），保留 SSE error 事件 | `test_stream_rectify.py` 四出口补 `result.error` 断言 |
| `app/domain/agent/executor.py` | `async for` 消费后检查 `stream_result.error`，非空则短路返回失败 `AgentResult`（`success=False`，content 保留已产出部分） | `test_agent.py` 新增 `test_strategy_cycle_short_circuits_on_llm_error` |
| 文档 | [llm.md](../../../docs/integration_doc/llm_doc/llm.md)（已实现列表）/ [streaming_rectifier.md](../../../docs/integration_doc/llm_doc/streaming_rectifier.md)（失败信号透传小节）/ [domain_doc/README.md](../../../docs/domain_doc/README.md)（StreamResult.error 契约） | — |

---

## 验证

- 相关测试 38 用例通过（test_stream_rectify / test_agent / test_streaming_rectifier / test_llm_service）
- 全量测试 **350 passed**（44.71s），无回归
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **编排层必须显式消费 LLM 失败信号**：事件流接口若把失败渲染成普通事件，消费端无法区分「失败」与「成功但空」，必然导致空转重试（参照 LangGraph 空响应坑）。失败信号应同时落在两处——Result 对象（供编排层决策）与 SSE error 事件（供前端感知）。
- **契约变更要有默认值兜底**：领域对象加字段用 `None` 默认，保证存量调用方零影响，再显式升级消费方。
- **文档同步规范（本次确立，后续问题沿用）**：每个审查问题登记到 `docs/issues/llm/`（README 索引 + 单问题文件）；修复时同步对应模块文档；教训只沉淀在本文件内，**不同步 `docs/lessons.md`**。
