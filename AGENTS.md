# 项目 AI 协作入口

本文件是唯一共享入口，CLAUDE.md 单向引用本文件。用中文沟通，先给结论；目标或关键契约不清时澄清；写前方案确认按 [项目工作流](docs/engineering/project-workflow.md#planning)执行。所有设计、实现和文档决策先按 [产品导向](docs/project/product.md)判断主链路价值，再执行 Gate；不凭历史 TODO 扩大任务。

## 核心不变量

以下仅摘要，完整定义及适用边界见 [G0](docs/engineering/ai-engineering-rules.md#g0)：

- G0-1：单次自动执行有可证明的终止条件。
- G0-2：已选定终态后不再启动新的业务副作用；必要收尾有界。
- G0-3：资源与用量有唯一逻辑结算责任，重复清理幂等。
- G0-4：终止不抹除已发生且有账务、恢复或成果意义的事实。
- G0-5：不发送本地已知非法的协议状态。
- G0-6：非关键观测失败不覆盖主业务终态。
- G0-7：没有收到成功响应不证明远端未执行，重试须处理重复副作用。

Gate 检查现有边界能否承载变化；按现有职责、小范围提取、有证据的新抽象依次选择。LOC 只是审查信号。产品价值不能豁免正确性；不硬编码或提交密钥。

## Trigger → 规范 → Skill

命中一行时必须读取该行对应的规范与 Skill，并按其执行。多项命中合并证据，避免递归重复检查；不预加载整套文档。这里的路由可通过读取文件执行，不依赖工具是否自动发现技能。

| 任务触发条件 | 按需规范或项目文档 | 执行 Skill |
| --- | --- | --- |
| 功能实现、Bug 修复、代码修改及其非平凡计划 | [工作流](docs/engineering/project-workflow.md)、[通用 Gate](docs/engineering/ai-engineering-rules.md#gates) | [code-change](skills/code-change/SKILL.md) |
| 代码审查 | [规则等级](docs/engineering/ai-engineering-rules.md#levels)、受影响模块契约 | [code-review](skills/code-review/SKILL.md) |
| 新增类/接口/Policy/State、编排复杂度增长 | [最小结构变化与 E9](docs/engineering/ai-engineering-rules.md#abstraction) | [code-change](skills/code-change/SKILL.md)，审查任务使用 code-review |
| retry、fallback、refine、replan、预算计数变化 | [重试 Gate](docs/engineering/agent-runtime-rules.md#retry)、对应模块正式契约 | [retry-review](skills/retry-review/SKILL.md) |
| 调用、usage、资源、取消、deadline、Guard 或终态变化 | [运行时路由](docs/engineering/agent-runtime-rules.md#routing)、[集成检查](docs/engineering/integration-rules.md) | [agent-lifecycle-review](skills/agent-lifecycle-review/SKILL.md) |
| 产品取舍、架构、分层、公共契约与配置变化 | [产品](docs/project/product.md)、[架构](docs/project/architecture.md)、[配置](docs/config_doc/config.md) | 实现用 [code-change](skills/code-change/SKILL.md)；记录设计用 [documentation-maintenance](skills/documentation-maintenance/SKILL.md) |
| 新增/修改层 README、模块说明或组件说明 | [文档规范导航](docs/engineering/documentation/README.md)选择对应模板 | [documentation-maintenance](skills/documentation-maintenance/SKILL.md) |
| 新增/修改 ADR、Issue、计划、教训 | [记录规范](docs/engineering/documentation/records.md) | [documentation-maintenance](skills/documentation-maintenance/SKILL.md) |
| 部署、验证命令、状态或文档路径变化 | [部署](docs/project/deployment.md)、[对齐表](docs/ALIGNMENT.md)、[文档规范](docs/engineering/documentation/README.md) | [documentation-maintenance](skills/documentation-maintenance/SKILL.md)；代码变化追加 code-change |

纯文档设计与计划走 documentation-maintenance；仅实际改变相应运行契约时追加专项审查，不因文件名含 budget/retry 就进入代码实现。

## 导航

[全部项目文档](docs/README.md) · [治理规则目录](docs/engineering/README.md) · [ADR](adr/README.md) · [Issue](issues/README.md) · [当前计划](docs/todo.md) · [教训](docs/lessons.md)。

新说明文档进入其父导航，ADR/Issue 进入所属索引；根入口只在新增文档类型或触发类别时更新。每项规则和文档仅维护一个正式位置，不复制原件、快照或第二份入口。
