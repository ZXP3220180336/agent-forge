# 校验失败日志未脱敏：jsonschema e.message 嵌入完整实例值（业务敏感数据落盘）

> **状态**：✅ 已修复（2026-08-16）
> **优先级**：P0（安全——日志泄露业务敏感数据）
> **来源**：2026-08-16 安全修复 · 2026-08-16 从 structure.md「已知边界与设计取舍」补提取归档
> **涉及模块**：`app/integration/llm/structured.py`（`_validate_schema` 日志 · `_collect_schema_error_summaries` · `_truncate_json_for_log`）
> **关联文档**：[structure.md](../../../docs/integration_doc/llm_doc/structure.md)

---

## 问题描述

### 现象

`_validate_schema` 校验失败日志直接用 `e.message` 落盘——jsonschema 的 `message` 会嵌入**完整实例值**（如 `'<超长值>' is too long`）。模型输出可能含业务敏感数据（Yield RCA 场景为良率/晶圆数据），全量落盘是泄露面；`parsed` 也未截断。回喂循环日志同源——`_collect_schema_errors` 保留 `e.message` 供回喂模型，同一文本也用于日志落盘。

### 影响

校验失败日志完整落盘模型输出的业务敏感数据，违反本模块「模型输出不完整落盘」安全基线（`_truncate_text_for_log` / `_LOG_TRUNCATE_LIMIT=500`）。

### 根因

校验失败日志未复用脱敏机制——直接落 `e.message`（含实例值），`parsed` 未截断；「给模型的错误」与「落盘的日志」未区分（模型需要具体错误修正，日志只需诊断摘要）。

---

## 工业级参照

| 结论 | 做法 |
| --- | --- |
| 日志最小化 | 日志只记诊断所需最小信息——结构化字段摘要（字段路径 + `validator` + `validator_value`），不嵌入数据值；可观测性不因脱敏丢失（校验器名/字段路径仍在） |
| 错误与日志分离 | 给模型的错误保留完整 `e.message`（模型需具体错误修正），日志落盘用脱敏摘要——两套文本，敏感数据不泄露、纠错能力不损 |

---

## 修复方案（含决策取舍）

**决策**：`_validate_schema` 失败日志用**结构化字段摘要**替代 `e.message`；`parsed` 经 `_truncate_json_for_log` 截断到 `_LOG_TRUNCATE_LIMIT`（500 字符）；`schema`（接口契约，非业务数据）保留完整。回喂循环日志同源修复——新增 `_collect_schema_error_summaries` 用于日志落盘，`_collect_schema_errors` 保留 `e.message` 供回喂模型。

**修复要点**：

1. `_validate_schema` 失败日志：字段摘要（路径 + validator + validator_value）替代 `e.message`；
2. `parsed` 日志截断到 500 字符；`schema`（契约）保留完整；
3. **回喂与日志分离**：`_collect_schema_errors`（含 `e.message`）只供回喂模型；新增 `_collect_schema_error_summaries`（结构化字段摘要）用于 `_try_extract` 回喂日志——回喂模型纠错能力不损，日志不落敏感数据。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/structured.py` | `_validate_schema` 失败日志脱敏（字段摘要 + `parsed` 截断）；`_collect_schema_error_summaries` 回喂日志脱敏 | `test_generate_structured.py` 校验失败日志不落完整实例值 |

---

## 验证

- 校验失败日志含字段路径/validator，不含完整实例值；`parsed` 截断到 500 字符
- 回喂模型仍拿到完整 `e.message`（纠错能力不损）
- 全量测试通过（2026-08-16 修复时验证）

---

## 教训沉淀

- **错误信息与日志分离**：模型回喂需要完整错误（`e.message`），日志落盘只需诊断摘要（字段路径 + validator）——两套文本，敏感数据不因日志泄露、模型纠错能力不损。
- **日志最小化是安全基线**：校验失败日志不要直接落 `e.message`（jsonschema 会嵌入完整实例值）——结构化字段摘要替代，可观测性不丢。
