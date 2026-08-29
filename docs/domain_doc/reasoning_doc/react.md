# ReActStrategy 推理策略说明

> **模块**：`app/domain/reasoning/react.py`
> **更新日期**：2026-08-28
> **职责**：ReAct 原子推理策略——推理 ↔ 工具调用的完整循环算法
> **状态**：✅ 已实现
> **配套**：桥接见 [executor.md](../agent_doc/executor.md)（`ReActAgent`）

---

## 📋 目录

- [ReActStrategy 推理策略说明](#reactstrategy-推理策略说明)
  - [📋 目录](#-目录)
  - [定位与职责](#定位与职责)
  - [接口契约](#接口契约)
    - [`ReActStrategy`](#reactstrategy)
    - [`ReActOutcome`（结果载体）](#reactoutcome结果载体)
    - [主循环关键语义（示意，完整实现见源码）](#主循环关键语义示意完整实现见源码)
  - [行为边界](#行为边界)
  - [使用示例](#使用示例)
  - [设计决策](#设计决策)
  - [测试](#测试)
  - [相关文档](#相关文档)

---

## 定位与职责

`ReActStrategy` 是领域层策略库的 **ReAct 原子推理算法**（推理 → 行动 → 观察循环）：

1. **完整循环算法**：`execute()` 承载 ReAct 主循环（LLM 推理 → finish_reason 分支 → 工具调用 / 正常结束 / 空输出重试 → 迭代兜底），各终止/错误分支经错误处理分发（ErrorHandlerRegistry）决策；方法拆分为职责单一的分支方法（`_finalize_*` / `_handle_*`）
2. **可复用原语**：`execute_tool_calls()` 为工具并行执行原语（gather 保序），供 ReAct 循环自身以及 PlannerAgent 执行阶段 / ReflectionAgent 收集阶段复用
3. **结果载体**：`ReActOutcome` 承载策略产出，桥接方（`ReActAgent`）组装为 `AgentResult`

> **边界（依赖方向）**：本模块只依赖 ports + shared + 标准库，**不 import agent/**——策略收标量参数而非 `AgentContext`，可独立测试、可被任意编排复用。编排职责（生命周期 / 状态流转 / 结果组装）在 agent/ 层。

---

## 接口契约

### `ReActStrategy`

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `__init__` | `(llm, tools, context_budget=None, error_handlers=None)` | 注入端口依赖 + 横切能力（ContextBudgetPort / ErrorHandlerRegistry） |
| `execute` | `(user_input, messages, *, max_iterations, temperature, max_tokens, max_execution_time=None, max_context_rounds=None, max_context_tokens=None, output_schema=None) -> AsyncGenerator[str]` | ReAct 主循环；yield SSE 事件，结果写入 `outcome` |
| `execute_tool_calls` | `(tool_calls, messages, iteration) -> AsyncGenerator[str]` | 工具并行执行原语（gather 保序 + 事件产出 + 记录） |

**实例属性**：`outcome: ReActOutcome | None`（`execute()` 结束后读取）。

### `ReActOutcome`（结果载体）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `content` | `str` | 最终回答 |
| `reasoning` | `str` | 完整推理过程（累计） |
| `structured` | `dict \| None` | 结构化最终答案（final_answer 工具产出，output_schema 启用时） |
| `tool_calls` | `list[dict]` | 工具调用记录（tool/params/result/success/error/error_code/duration） |
| `iterations` | `int` | 实际轮数 |
| `total_tokens` / `usage` | `int` / `dict \| None` | Token 统计 |
| `error` | `str \| None` | 失败原因（LLM 调用失败 / 无结果） |
| `success` | `bool` | 是否成功（content 非空） |

### 主循环关键语义（示意，完整实现见源码）

```text
第 N 轮推理开始
  ├─ 1. LLM 推理（async_generate 流式，yield reasoning/message 事件）
  ├─ 2. stream_result.error 非空？ → 错误分发（默认短路失败；handler 可重试/上抛）
  ├─ 3. 追加 assistant 消息（含 tool_calls 配对，防 400）
  ├─ 4. finish_reason 分支：
  │     ├─ "tool_calls" → final_answer 检测（结构化终止 / 回喂继续）→ execute_tool_calls()
  │     │                  → 可恢复错误分发（默认回喂继续）→ 上下文预算裁剪 → 下一轮
  │     ├─ "stop" / "length" / 有内容 → 正常结束（outcome.success = content 非空）
  │     └─ 空输出 → 错误分发（默认重试）
  ├─ 5. 达到 max_iterations → 错误分发（默认用 last_result 兜底）
  └─ 6. 达到 max_execution_time → 错误分发（默认降级，error 记录超时）
```

---

## 行为边界

| 场景 | 处理 |
| --- | --- |
| LLM 调用失败（`StreamResult.error`） | 短路返回 `success=False` + `error`，不重试 |
| 空输出（finish_reason 空 + content 空） | 重试下一轮 |
| 达到 `max_iterations` | 用最后结果兜底，强制结束；无结果则 `error="LLM 未返回任何结果"` |
| 达到 `max_execution_time`（None=不设限） | 用 last_result 兜底，`error` 记录超时原因；有 content 算部分成功 |
| 工具参数 JSON 解析失败 | 不执行工具：构造失败 ToolResult（JSON_PARSE）回喂模型自纠，`error`/`error_code` 进证据链 |
| 工具执行失败 / 无效工具名 | 回喂 `str(result)`（"错误: <error>"，无效工具含「未注册」），模型可感知失败原因自愈；`error` / `error_code` 进证据链记录 |
| reasoning_content 回喂 | DeepSeek V4 thinking + tools 必须回喂（否则 400）；`has_reasoning` 覆盖空 reasoning 场景（空串也回喂） |
| 上下文预算（`max_context_rounds` / `max_context_tokens`，None=不裁剪） | 模型下次调用前经注入的 ContextBudgetPort（context_manager 实现）裁剪：保留最近 N 轮 assistant/tool 配对 + token 硬上限 |
| 结构化最终答案（`output_schema`，None=不启用） | 注入 final_answer 工具；模型调用即终止并产出 `outcome.structured`；参数校验失败回喂（VALIDATION）自纠 |
| 错误处理分发（注入 `ErrorHandlerRegistry`，None=默认行为） | 各终结/可恢复错误按 kind 分发（CONTINUE/STOP/RAISE）；默认 = 现有行为，调用方按 kind 注册覆盖 |

---

## 使用示例

```python
from app.domain.reasoning import ReActStrategy

strategy = ReActStrategy(llm=llm_service, tools=tool_service)
messages = [{"role": "user", "content": "30C 转华氏"}]
async for event in strategy.execute(
    "30C 转华氏", messages,
    max_iterations=3, temperature=0.2, max_tokens=1024, max_execution_time=30.0,
):
    print(event, end="")
# strategy.outcome → ReActOutcome
```

独立调用工具原语（供编排复用）：

```python
async for event in strategy.execute_tool_calls(tool_calls, messages, iteration=1):
    pass
# tool 消息已追加到 messages
```

---

## 设计决策

- **为什么收标量参数（非 AgentContext）**：遵守依赖方向 `reasoning → ports + shared`（不 import `agent/`），策略可独立测试、被任意编排复用
- **为什么 `execute_tool_calls` 独立成原语**：PlannerAgent 执行阶段（程序执行工具）与 ReflectionAgent 收集阶段需复用工具循环，与「完整 ReAct 循环」解耦
- **为什么默认 `model_key` 走 "main"**：ReAct 主循环是 Agent 的主推理路径，保持与 `async_generate` 默认一致（structured 阶段走 "fast" 是后续策略的事）
- **为什么用 `asyncio.timeout` 包整个循环**：语义是「循环总时长上限」（对齐 LangChain `max_execution_time`），而非单轮预算；`asyncio.timeout(None)` 即不设限，无需 nullcontext 分支。超时对齐 `max_iterations` 兜底模式降级（用 last_result，`error` 记录超时），并判别「真超时 vs 生成器被 finalizer 关闭」（慢消费者场景）避免 `RuntimeError: async generator ignored GeneratorExit`
- **为什么上下文预算归 context_manager（端口注入）**：预算是横切能力（所有 Agent 模式共享），由 context_manager 统一实现（复用 TokenCounter + 上下文职责），经 `ContextBudgetPort` 注入——ReAct 不实现算法；选 trimming（轮次 + token 双层）而非摘要——摘要压缩工具原始记录会破坏证据链（产品核心）
- **为什么结构化用 Final Answer 工具而非事后提取**：模型原生结构化（从开始按 schema 组织证据链、无信息损失、无额外 LLM 调用），兼作循环终止机制；严格 `output_type` 会抑制中间工具调用；`generate_structured` 留作非 Agent 提取场景（分工见 ADR structured-output）
- **为什么错误处理走 ErrorHandlerRegistry（共享内核）**：错误处理是领域层横切能力（所有 Agent 模式共享），按 kind 注册 handler（CONTINUE/STOP/RAISE）——可恢复默认回喂、终结性默认终止；默认行为 = 现有逻辑，调用方可按 kind 覆盖（见 ADR agent-error-handling）
- **为什么 execute 拆分为职责单一的分支方法**：主循环仅保留骨架（LLM 推理 → 分发点），各终止/错误分支提取为私有方法（`_finalize_*` / `_handle_*`），以 `outcome is not None` 作终止信号——降低方法体耦合、提升可维护性，行为不变（回归护栏）
- **决策记录**：[ADR react-strategy-extraction](../../../adr/domain/agent/2026-08-27-react-strategy-extraction.md)

---

## 测试

`tests/unit/test_react_strategy.py`（27 用例）：

- 工具循环 stop 结束（outcome 正确组装：content / iterations / tool_calls 记录 / tool 消息回喂）
- LLM 失败短路（LLM-001，iterations=1）
- 空输出重试后正常结束
- 持续空输出 → 迭代兜底（success=False）
- `execute_tool_calls` 并行保序（工具延迟交错，结果顺序 = 输入顺序）
- `execute_tool_calls` 实际并发（总耗时 < 串行和）
- 时间上限：首轮 LLM 超时降级（success=False + error 含超时）
- 时间上限：中途超时保留部分进度（已完成工具调用记录保留）
- 时间上限：宽松上限不影响正常完成
- 时间上限：`max_execution_time=None` 显式不设限
- 工具失败：错误文本回喂模型（tool 消息含失败原因）
- 工具失败：证据链记录 error / error_code
- 无效工具名：回喂「未注册」+ 证据链 NOT_REGISTERED
- 解析失败：不执行工具 + 回喂解析失败 + 证据链 JSON_PARSE
- 截断标记：长结果带 `[结果已截断]`（含标记不超限）
- 截断标记：短结果无标记
- reasoning 回喂：`has_reasoning` 时回喂空串（DeepSeek V4 必须回喂）
- reasoning 回喂：无信号不回喂（chat 模型）
- 上下文预算：注入 ContextBudgetPort 后轮次裁剪生效（assistant/tool 配对保留）
- final_answer：成功提取终止（outcome.structured）
- final_answer：校验失败回喂（VALIDATION）
- final_answer：未配置 output_schema 不注入
- 错误处理：LLM_FAILED handler → CONTINUE 重试下轮成功
- 错误处理：LLM_FAILED handler → RAISE 抛 AgentRunError
- 错误处理：EMPTY_OUTPUT handler → STOP 终止
- 错误处理：TOOL_FAILED handler → STOP 终止（部分进度保留）
- 错误处理：STRUCTURED_INVALID handler → STOP 终止

---

## 相关文档

- [推理策略模块](reasoning.md)（主文档）
- [ReActAgent 桥接组件](../agent_doc/executor.md)（同级组件）
- [Agent 模块对外接口文档](../agent_doc/agent.md)
- [领域层说明](../README.md)
- [ADR react-strategy-extraction](../../../adr/domain/agent/2026-08-27-react-strategy-extraction.md)
