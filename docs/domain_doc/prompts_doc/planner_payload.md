# Planner Payload 组件设计说明

> **对应代码**：`app/domain/prompts/_planner_payload.py`
> **文档定位**：Prompts 包内部的 Planner 三阶段语义投影协作契约
> **更新日期**：2026-09-13
> **状态与映射**：[ALIGNMENT](../../ALIGNMENT.md)

## 设计目标与边界

该组件为 Planner 的初始规划、失败后重规划和最终汇总请求形成只读载荷。它解决的核心
问题是：当工具目录或步骤记录很长时，必须保住任务目标、步骤因果和证据引用，不能用
整体头部截断删除后部成功步骤或当前失败事实。

组件只由同包 `PromptManager` 调用，不是模块公开 API。它不修改 goal、executed、
failed_step 或工具调用，不调用模型，不决定结构化 schema，也不负责 Guard、usage 或
provider 最终请求准入。Planner 的执行和终态契约见
[PlannerStrategy](../reasoning_doc/planner.md)。

## 内部协作契约

| 入口 | 输入 | 输出 | 对应阶段 |
| --- | --- | --- | --- |
| `serialize_planning_sections` | `goal`、`tool_descriptions`、可选预算与计数器 | `(goal_text, tools_text)` | 初始规划 |
| `serialize_replan_sections` | `goal`、工具目录、`executed`、`failed_step`、`error`、可选预算与计数器 | `(goal, tools, executed, failed, error)` 五段文本 | 失败后重规划 |
| `serialize_summarize_sections` | `goal`、`executed`、可选预算与计数器 | `(goal_text, step_results)` | 最终或部分成果汇总 |

`available_tokens=None` 或 `count_tokens=None` 时使用完整投影，不执行 token 降采样。
组件始终返回字符串元组，不返回 `None` 或 `ContextWindowExceededError`。

## 业务数据与保留规则

### 初始规划

`goal` 是任务契约，任何层级都完整保留。工具目录按行解析，第一个冒号之前的部分视为
工具身份；所有工具身份始终保留，说明文字依次采用完整、160 字符、64 字符和仅身份
四个层级。若目标与全部工具身份仍超过动态预算，组件返回该最小骨架，由调用策略决定
是否阻止真实请求。

### 步骤记录

replan 和 summarize 按 `executed` 原始顺序保留所有步骤，每条最小审计骨架包括：

```text
[步骤 <id>] description=<描述> depends_on=<依赖> status=<成功/失败>
```

在容量允许时继续携带：

- 成功步骤的 `summary`，缺失时使用 `content`；
- 成功步骤工具调用的工具名与查询参数；
- 失败步骤的 `error`。

`depends_on` 来自 Planner 已接管的执行事实，用于让重规划和汇总保持因果顺序。组件不
重新计算依赖，也不按步骤成功与否重排记录。

### 证据与失败诊断

工具引用兼容 ReAct 原始 function call 形状和已规范化的 `tool` / `params` 形状：

```text
<tool_name>(<arguments>)
```

只有成功步骤的工具调用进入 `evidence`。失败调用仍保留在调用方的原始审计记录中，
本投影不把它发送为证据；失败原因单独保留，供 `explicit_abstention` 和 `next_steps`
使用。该边界与产品的“失败记录不能充当支持证据”一致。

重规划还会把当前 `failed_step` 投影为 id、description、depends_on、success、error，
避免把完整 transcript 与 `executed` 重复发送。外部 `error` 段继续保留，供模板直接
强调本轮需要解决的失败根因。

## 分层降采样

组件先生成完整候选，再用注入的 `count_tokens` 检查所有动态段总量。超限时整组切换到
下一层，避免某个早期步骤独占容量。数字表示该字段最多保留的字符；“完整”表示不截断。

### Planning 工具说明层级

| 层级 | 工具说明 |
| --- | ---: |
| 完整 | 完整 |
| L1 | 160 |
| L2 | 64 |
| 最小 | 0，仅工具身份 |

### Replan 层级

