# ReActStrategy 推理策略说明

> **模块**：`app/domain/reasoning/react.py`
> **更新日期**：2026-08-27
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

1. **完整循环算法**：`execute()` 承载 ReAct 主循环（LLM 推理 → finish_reason 分支 → 工具调用 / 正常结束 / 空输出重试 → 迭代兜底）
2. **可复用原语**：`execute_tool_calls()` 为工具并行执行原语（gather 保序），供 ReAct 循环自身以及 PlannerAgent 执行阶段 / ReflectionAgent 收集阶段复用
3. **结果载体**：`ReActOutcome` 承载策略产出，桥接方（`ReActAgent`）组装为 `AgentResult`

> **边界（依赖方向）**：本模块只依赖 ports + shared + 标准库，**不 import agent/**——策略收标量参数而非 `AgentContext`，可独立测试、可被任意编排复用。编排职责（生命周期 / 状态流转 / 结果组装）在 agent/ 层。

---

## 接口契约

### `ReActStrategy`

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `__init__` | `(llm: LLMGateway, tools: ToolGateway)` | 注入端口依赖 |
| `execute` | `(user_input, messages, *, max_iterations, temperature, max_tokens) -> AsyncGenerator[str]` | ReAct 主循环；yield SSE 事件，结果写入 `outcome` |
| `execute_tool_calls` | `(tool_calls, messages, iteration) -> AsyncGenerator[str]` | 工具并行执行原语（gather 保序 + 事件产出 + 记录） |

**实例属性**：`outcome: ReActOutcome | None`（`execute()` 结束后读取）。

### `ReActOutcome`（结果载体）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `content` | `str` | 最终回答 |
| `reasoning` | `str` | 完整推理过程（累计） |
| `tool_calls` | `list[dict]` | 工具调用记录（tool/params/result/success/duration） |
| `iterations` | `int` | 实际轮数 |
| `total_tokens` / `usage` | `int` / `dict \| None` | Token 统计 |
| `error` | `str \| None` | 失败原因（LLM 调用失败 / 无结果） |
| `success` | `bool` | 是否成功（content 非空） |

### 主循环关键语义（示意，完整实现见源码）

```text
第 N 轮推理开始
  ├─ 1. LLM 推理（async_generate 流式，yield reasoning/message 事件）
  ├─ 2. stream_result.error 非空？ → 短路返回失败 outcome（不空转重试，LLM-001）
  ├─ 3. 追加 assistant 消息（含 tool_calls 配对，防 400）
  ├─ 4. finish_reason 分支：
  │     ├─ "tool_calls" → execute_tool_calls() 并行执行 → 追加 tool 消息 → continue
  │     ├─ "stop" / "length" / 有内容 → 正常结束（outcome.success = content 非空）
  │     └─ 空输出 → yield 重试信息 → 下一轮
  └─ 5. 达到 max_iterations → 用 last_result 兜底，强制结束
```

---

## 行为边界

| 场景 | 处理 |
| --- | --- |
| LLM 调用失败（`StreamResult.error`） | 短路返回 `success=False` + `error`，不重试 |
| 空输出（finish_reason 空 + content 空） | 重试下一轮 |
| 达到 `max_iterations` | 用最后结果兜底，强制结束；无结果则 `error="LLM 未返回任何结果"` |
| 工具参数 JSON 解析失败 | 按空参数 `{}` 执行（`json.loads` 异常保护） |
| 未知工具 | 由 ToolGateway 返回失败 ToolResult，不抛出 |

---

## 使用示例

```python
from app.domain.reasoning import ReActStrategy

strategy = ReActStrategy(llm=llm_service, tools=tool_service)
messages = [{"role": "user", "content": "30C 转华氏"}]
async for event in strategy.execute(
    "30C 转华氏", messages,
    max_iterations=3, temperature=0.2, max_tokens=1024,
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
- **决策记录**：[ADR react-strategy-extraction](../../../adr/domain/agent/2026-08-27-react-strategy-extraction.md)

---

## 测试

`tests/unit/test_react_strategy.py`（6 用例）：

- 工具循环 stop 结束（outcome 正确组装：content / iterations / tool_calls 记录 / tool 消息回喂）
- LLM 失败短路（LLM-001，iterations=1）
- 空输出重试后正常结束
- 持续空输出 → 迭代兜底（success=False）
- `execute_tool_calls` 并行保序（工具延迟交错，结果顺序 = 输入顺序）
- `execute_tool_calls` 实际并发（总耗时 < 串行和）

---

## 相关文档

- [推理策略模块](reasoning.md)（主文档）
- [ReActAgent 桥接组件](../agent_doc/executor.md)（同级组件）
- [Agent 模块对外接口文档](../agent_doc/agent.md)
- [领域层说明](../README.md)
- [ADR react-strategy-extraction](../../../adr/domain/agent/2026-08-27-react-strategy-extraction.md)
