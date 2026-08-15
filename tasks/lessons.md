# 研发教训

## 2026-08-15 文档/测试以代码架构为准对齐

- 文档目录迁移后，批量替换链接时不能只匹配带 `docs/` 前缀的路径：文档内互链常用不带前缀的相对路径（如 `service_doc/llm_doc/llm.md`），需同时替换两种形式。
- 批量替换要按「最长最具体 → 最短最通用」排序，否则 `app/services/` 这类通用前缀会先吃掉子路径，导致错误映射。
- `apply_patch` 一次补丁不能对同一文件拆成两个 `Update` 块；遇到同一文件多处改动要合并到一个块或分次提交。
- 沙箱/策略可能拦截 `Remove-Item` 等破坏性命令；空目录不影响 git 追踪，优先保证工作区内容正确，清理可后置。

## 2026-08-15 补齐缺失测试（第 7 项）

- 手写 fake DB 语句分发时，不能用 `column_descriptions[0]["entity"]` 区分实体行查询与聚合查询：聚合 select（stats/count）的 entity 也是 FROM 映射类。改用 `descs[0]["expr"]` 是否为映射类（type）判定。
- fake 的 `db_session_factory` 返回值必须实现异步上下文管理器（`__aenter__/__aexit__`），因为业务侧用 `async with self.db_session() as db`。
- 构造参数名 ≠ 实例属性名：`SessionManager(db_session_factory=...)` 存为 `self.db_session`，写测试断言前先看源文件的属性赋值。
- 复用 fake 要清理状态：`list_sessions` 首页会写 Redis 缓存，连续调用需清空 fake redis，否则二次调用命中缓存不查库、断言落空。
- 内置工具测试要显式 `register_config(api_key="")` 重置 key：仓库根 `.env` 含真实 TAVILY_API_KEY，不重置会触发真实网络请求。
- pydantic-settings 构造用 `Settings(_env_file=None)` 可跳过 `.env`，使配置测试确定性（不依赖环境）。
