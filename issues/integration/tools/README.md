# 工具模块问题追踪

> **用途**：登记 Integration 层工具模块（`app/integration/tools/` 及其跨模块关联方）审查/审核发现的问题，追踪从发现 → 分析 → 修复 → 验证的完整生命周期。
> **更新日期**：2026-08-19
> **关联**：[工具模块接口文档](../../../docs/integration_doc/tools_doc/tools.md) · [builtin 工具说明](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

## 状态图例

| 状态 | 含义 |
| --- | --- |
| 🔴 待修复 | 已登记未修复 |
| 🟡 修复中 | 修复实施中（代码/测试/文档） |
| ✅ 已修复 | 代码 + 测试 + 文档全部完成，已验证 |
| ⬜ 已放弃 | 评估后不修（附理由） |

## 问题索引

| ID | 标题 | 优先级 | 状态 | 涉及模块 | 登记日期 | 修复日期 |
| --- | --- | --- | --- | --- | --- | --- |
| [TOOLS-001](2026-08-18-subprocess-orphan-on-cancel.md) | executor 超时取消后子进程孤儿泄漏 | P1 | ✅ 已修复 | builtin/code_exec（CodeExecTool） | 2026-08-18 | 2026-08-18 |
| [TOOLS-002](2026-08-19-file-tools-allowed-dirs.md) | 文件工具无路径范围限制，可读 .env / 覆盖源码 | P1 | ✅ 已修复 | builtin/file_ops + settings / container | 2026-08-19 | 2026-08-19 |
| [TOOLS-003](2026-08-19-web-browse-ssrf.md) | web_browse SSRF：任意 URL + 重定向无主机/网络约束 | P1 | ✅ 已修复 | builtin/web_browse（WebBrowseTool） | 2026-08-19 | 2026-08-19 |
| [TOOLS-004](2026-08-19-memory-capped-reads.md) | 工具整文件/整响应读入内存，截断发生在读取之后 | P1 | ✅ 已修复 | builtin/file_ops + code_exec + web_browse | 2026-08-19 | 2026-08-19 |
| [TOOLS-005](2026-08-19-code-exec-gbk-decode.md) | code_exec 输出按 UTF-8 硬解码，Windows 中文环境 GBK 乱码 | P1 | ✅ 已修复 | builtin/code_exec（输出解码） | 2026-08-19 | 2026-08-19 |
| [TOOLS-006](2026-08-19-executor-json-non-dict.md) | executor 参数 JSON 解析结果未校验 dict，非 dict 抛 TypeError 逃逸 | P1 | ✅ 已修复 | tools/executor（参数解析） | 2026-08-19 | 2026-08-19 |
| [TOOLS-007](2026-08-19-executor-retry-count-semantics.md) | executor retry_count 成功/失败路径口径不一致 | P2 | ✅ 已修复 | tools/executor（retry_count） | 2026-08-19 | 2026-08-19 |
| [TOOLS-008](2026-08-19-executor-max-retries-zero.md) | executor max_retries=0 时零次循环，工具从未执行 | P2 | ✅ 已修复 | tools/executor（重试参数） | 2026-08-19 | 2026-08-19 |
| [TOOLS-009](2026-08-19-search-source-urls.md) | search 搜索结果与 answer 不携带来源 URL，证据不可回溯 | P1 | ✅ 已修复 | builtin/search（结果格式化） | 2026-08-19 | 2026-08-19 |
| [TOOLS-010](2026-08-19-external-tool-config-injection.md) | 外部工具无配置注入通道，register_config 形同虚设 | P1 | ✅ 已修复 | tools/loader + external/http_api + tool_service/container/settings | 2026-08-19 | 2026-08-19 |
| [TOOLS-011](2026-08-19-http-api-approval-ssrf.md) | external http_api 写操作未声明审批 + 无 SSRF 防护 | P1 | ✅ 已修复 | external/http_api + security（SSRF 共享抽取） | 2026-08-19 | 2026-08-19 |
| [TOOLS-012](2026-08-19-external-maybe-refresh-io.md) | maybe_refresh 每次 execute 同步磁盘 IO 上事件循环 | P3 | ✅ 已修复 | tools/loader（maybe_refresh TTL） | 2026-08-19 | 2026-08-19 |
| [TOOLS-013](2026-08-19-external-sibling-module-cache.md) | 外部工具卸载仅清理自身模块，兄弟模块缓存残留致重载失效 | P3 | ✅ 已修复 | tools/loader（模块缓存管理） | 2026-08-19 | 2026-08-19 |
| [TOOLS-014](2026-08-19-loader-scan-lock-deadlock.md) | loader _scan_lock 在生命周期钩子 await 期间持有，反向调用 execute 死锁 | P3 | ✅ 已修复 | tools/loader（锁约束） | 2026-08-19 | 2026-08-19 |
| [TOOLS-015](2026-08-19-executor-to-thread-cancel.md) | executor wait_for 超时对 to_thread 同步调用无法取消（无注释说明） | P3 | ✅ 已修复 | tools/executor（docstring 澄清） | 2026-08-19 | 2026-08-19 |
| [TOOLS-016](2026-08-19-audit-sensitive-key-masking.md) | 审计日志完整序列化参数，敏感键（API Key / Token）落盘泄露 | P3 | ✅ 已修复 | tools/security（审计脱敏） | 2026-08-19 | 2026-08-19 |
| [TOOLS-017](2026-08-19-normalize-error-indent.md) | normalize_error 每行 strip 破坏 traceback 缩进 | P3 | ✅ 已修复 | tools/result_processor | 2026-08-19 | 2026-08-19 |
| [TOOLS-018](2026-08-19-hooks-logger-name.md) | hooks logger 名 services.tool_service 与模块路径不符 | P3 | ✅ 已修复 | tools/hooks（logger 名） | 2026-08-19 | 2026-08-19 |
| [TOOLS-019](2026-08-19-search-tavily-reuse.md) | search 每次 execute 新建 TavilyClient，未复用 | P3 | ✅ 已修复 | builtin/search（客户端复用） | 2026-08-19 | 2026-08-19 |
| [TOOLS-020](2026-08-19-writefile-makedirs-async.md) | writeFile os.makedirs 同步阻塞事件循环 | P3 | ✅ 已修复 | builtin/file_ops（makedirs 异步化） | 2026-08-19 | 2026-08-19 |
| [TOOLS-021](2026-08-19-readfile-encoding-fallback.md) | readFile 仅 UTF-8 解码无 GBK 回退，中文文件乱码 | P3 | ✅ 已修复 | builtin/file_ops + shared/encoding | 2026-08-19 | 2026-08-19 |
| [TOOLS-022](2026-08-19-code-exec-workdir-empty.md) | code_exec workdir 空串传 cwd="" 抛异常 | P3 | ✅ 已修复 | builtin/code_exec（workdir 归一） | 2026-08-19 | 2026-08-19 |
| [TOOLS-023](2026-08-19-web-browse-config-injection.md) | web_browse 连接层超时/重定向硬编码，未走 register_config | P3 | ✅ 已修复 | builtin/web_browse（连接层注入） | 2026-08-19 | 2026-08-19 |
| [TOOLS-024](2026-08-19-web-browse-encoding-comment.md) | web_browse 编码注释与实现不一致（缺策略说明） | P3 | ✅ 已修复 | builtin/web_browse（注释澄清） | 2026-08-19 | 2026-08-19 |
| [TOOLS-025](2026-08-20-web-browse-parser-consistency.md) | web_browse HTML 实体未联动链接文本，`</a>` 后文本误计入链接 | P3 | ✅ 已修复 | builtin/web_browse（parser） | 2026-08-20 | 2026-08-20 |
| [TOOLS-026](2026-08-20-web-browse-parser-blank-lines.md) | web_browse 连续块标签换行观感（复核已防 + 测试锁定） | P3 | ✅ 已修复 | builtin/web_browse（parser 测试锁定） | 2026-08-20 | 2026-08-20 |
| [TOOLS-027](2026-08-20-builtin-duplicate-class-warning.md) | builtin 自动发现类名冲突静默覆盖 | P3 | ✅ 已修复 | builtin/__init__（发现告警） | 2026-08-20 | 2026-08-20 |
| [TOOLS-028](2026-08-20-builtin-lazy-comment.md) | builtin「惰性加载」注释与实现语义不符 | P3 | ✅ 已修复 | builtin/__init__（注释修正） | 2026-08-20 | 2026-08-20 |
| [TOOLS-029](2026-08-20-rca-in-range-single-time.md) | RCA _in_range 单边纯时间过滤静默失效 | P3 | ✅ 已修复 | rca/data（_in_range 补日期） | 2026-08-20 | 2026-08-20 |
| [TOOLS-030](2026-08-20-rca-time-range-dry.md) | RCA time_range 过滤逻辑三处重复 | P3 | ✅ 已修复 | rca/data（_apply_time_range 抽取） | 2026-08-20 | 2026-08-20 |

## 新问题登记规范

1. **命名**：`<日期>-<短横线描述>.md`——日期为登记日（`YYYY-MM-DD`），描述为该问题的短 slug。
2. **编号（索引 ID）**：TOOLS-XXX 递增（001、002、…），仅用于索引表展示；**文件名不含编号**。
3. **模板**：复制既有问题文件的结构——元信息块（状态/优先级/来源/涉及模块）→ 问题描述（现象/影响/根因）→ 工业级参照 → 修复方案（含决策取舍）→ 实施记录（文件×改动×回归测试）→ 验证 → 教训沉淀。
4. **登记**：新建文件后同步更新上方索引表（ID / 标题 / 优先级 / 状态 / 涉及模块 / 登记日期 / 修复日期）。
5. **修复闭环**：修复完成后更新状态为 ✅ + 修复日期，并同步对应模块文档（CLAUDE.md 工作流 gate：改代码必改对应模块文档）。

## 维护原则

- **与 todo.md / lessons.md 分离**：本目录沉淀「问题从发现到验证的完整生命周期」（可回溯、可审计）；todo.md 是「将来要做什么」；lessons.md 是「纠正后的结论」。
- **一个文件一个问题**：跨模块问题以主因模块归位（如涉及 domain/agent 在文件内说明）。
- **目录与 app/ 结构对齐**：`issues/<层名>/<模块名>/`（本目录 = `issues/integration/tools/` 对应 `app/integration/tools/`）；新层/模块的问题归入对应目录。
