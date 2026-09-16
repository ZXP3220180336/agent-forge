# 三策略 execute 参数列表持续扩张（REASON-026）

状态：已修复。优先级：P2。发现来源：R-03 架构复核。
范围：reasoning 三策略、Agent 桥接、嵌套策略调用与直接策略测试。

## 现象与影响

ReAct、Planner、Reflection 的 `execute()` 入口包含二十余个平铺标量。相同参数在三个 Agent 桥接和
Planner/Reflection 内部 ReAct 调用中重复列出；增加一种控制或预算时，需要修改多个生产调用点和
大量测试，容易发生遗漏、位置错配或语义不明。此前参数仍能正确运行，因此这是已核实的维护性和
契约漂移风险，不是已发生的运行故障。

## 根因与取舍

策略从最初的模型和迭代参数逐步加入上下文、取消、deadline、重试、工具和运行身份控制，但入口
一直沿用标量扩展，没有在稳定语义形成后收敛参数边界。单一大 Context 虽能缩短签名，却会隐藏不同
变化原因和适用范围，因此选择六类语义参数对象。详细备选、`None/0` 语义及边界见
[参数分组 ADR](../../../adr/domain/reasoning/2026-09-16-semantic-execution-parameters.md)。

## 修复

- 新增六类 frozen、slots 值对象，三策略执行入口改为接收这些对象。
- 运行作用域在构造时校验身份和 Event 链，恢复预算拒绝负数，防直接策略调用在非法控制状态下
  启动外部副作用。
- Agent 桥接显式完成 `AgentContext → reasoning` 映射；嵌套 ReAct 复用父作用域和对应预算。
- replan/refine 进入同一个 `RecoveryBudget`，以可空字段表达策略适用性；缺失专属预算提前失败。
- 全部生产调用和直接测试迁移到新契约，不保留双签名兼容层。

## 验证与教训

测试直接覆盖对象冻结、Event 可传播、运行作用域与负预算校验、`None/0` 区分和
Planner/Reflection 缺失预算；三种 Agent、
三策略、嵌套 ReAct、双通道、取消、deadline、usage 与工具事实继续回归。最终验证数量见
[R-03 计划](../../../docs/todo.md#r-03-reasoning-execution-parameters)。

当一组参数在多个调用点稳定成形时，应按变化原因引入小型参数对象；不要等到签名继续扩张，也不要
用一个万能 Context 把不同所有权重新混在一起。
