# TOOLS-032 RCA defect 渲染未输出 particle 尺寸（size_um）

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（证据链完整性，次要项）
> **来源**：2026-08-18 工具模块代码审核（RCA 组 · 次要项 4）
> **涉及模块**：`app/integration/tools/builtin/rca/defect_tool.py`
> **关联文档**：[rca.md](../../../docs/integration_doc/tools_doc/builtin_doc/rca.md)

---

## 问题描述

### 现象

`query_defect_map` 渲染未输出 data.py 已采集的 `size_um`——particle 尺寸（1.6-1.9um）是佐证 chamber 污染的证据链一环，却被丢弃。

### 影响

证据链缺失 particle 尺寸，Agent 无法用尺寸佐证污染来源（chamber 粒子污染通常 1-2um 级）。

### 根因

渲染时未带 `size_um` 字段。

---

## 修复方案

defect 渲染行补 `尺寸 {size_um}um`（如 `主导类型=particle（尺寸 1.8um，16:00）`）。

**取舍**：尺寸是证据链关键信号，入内容（Agent 可读）而非仅 metadata。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/rca/defect_tool.py` | 渲染补 `尺寸 {r['size_um']}um` | `tests/unit/test_rca_tools.py` 更新 `test_query_defect_center_cluster_particle`（断言 `尺寸 1.8um`） |

---

## 验证

- 相关测试 **17 passed**（RCA）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **证据链字段全部入内容**：已采集的佐证字段（尺寸）必须进渲染输出——Agent 可读性即证据链完整性。
