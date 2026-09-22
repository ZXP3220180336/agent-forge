# 默认 PostgreSQL 方言缺少 asyncpg 依赖

> **ID**：DB-001
> **日期**：2026-09-22
> **状态 / 优先级**：已修复驱动依赖 / P1
> **发现来源**：用户数据库基础设施核验与 DB-F01 自动化红测
> **范围**：pyproject.toml、uv.lock 及驱动验收；不包含数据库连通、迁移或业务可用性

## 现象与根因

默认数据库 URL 选择 postgresql+asyncpg，但依赖与锁文件未声明 asyncpg。SQLAlchemy 构造异步引擎时导入方言驱动，因此尚未尝试连接就抛 ModuleNotFoundError。Container 置空工厂后的伪可用消费者是另一未修复边界，继续由 DB-F05 处理。

## 方案与实施

按 [DB-ADR-001](../../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md)使用唯一 PostgreSQL/asyncpg 后端。先新增 `tests/unit/test_database.py` 的真实 import 与 engine 构造测试，两项均因 ModuleNotFoundError 失败；之后添加 `asyncpg>=0.31.0` 并将锁文件解析为 0.31.0。没有升级或删除原有依赖，也没有引入 SQLite 或第二连接池。

工业参照：[asyncpg 官方安装说明](https://magicstack.github.io/asyncpg/current/installation.html)、[发行元数据](https://pypi.org/pypi/asyncpg/0.31.0/json)。安装前核对 Python 3.14 Windows wheel，安装后在实际 Python 3.14.6 环境导入验证。

## 验证

安装前两项驱动测试红测；安装后两项通过，构造的真实 SQLAlchemy 引擎显式 dispose，测试不连接数据库。相关及全量结果见 [DB-F01 评审](../../../docs/todo.md#db-f01-review)，当前映射见 [ALIGNMENT](../../../docs/ALIGNMENT.md)。

驱动安装不证明 PostgreSQL 可达、权限正确或 schema 就绪。真实 PostgreSQL 验收仍属于后续 DB-F06，空工厂、启动未探测和空 CLI 的严格 xfail 不能视为本问题修复后的业务验收成功。

## 经验与关联

异步引擎构造和真实连接是不同证据；所选数据库方言的驱动必须进入正式依赖及锁文件。正式验证要求沿用 [工作流](../../../docs/engineering/project-workflow.md#verification)，不另增全局规则。
