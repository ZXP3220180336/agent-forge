# TOOLS-030 RCA time_range 过滤逻辑三处重复

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（DRY，次要项）
> **来源**：2026-08-18 工具模块代码审核（RCA 组 · 次要项 2）
> **涉及模块**：`app/integration/tools/builtin/rca/data.py`（查询函数）
> **关联文档**：[rca.md](../../../docs/integration_doc/tools_doc/builtin_doc/rca.md)

---

## 问题描述

### 现象

`time_range` 过滤逻辑三处重复：`query_yield` 内联 `_in_range`、`query_alerts` / `query_fdc` 各含 `if time_range:` 守卫——过滤语义散落，未来修改（如 `_in_range` 补日期）易漏改一处。

### 影响

过滤逻辑多处维护，规则演进时 DRY 违约导致行为漂移。

### 根因

未抽取统一过滤入口。

---

## 修复方案

抽取 `_apply_time_range(records, time_range)`：空 time_range 原样返回，否则逐条 `_in_range` 过滤；三处查询统一调用（`query_yield` 改为先按 batch 过滤再套用）。

**取舍**：单一过滤入口（DRY），行为不变（`_in_range` 语义由 TOOLS-029 统一）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/rca/data.py` | 新增 `_apply_time_range`；`query_yield` / `query_alerts` / `query_fdc` 统一调用 | 现有 RCA 测试 **17 passed**（time_range 行为不变） |
| 文档 | 无需改（契约未变） | — |

---

## 验证

- 相关测试 **17 passed**（RCA）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **过滤语义单一入口**：多查询共用的过滤逻辑抽统一函数，规则演进（如补日期）一处修改全局生效，杜绝漏改漂移。
