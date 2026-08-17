# 研发教训

## 2026-08-17 工具模块重构（六大子组件对齐）

- **「看似 bug」需实测语义再定性**：`domain/agent/executor.py` 的 `except json.JSONDecodeError, KeyError:` 初判为 Python 2 语法 bug，实测 Python 3.14（PEP 758）下语义 = 元组捕获（运行时正确），但 <3.14 会 SyntaxError——定性为「风格/可移植性问题」而非运行时缺陷，仍需修复为显式元组。多异常捕获永远写 `except (A, B):`。
- **head+tail 截断后长度 ≠ max_length**：截断结果 = head + marker + tail，marker 占额外空间必然略超 max_length。测试断言不能写 `len(content) <= max_length`，应断言 head 开头 / tail 结尾 / marker 存在 / 远小于原始。
- **文档跨目录相对链接要算准路径**：`docs/integration_doc/tools_doc/` 下的子文档引用 `adr/` 需 `../../../` 前缀（三级到根），漏前缀会被 `verify_alignment.py` 死链校验拦截（报「死链」而非解析错误）。
- **verify_alignment 的强制项**：`app/` 下每个非空 `.py` 必须在 ALIGNMENT.md 登记；✅ 状态必须同时有非空文档 + 非空测试；文档路径必须真实存在。新增组件文件后不同步 ALIGNMENT 会直接导致 `test_verify_alignment.py` 失败。
- **async 冒烟脚本要 await**：`Container.initialize()` 是 async，`python -c` 冒烟需 `asyncio.run(main())` 包裹，否则 `RuntimeWarning: coroutine never awaited` + tool_service 为 None。
- **jsonschema 中文模板断言防笔误**：测试断言用「必须 ['fast','slow'] 之一」写错为「必须 ['fast','slow'] 之一」少个「是」导致 false negative；断言直接用代码 `_map_error` 生成的准确措辞。
- **审计落点独立于 Hooks**：审计须覆盖未注册 / JSON 失败 / 校验失败 / 成功 / 失败全路径，而 ExecutionHooks 仅成功路径——两者职责不同不能复用；未注册工具审计要保留原始工具名（`_audit` 加 `tool_name` 参数），不能硬编码兜底。

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

## 项目级教训（自 HANDOFF.md 迁移，2026-08-15 归档）

> HANDOFF.md 移除后，原「研发教训」中未沉淀到模块文档/CLAUDE.md 的条目迁移至此。

- **`__init__.py` 文件名笔误**：写成 `__ini__.py`（少个 t）会导致 `ImportError: cannot import name 'XX' from 'app.models' (unknown location)`。出现 `(unknown location)` 的导入报错，**先检查 `__init__.py` 文件名**。
- **markdown 中文表格 lint**：markdownlint 的 MD060 按字符宽度（中文算 2 格）校验表格对齐，手写中文表格极易误报。用脚本按 east_asian_width 计算列宽自动对齐。**新改表格后重跑对齐脚本。**
- **文档移动后必须同步交叉链接**：文档目录重组后，`architecture.md` 的「相关文档」链接已同步修正，但**检查其他文档中是否仍有指向旧路径的链接**（如曾引用 `config.md` 旧位置）。
- **路由导入路径与文件结构不一致**：`api/routes/` 文件名与实际导入名不一致会 ImportError；路由内引用应匹配 `app/` 下的真实模块位置。**`__init__.py` 的导入名要匹配实际文件名，跨层导入用绝对导入 `from app.xxx import ...`。**
