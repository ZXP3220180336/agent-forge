# 单一文档体系迁移说明

本次合并原入口与治理入口，原有项目资料成为正式文件，每项只有一个维护位置。不包含旧治理草案或原文快照目录；当前目录不依赖上一版交付包。

## 目录对照

| 原位置 | 唯一正式位置 |
| --- | --- |
| 原、新 AGENTS.md | [根 AGENTS](../AGENTS.md) |
| 原、新 CLAUDE.md | [根 CLAUDE](../CLAUDE.md)，单向委托 AGENTS |
| docs/layer_readme_doc.md | [层规范](engineering/documentation/layer_readme_doc.md) |
| docs/module_doc.md | [模块规范](engineering/documentation/module_doc.md) |
| docs/component_doc.md | [组件规范](engineering/documentation/component_doc.md) |
| docs/architecture.md | [架构](project/architecture.md) |
| docs/deployment.md | [部署](project/deployment.md) |
| docs/product.md | [产品](project/product.md) |
| docs/ALIGNMENT.md | [对齐表](ALIGNMENT.md)，保留路径与表结构 |
| docs/todo.md | [活动计划](todo.md)，已完成的独特交接信息归 [完成记录](history/completed-work.md) |
| docs/lessons.md | [教训](lessons.md)，重复规则改为正式正文链接 |
| 原模块说明、ADR、Issue | 保留原有正式目录；导航见 [完整目录](catalog.md) |

新增 [文档维护 Skill](../skills/documentation-maintenance/SKILL.md)负责说明、决策、问题、计划和教训；根入口按 Trigger 要求读取。三份写作规范只留各自模板，共用规则在同目录 README；记录类规则在 records.md，各索引不再抄写登记规范。

## 仓库接入结果

2026-09-12 已接入完整仓库，基准提交 `c351a81`，变更保留在工作区，未提交。唯一入口、工程规范、项目文档与五项 Skill 已按上表归位；旧正式路径已移除。保留 README 业务内容及 ALIGNMENT/todo/lessons 的稳定路径，不建立资料包、原文快照或第二套规范。

本轮恢复 43 处真实源码/测试链接，修复 3 处 Markdown 示例误替换，合并问题索引的重复导航，补齐文档问题目录入口。治理规则补回原入口的通用审查维度，修正失效章节称呼；未改变业务契约。

工具自动发现技能的配置未变更。根 AGENTS 明确要求按 Trigger 读取对应 SKILL.md，五项项目 Skill 通过该入口生效。

## 验收与范围

- 259 份正式 Markdown 的本地文件链接、标题和显式锚点检查通过；从 AGENTS 可达全部正式 Markdown 文件。
- 已迁移旧路径仅保留在本页的历史目录对照；源码、测试与脚本未发现对旧文档路径的依赖。
- `uv run python -m scripts.verify_alignment`（使用项目内 UV 缓存）通过；脚本检查登记、路径和文件状态，不验证业务契约。
- `git diff --check` 通过。未运行产品测试、启动服务或验证数据库/外部模型。
- 工作区改动仅涉及 Markdown 文档与 Skill；本轮不修改源码、测试、配置或校验脚本。

已提供资料的治理搭建与迁移已完成。`编码规范文档.txt` 未在仓库找到，外部来源待提供，详见[引用与来源核对](../issues/documentation/2026-09-12-reference-gaps.md)；不宣称其已迁移或已被其他规范替代。

Reflection/Planner 语义预算及 G0-6 等代码符合性工作是独立后续任务，见[计划](todo.md)与[治理决策](../adr/2026-09-12-single-source-governance.md)，不作为本次治理迁移验收条件。
