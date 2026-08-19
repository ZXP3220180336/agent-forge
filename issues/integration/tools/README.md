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
