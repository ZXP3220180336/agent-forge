# TOOLS-034 RCA metadata timestamp 锚点语义不一致

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（证据链一致性，次要项）
> **来源**：2026-08-18 工具模块代码审核（RCA 组 · 次要项 6）
> **涉及模块**：`app/integration/tools/builtin/rca/`（yield / alerts / fdc / defect 工具）
> **关联文档**：[rca.md](../../../docs/integration_doc/tools_doc/builtin_doc/rca.md)

---

## 问题描述

### 现象

metadata `timestamp` 锚点语义不一致：yield 用 `records[-1]`（最新），alerts / fdc / defect 用 `records[0]`（最早）——时间窗口查询时「代表性时间戳」口径不统一，跨工具证据链聚合易误解。

### 影响

证据链各工具的 timestamp 含义漂移（最新 vs 最早），聚合分析口径混乱。

### 根因

各工具 metadata 锚点取值未统一约定。

---

## 修复方案

统一为**最新记录时间戳**（`records[-1]`，结果集最近时间点），yield 保持、alerts / fdc / defect 改之；代码注释固定语义。

**取舍**：统一 latest——良率（当前状态）、告警 / FDC（窗口内最近事件）、缺陷（最近采样）均以「结果集最近时间点」为证据锚点，跨工具一致。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `rca/alerts_tool.py` / `rca/fdc_tool.py` / `rca/defect_tool.py` | metadata timestamp `records[0]` → `records[-1]` + 注释固定口径 | RCA 测试 **17 passed**（无 timestamp 断言，行为口径统一） |

---

## 验证

- 相关测试 **17 passed**（RCA）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **跨工具 metadata 口径要统一**：证据链字段（timestamp）语义跨工具漂移会让聚合失真——统一「最近时间点」锚点并注释固定。
