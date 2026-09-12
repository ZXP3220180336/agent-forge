---
name: agent-lifecycle-review
description: 新增、修改或审查 Agent/工作流的外部调用、usage、资源、Guard、取消、Deadline、部分成果或终态时，检查生命周期与副作用一致性。
---

# Agent 生命周期审查

依据 [运行时路由](../../docs/engineering/agent-runtime-rules.md#routing)选择 R1–R6，引用 [通用 G0](../../docs/engineering/ai-engineering-rules.md#g0)判定正确性，不建立重复规则。

1. 识别变更涉及的真实调用、共享状态和终态提交点；必要时追踪调用方与错误策略。只按函数名判断不足以确认生命周期完整。
2. 沿正常返回、异常、取消、超时、early return 和 finally 检查事实接管、Owner 转移与结算。用一条资源流或小表说明责任，不要求新增状态对象。
3. 检查调用前后 Guard、当前取消模式、统一优先级、预算与最后提交窗口；区分取消请求、已提交终态和迟到响应。
4. 核对有效成果与 termination 的表达，以及 Tool history 合法性；追踪 STOP 是否间接触发 fallback/summary，RAISE 是否在上层形成不可达分支。
5. 从运行时验证场景中选取与改动相关的竞态和失败路径，优先可控时钟、响应和调用计数。涉及自动重复时读 R3，必要时合并执行 [retry-review](../retry-review/SKILL.md)。

交付：受影响生命周期、可证实缺陷及位置、验证结果、缺失契约。若项目未定义取消模式、Guard 优先级或成果语义，指出具体缺口，不能自行套用示例顺序。纯审查不修改业务；实现任务按授权修复并重验。满足现有边界时明确保持现有结构。


## 项目接入

必须读取 [项目契约导航](../../docs/engineering/agent-runtime-rules.md#reflection-exception)及 [参数语义与结果判定来源](../../docs/engineering/agent-runtime-rules.md#project-contracts)，按变更涉及的策略进入正式契约，特别核对 REASON-020 自查早退与 Planner 二维判据。不因通用模板给这些既有行为贴缺陷标签；对需要迁移的语义提出具体 ADR 与回归方案。
