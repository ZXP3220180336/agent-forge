# 领域推理策略的纯转换边界

日期：2026-09-16。决定状态：已接受（用户授权执行 R4）。实现状态：已验证。
范围：Planner 步骤转换、Reflection 阶段判定、ReAct 工具协议转换。关联：[R4 计划](../../../docs/todo.md#refactoring-plan)。

## 背景

Planner、Reflection 与 ReAct 的主文件同时表达运行编排、成果与用量接管、终态提交，以及不需要
策略实例状态的纯转换。三种策略的 Guard、重试预算和最终结果语义不同，不适合建立共享阶段
框架；其中少量纯转换已有明确输入输出和独立变化原因，可以从长编排中分离。

真实备选如下：

1. 原地保留全部逻辑，只用局部注释分段。改动最小，但不能给纯转换建立独立测试面，主循环仍需
   同时承载协议细节。
2. 建立跨 Planner、Reflection、ReAct 的共享阶段或 State 对象。它会把三种不同的预算、Guard、
   usage 和终态语义压入同一抽象，新增并不存在的共同生命周期。
3. 为 Planner 与 ReAct 提取包内纯函数，Reflection 仅提取原类方法。该方案按真实输入输出划界，
   不转移运行状态或 Owner，因此被采用。

工业级参照采用 Martin Fowler 的
[Extract Function](https://refactoring.com/catalog/extractFunction.html) 与
[Extract Class](https://refactoring.com/catalog/extractClass.html)：优先提取有独立名称和变化原因的
计算；只有状态与生命周期形成独立职责时才升级为对象。本次纯转换符合前者，尚不满足后者。

## 决定

- `_planner_steps.py` 只负责步骤规范化、对外计划快照和 ReAct 单步结果到审计记录的转换。
  `PlannerStrategy` 继续拥有串行执行、replan 预算、子运行控制、usage/事实接管和完整或部分提交。
- `_react_protocol.py` 只负责 final_answer 工具定义、批内调用身份检查、结构化答案解析校验和动作
  指纹。`ReActStrategy` 继续拥有协议修正预算、消息历史、真实工具批次、usage、Guard 与终态。
- Reflection 不新增模块或状态类；`_finalize_after_critique` 收拢已有提交判定，
  `_adopt_refined_draft` 明确完整修正稿的接管与已完成轮数。`_critique`、`_refine` 和 `_finalize`
  仍是调用、用量与唯一 done 的 Owner。
- 新模块保持包内，不从 `reasoning.__init__` 导出；依赖方向仍为 `reasoning → ports/shared/prompts`。

## 保持的运行契约

- Planner 在子 ReAct 关闭并转交工具事实后，依次接管 outcome、归并 usage/iterations、记录当前
  步骤，再检查 Guard；replan 调用上界与计数时点不变。
- Reflection 的 critique/refine usage 先归账；非空修正稿先成为最近完整稿，再检查取消、期限与
  成本。自查通过和达到上限仍只受 strict deadline 复查影响。
- ReAct 在任何消息历史或真实工具调用前检查批内身份；final_answer 只在 output schema 启用时
  具有终止语义；动作指纹保留调用顺序及既有 JSON 规范化。

## 未纳入

本次不修正 Planner 空描述步骤可能留下依赖编号的既有边缘行为，也不定义 final_answer 与普通
工具混用、反向 finish_reason 不一致或畸形 function 载荷的新协议。这些需要独立行为契约与失败
测试。R5 的工具批次执行、事实收集和终态能力没有迁移或提前实施。

## 后果

- 收益：纯转换拥有直接测试，策略主循环更集中地表达阶段顺序；协议或记录格式变化可在较小范围
  定位，同时不改变 Guard、usage、事实与终态 Owner。
- 代价：Planner 运行模块对包内 `_planner_steps` 增加类型和函数依赖，ReAct 运行模块对包内
  `_react_protocol` 增加协议依赖；阅读单个策略时需要在两个文件间跳转。Reflection 的阶段方法
  仍与类状态耦合，不能单独作为无状态组件复用。
- 升级触发条件：只有出现多个真实调用方，或某组转换取得独立状态、资源、重试预算或终态所有权
  时，才评估 Extract Class 或新的对象边界；仅因文件继续增长不升级。若三种策略形成可证明相同的
  阶段契约，再另立决策评估共享阶段，不在本 ADR 中预设。

## 验证

新增纯转换测试及 Reflection 修正后同时取消的成果接管测试；Planner、Reflection、ReAct 双通道、
Agent 桥接和工具事实测试继续保护跨组件 Owner。合并定向 250 项、全量 1230 项通过；文档对齐、
编译、差异格式和独立复核结果见 [R4 计划](../../../docs/todo.md#refactoring-plan)。
