# TOOLS-033 RCA alerts 渲染未输出 alert_id 可引用标识

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（证据链完整性，次要项）
> **来源**：2026-08-18 工具模块代码审核（RCA 组 · 次要项 5）
> **涉及模块**：`app/integration/tools/builtin/rca/alerts_tool.py`
> **关联文档**：[rca.md](../../../docs/integration_doc/tools_doc/builtin_doc/rca.md)

---

## 问题描述

### 现象

`query_equipment_alerts` 渲染未输出 data.py 的 `alert_id`（ALM-1001），证据链报告中可引用的唯一标识缺失——告警记录无法被报告 / 后续排查引用。

### 影响

证据链告警条目无唯一引用（ALM-xxx），跨工具聚合时无法指代具体告警。

### 根因

渲染时未带 `alert_id`。

---

## 修复方案

告警渲染行补 `[alert_id]` 前缀（如 `- [ALM-1001] [ALARM/HIGH] ETCH-01: ...`）。

**取舍**：`alert_id` 是报告可引用标识，入内容（Agent 可引用）而非仅 metadata。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/rca/alerts_tool.py` | 渲染补 `[{r['alert_id']}]` 前缀 | `tests/unit/test_rca_tools.py` 更新 `test_query_alerts_filters_by_equipment`（断言 `[ALM-1001]`） |

---

## 验证

- 相关测试 **17 passed**（RCA）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **证据链条目需可引用标识**：告警 / 记录的唯一 ID（alert_id）必须进渲染输出，报告才能指代具体条目。
