---
name: retry-review
description: 新增、修改或审查 retry、fallback、协议修正、refine、replan 和 SDK 重试配置时，验证调用上界、预算语义及重复副作用风险。
---

# 重试审查

依据 [通用 E4 与 G0](../../docs/engineering/ai-engineering-rules.md#gates)；Agent/工作流读取 [R3](../../docs/engineering/agent-runtime-rules.md#retry)。普通网络重试使用其中适用的预算语义，不强加 Agent 状态模型。

1. 从真实调用点沿循环、异常恢复、fallback 和 SDK 内置机制画出重复路径；不要只搜索名称包含 retry 的函数。
2. 为每条路径记录操作单位、计数 Owner、首次调用语义、最大值、重置与共享关系、耗尽动作。合并描述同一机制，不要求独立预算类。
3. 推导实际外部调用上界，检查错误交替、嵌套重试和重置能否逃逸预算；同时检查 deadline、取消、成本对准入和等待的约束。
4. 模拟“远端已成功、本地未收到响应”，核对幂等/核对策略和 usage 结算；不能用再次尝试掩盖不确定事实。
5. 按受影响风险验证零预算、首次尝试、最后允许尝试、下一次被阻止，以及重置、取消和失败路径。观察调用次数与账务效果，不只断言计数器值。

输出：路径及上界、发现与证据、未验证项。若属于实现任务，在授权范围内修复后重验；若仅要求审查，提交发现即可。问题涉及生命周期时读取 R1/R2/R4/R6 对应章节，不反向调用通用审查形成循环。


## 项目接入

从 [参数语义与结果判定来源](../../docs/engineering/agent-runtime-rules.md#project-contracts)进入对应正式契约，核对 LLM 与 Tool 的首次计数差异、Reflection/Planner 轮次；涉及集成恢复读 [集成规则](../../docs/engineering/integration-rules.md)，据真实嵌套路径推上界，不把两个默认值相等当相同语义。