| 层级 | 工具说明 | 步骤描述 | 成功结果 | 工具参数 | 失败原因 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 完整 | 完整 | 完整 | 完整 | 完整 | 完整 |
| L1 | 96 | 160 | 220 | 96 | 160 |
| L2 | 32 | 100 | 100 | 48 | 100 |
| 最小 | 0 | 64 | 48 | 24 | 64 |

### Summarize 层级

| 层级 | 步骤描述 | 成功结果 | 工具参数 | 失败原因 |
| --- | ---: | ---: | ---: | ---: |
| 完整 | 完整 | 完整 | 完整 | 完整 |
| L1 | 160 | 220 | 96 | 160 |
| L2 | 100 | 100 | 48 | 100 |
| 最小 | 64 | 48 | 24 | 64 |

字符上限决定每层保留的业务细节，是否选择该层仍由 token 计数结果决定。文本截断保留
首尾并插入 `…`：首部通常包含对象身份，尾部可能包含结果或错误结论。

## 省略与最终准入

被省略的工具说明、步骤描述、结果、参数和错误字符统一汇总为标记：

```text
<omitted section="tool_descriptions" characters="1200" reason="context_budget"/>
<omitted section="step_results" characters="860" reason="context_budget"/>
```

标记本身参与动态段计量。若最小层仍不满足容量，组件仍返回完整目标、工具身份和全部
步骤骨架；它不会删除因果事实来制造“已在预算内”的假象。

`PromptManager` 把动态段填入固定模板后，`PlannerStrategy` 会对完整 prompt 再次计数。
超限时构造 `ContextWindowExceededError(max_tokens=0)` 并沿现有 Context Guard 收尾，
真实 SDK 调用为零。通过本地计数也不等于 provider 一定接收；Integration 仍需把输出
预留和结构化 schema 计入最终准入。

## 行为边界

| 场景 | 行为 | 不变量 |
| --- | --- | --- |
| 无工具 | 保留调用方提供的“无可用工具”文本或空目录 | 不创造工具能力 |
| 预算充足 | 返回完整业务投影 | 原始对象不变 |
| 中间步骤内容很长 | 所有步骤统一降到同一层 | 后部步骤不会被整体头部截断删除 |
| 成功与失败交错 | 成功步骤保留 evidence，失败步骤保留 error | 失败调用不作为支持证据 |
| 工具参数不是字符串 | 使用紧凑 JSON 序列化 | 参数仍可追溯 |
| 最小骨架超限 | 返回最小骨架，交给 Planner 本地 Guard | 不发同阶段缩减重试 |
| 输入或计数器异常 | 异常直接传播 | 不转换成普通 plan/replan/summarize 失败 |

组件没有异步任务、资源所有权、取消处理或 usage 结算。

## 验证入口

[tests/unit/test_prompts.py](../../../tests/unit/test_prompts.py) 覆盖：

- 规划保留完整目标和全部工具身份；
- 重规划保留失败原因、依赖和成功步骤的工具参数；
- 汇总在预算下保留后部成功步骤，不把失败调用写入 evidence；
- 分层省略标记和输入只读。

[tests/unit/test_planner.py](../../../tests/unit/test_planner.py) 覆盖三个阶段最小 prompt 超限时
的零结构化调用、已有 plan/步骤/usage 保留，以及只有失败记录时不能标为部分成功。
当前映射与验证状态见 [ALIGNMENT](../../ALIGNMENT.md)。

## 设计决策与问题记录

- [语义上下文预算 ADR](../../../adr/domain/reasoning/2026-08-28-context-budget.md)：领域字段选择与统一 token 计量。
- [Planner Strategy ADR](../../../adr/domain/reasoning/2026-09-05-planner-strategy.md)：计划、依赖步骤和证据链汇总契约。
- [REASON-025](../../../issues/domain/reasoning/2026-09-13-planner-semantic-context-reduction.md)：固定字符头部截断的修复闭环。

## 相关文档

- [Prompts 模块公开契约](prompts.md)
- [Reflection payload 组件](reflection_payload.md)
- [PlannerStrategy](../reasoning_doc/planner.md)
- [ContextBudgetPort](../ports_doc/ports.md)
- [良率 RCA 业务对象](../../project/product.md#良率-rca-的业务对象与报告生命周期)
