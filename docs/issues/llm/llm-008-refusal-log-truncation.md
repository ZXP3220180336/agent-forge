# LLM-008 拒答文本完整落日志，违反模型输出不落盘安全基线

> **状态**：✅ 已修复（2026-08-16）
> **优先级**：P0（合并前必修，安全）
> **来源**：2026-08-16 Integration 层 LLM 模块工业级审核（重要项 7）
> **涉及模块**：`app/integration/llm/structured.py`（`_raise_boundary` refusal 日志）
> **关联文档**：[structure.md](../../integration_doc/llm_doc/structure.md) · [llm-004](llm-004-empty-content-refusal-misjudge.md)（模型输出截断基线）

---

## 问题描述

### 现象

`_raise_boundary` 的 refusal 分支 `logger.warning("...refusal=%r, finish_reason=%s", ...)` 将**完整拒答文本**落盘。本模块自定「模型输出不完整落盘」安全基线（`structured.py:292-296` `_truncate_text_for_log` / `_LOG_TRUNCATE_LIMIT=500`），拒答日志是**唯一的模型输出未截断泄露点**。

### 影响

拒答文本常引用触发内容（Yield RCA 场景可能含晶圆/良率数据），完整落盘违反安全基线，敏感数据泄露。

### 根因

refusal 日志未复用本模块已建立的 `_truncate_text_for_log` 截断机制（`_validate_schema` 的错误摘要、`_truncate_json_for_log` 均已应用）。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| 本项目安全基线 | `_truncate_text_for_log` / `_LOG_TRUNCATE_LIMIT=500`——「模型输出只记截断前缀，收敛敏感数据泄露面」 |
| LLM-004 确立原则 | 模型输出（含拒答）是敏感数据载体，日志只记诊断所需前缀 |

**核心**：模型输出（含拒答文本）写入日志必须经截断，只保留诊断所需前缀。

---

## 修复方案（含决策取舍）

**决策**：refusal 日志经 `_truncate_text_for_log` 截断后落盘（`%r` → 截断后的 str）。

**取舍理由**：

1. 对齐本模块已确立的安全基线（`_truncate_text_for_log` 复用）；
2. 拒答文本是唯一未截断的模型输出泄露点，修复后全部收敛；
3. 日志与抛出的异常 message 分离——异常 message 保持简洁（不含拒答文本），日志保留截断前缀供诊断。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/structured.py` | `_raise_boundary` refusal 日志用 `_truncate_text_for_log` 截断 | `test_generate_structured.py` 新增 `test_refusal_log_truncated`（超长拒答截断标记 + 完整文本不落盘） |
| 文档 | [llm.md](../../integration_doc/llm_doc/llm.md)（已实现列表加 LLM-008 条目） | — |

---

## 验证

- `tests/unit/test_generate_structured.py` **48 passed**（含新增拒答日志截断用例）
- 全量测试 **360 passed**（46.28s），无回归
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **拒答文本也是模型输出**：refusal 日志不能豁免「模型输出不完整落盘」基线——拒答常引用触发内容（业务敏感数据），必须截断。
- **日志与异常 message 分离**：异常 message 面向调用方（简洁），日志面向诊断（截断前缀）——两套文本独立，避免敏感数据经异常 message 外泄。
