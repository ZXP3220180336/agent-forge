# 工具调用协议修正缺少独立重试上限（REASON-017）

> **状态**：✅ 已修复 ｜ **优先级**：P1 ｜ **来源**：ReAct / Reflection / Planner 重试闭环审计 ｜ **涉及模块**：`app/domain/reasoning/react.py`、`reflection.py`、`planner.py`、`app/domain/agent/`、`app/api/`、`app/config/settings.py`

## 问题描述

### 现象

三个策略的外层循环均有总上限，Reflection 修正和 Planner 重规划也有独立上限；但 ReAct 中以下三种“修正后再次调用 LLM”的协议错误只受 `max_iterations` 间接约束：

1. `finish_reason=tool_calls` 与工具调用数据或工具可用性不一致；
2. 结构化 `final_answer` 参数解析或 schema 校验失败；
3. 普通工具参数不是合法 JSON。

API 请求模型同时允许调用方传入任意大的 `max_iterations`，使总上限不能可靠承担协议修正预算。无工具场景中的无效 assistant/tool_calls 还会先写入消息历史，形成未配对工具消息；未启用 `output_schema` 时，名称为 `final_answer` 的未知工具又被动作指纹按名排除，其参数不参与指纹——整轮仅该调用时指纹恒为 `"[]"`，参数各异的连续轮次被误判为同一动作。

### 影响

- 模型持续输出不合法工具协议时会产生过多付费 LLM 调用；
- 无效工具消息污染下一轮上下文，部分 OpenAI 兼容网关会直接返回 400；
- Reflection 初稿和 Planner 每步执行都复用 ReAct，因此缺口会跨三个策略传播；
- API 层缺少边界时，部署配置中的合理默认值会被请求体的大值绕过。

### 根因

原 REASON-004 只解决协议异常的分类和短路，明确选择由 `max_iterations` 兜底，没有把“分类正确”继续推导为“每类可恢复重试都有独立预算”。三个协议错误分散在不同处理函数，也没有共享状态来表达连续修正。

## 工业级参照

- OpenAI Agents SDK 用 `max_turns` 为 agent loop 设置硬边界，并用 `ModelBehaviorError` 表达模型产生非法工具协议；本项目保留总轮次边界，同时给可恢复的协议自纠增加更小的独立预算。
- LangGraph 用 recursion limit 限制图执行步数；本项目对应 `max_iterations`。对会重复付费且原因明确的局部恢复链，再设置独立预算，避免所有错误都消耗完整图级上限。
- 项目既有 `max_empty_retries`、`max_llm_fail_retries`、`max_refine_rounds` 和 `max_replan_rounds` 已采用相同分层：全局上限负责最终兜底，局部上限负责具体恢复链。

## 修复方案

1. `ReActStrategy.execute` 新增 `max_tool_protocol_retries`，三类工具协议错误共享连续计数；允许 N 次修正，第 N+1 次仍异常硬终止，0 表示首次异常即终止。
2. 每次 `execute` 重置状态；合法普通工具协议或合法结构化 `final_answer` 清零。LLM 调用失败和空输出不证明工具协议恢复，因此不清零。
3. 达到硬上限仍按当前错误 kind 分发，使 RAISE 保持可观察；STOP 和 CONTINUE 都折算为终止，防止 handler 配置绕过硬上限。
4. 工具协议信号先校验再写 assistant 历史；无效响应不进入空输出计数、停滞检测或工具执行。
5. 动作指纹不再按工具名称无条件排除 `final_answer`。真正的结构化终止工具在停滞检测前已完成处理；未启用 schema 时同名工具必须和其它未知工具一样受停滞上限约束。
6. 新配置经 settings → container → API `AgentContext` → 三个 Agent → 三个 Strategy 透传。请求体 `max_iterations` 改为可选且限制为 1..100，缺省时使用装配值。
7. 同轮工具失败的分发顺序固定为协议类在前；协议预算耗尽即定终局，同轮业务失败不再分发。原实现按 `grouped` 插入序分发（取决于工具返回顺序），剩余 kind 仍会调用 handler——其 RAISE 会把预算诊断顶成无关错误，STOP 归因也会被硬终止错误覆盖。

### 决策取舍

三类错误共享预算，因为它们都表示“模型没有产出可执行的工具协议，下一轮用于协议自纠”。若分别计数，模型可以在错误类型间切换而不断重置预算。业务工具执行失败仍使用 `TOOL_FAILED` 和工具执行器自己的重试上限，不占协议预算。

该改动不新增错误枚举，也不修改 LLM 或 Tool Gateway 契约。Reflection 的结构化自查/修正与 Planner 的 plan/replan/summarize 已由结构化输出组件的固定回喂和降级次数约束；本配置只透传它们内部复用的 ReAct。

## 实施记录

| 范围 | 实施内容 |
| --- | --- |
| ReAct | 新增协议修正计数与统一分发（协议类优先、预算耗尽即定终局）；三类异常接入；无效历史短路；动作指纹不再按名排除 `final_answer` |
| Reflection / Planner | execute 参数和每次内嵌 ReAct 调用完整透传 |
| Agent / 装配 | `AgentContext`、三个 Agent 包装器、settings、container、chat 路由接线 |
| API | `SendMessageRequest.max_iterations` 改为可选的 1..100；缺省回落装配配置 |
| 测试 | 覆盖 N/0 边界、共享预算、合法轮清零、非协议轮不清零、历史不污染、指纹覆盖面和三策略透传 |

## 验证

- `tests/unit/test_react_strategy.py`：协议修正状态机、错误分发、历史所有权、动作指纹覆盖面、空输出轮不清零、协议硬上限优先于同轮业务失败分发。
- `tests/unit/test_react_strategy_nonstream.py`：非流式通道共用同一协议修正预算。
- `tests/unit/test_agent.py`、`test_reflection_agent.py`、`test_planner_agent.py`：配置从 AgentContext 到三个策略的真实行为透传。
- `tests/unit/test_settings.py`、`test_container.py`、`test_request_schemas.py`：默认值、非负校验、装配字典和 API 边界。
- `tests/integration/test_chat_flow.py`：路由层请求覆盖与缺省回落装配值。
- 全量测试、alignment 与 diff check 结果记录在 `docs/todo.md` 的对应切片。

## 教训沉淀

总循环上限只能保证最终停止，不能替代局部恢复链的预算。只要某个错误处理动作会再次发起付费调用，就应明确它消耗哪一个独立预算、由什么成功信号清零，以及 handler 是否有权绕过硬上限。同一轮里多个失败并存时，先定终局再分发：预算类失败的终局一旦成立，其余 handler 的决策改变不了结果，却能用 RAISE 顶掉诊断——分发顺序本身就是契约的一部分。

协议响应必须在写入历史前完成一致性校验。消息对象不仅是文本，也是下一次 provider 请求的协议载体；把无效 tool_calls 留在历史中，会把一个可恢复的模型行为错误放大为下一轮请求格式错误。
