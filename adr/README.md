# 根级横切 ADR 索引

> **用途**：登记不归属单一「层/模块」的**横切决策**（涉及多个层、跨 application/api/domain 等），追踪从 Context → Decision → Consequences 的决策记录。
> **定位**：模块级 ADR 归 `adr/<层名>/<模块名>/`（跨模块决策以主决策模块归位）；**本目录专收横切决策**（无单一主决策模块），子目录化会人为绑架到某层。根级决策强制在此登记，防文件失联。
> **更新日期**：2026-09-12

## 决策索引

| ID | 决策标题 | 状态 | 涉及层级 | 决策日期 |
| --- | --- | --- | --- | --- |
| [ADR-001](2026-09-02-request-build-validation.md) | 请求构建期校验取舍：不建集中校验层，超长上下文裁剪显式告警 | ✅ 已采纳 | application + api（横切） | 2026-09-02 |
| [ADR-002](2026-09-12-single-source-governance.md) | 单一文档维护体系与治理目标 | 文档整合已采用；代码迁移待实施 | 横切治理 | 2026-09-12 |

登记、状态和维护规则见 [记录规范](../docs/engineering/documentation/records.md)。本表保留本模块的既有编号序列；历史状态为当时记录，不代表本轮重新验证。

## 目录导航

- [integration/llm/README.md](integration/llm/README.md)
- [integration/tools/README.md](integration/tools/README.md)
- [2026-09-02-request-build-validation.md](2026-09-02-request-build-validation.md)
- [2026-09-12-single-source-governance.md](2026-09-12-single-source-governance.md)
