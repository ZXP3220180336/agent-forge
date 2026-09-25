# 数据库基础设施问题索引

| ID | 问题 | 状态 | 日期 |
| --- | --- | --- | --- |
| [DB-001](2026-09-22-missing-asyncpg-dependency.md) | 默认 PostgreSQL 方言所需 asyncpg 缺少正式依赖 | 已修复驱动依赖；数据库就绪另行验收 | 2026-09-22 |
| [DB-002](2026-09-22-unstarted-connection-cleanup.md) | 未启动连接清理造成永久不可用 | 已修复；DB-F02 开发阶段发现 | 2026-09-22 |
| [DB-003](2026-09-22-runtime-resource-ownership.md) | 关闭遗漏 Session/连接 Owner 与并发任务引用竞争 | 已修复；DB-F02 开发阶段发现 | 2026-09-22 |
| [DB-004](2026-09-23-ping-admission-write.md) | `ping()` 观测在内部缺陷时写入准入状态 | 已修复；DB-F02 提交前审查发现 | 2026-09-23 |
| [DB-005](2026-09-23-runtime-reason-classification.md) | 运行期自身原因码被归类为未知缺陷 | 已修复；DB-F02 提交前审查发现 | 2026-09-23 |
| [DB-006](2026-09-23-idle-driver-termination-guard.md) | 空闲驱动的强制终止缺少 Owner 过滤 | 已修复；关闭流程解析核对发现 | 2026-09-23 |
| [DB-007](2026-09-23-half-built-engine-admission.md) | 半构建引擎会开放准入且丢失跟踪与脱敏 | 已修复；DB-F02 提交前审查发现 | 2026-09-23 |
| [DB-008](2026-09-23-connect-during-close-assertion.md) | 关闭期连接拦截缺少直接断言 | 已修复；DB-F02 后续发现登记 | 2026-09-23 |
| [DB-009](2026-09-23-pool-logger-name-hardcode.md) | 池实例 logger 名硬编码，换池实现会静默失效 | 已修复；DB-F02 后续发现登记 | 2026-09-23 |
| [DB-010](2026-09-23-stale-probe-cancellation.md) | 旧探测调用取消时误取消后继 worker | 已修复；DB-F02 工作区审查发现 | 2026-09-23 |
| [DB-011](2026-09-23-migration-final-deadline-guard.md) | 同步历史校验后遗漏最终期限检查 | 已修复；DB-F03a 开发阶段发现 | 2026-09-23 |
| [DB-012](2026-09-23-migration-optional-attribute-guard.md) | 事务前置条件读不到可选属性时落入 AttributeError | 已修复；DB-F03a 后续发现登记 | 2026-09-23 |
| [DB-013](2026-09-23-migration-cancel-cleanup.md) | 业务取消 Guard 阻止迁移必要回滚 | 已修复；DB-F03b 开发阶段发现 | 2026-09-23 |
| [DB-014](2026-09-23-migration-terminal-preservation.md) | 迁移迟到消息或收尾取消覆盖主失败 | 已修复；DB-F03b 开发阶段独立复审发现 | 2026-09-23 |
| [DB-015](2026-09-24-migration-verification-followups.md) | 命令原因码语义与行为边界表述不符，且收尾/终态分支缺断言 | 已修复；DB-F03b 工作区验证发现 | 2026-09-24 |
| [DB-016](2026-09-24-orm-default-factories.md) | ORM 时间与可变默认值在导入时求值 | 已修复；DB-F04 真实写入复现 | 2026-09-24 |
| [DB-017](2026-09-24-catalog-char-decoding.md) | 内部 char 解码导致 catalog 误判 | 已修复；DB-F04 真实 PostgreSQL 发现 | 2026-09-24 |
| [DB-018](2026-09-24-orm-schema-shadowing.md) | ORM 未限定 public 导致同名表遮蔽 | 已修复；DB-F04 独立复审 | 2026-09-24 |
| [DB-019](2026-09-24-incoming-foreign-key-check.md) | 严格基线遗漏改变写入语义的入向外键 | 已修复；DB-F04 独立复审 | 2026-09-24 |

[问题导航](../../README.md) · [数据库决策](../../../adr/infrastructure/database/README.md)
