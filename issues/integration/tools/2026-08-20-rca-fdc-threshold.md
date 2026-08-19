# TOOLS-036 RCA FDC 偏离判定阈值无显式规则

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（契约显式化，次要项）
> **来源**：2026-08-18 工具模块代码审核（RCA 组 · 次要项 8）
> **涉及模块**：`app/integration/tools/builtin/rca/data.py`（FDC_PARAMS）
> **关联文档**：[rca.md](../../../docs/integration_doc/tools_doc/builtin_doc/rca.md)

---

## 问题描述

### 现象

FDC `status` 偏离阈值无显式规则：12:00 `+4.9%` 判 normal、13:00 `+8.9%` 判 deviated——阈值语义（约 5%）藏在模拟数据里。

### 影响

未来接真实数据源时偏离判定需明确定义；当前规则不可见，维护者无法复现 / 承接。

### 根因

阈值规则未文档化。

---

## 修复方案

data.py FDC_PARAMS 上方注释显式记录判定阈值约定：`|deviation_pct| >= 5% 判 deviated，< 5% 判 normal`（模拟数据按此生成，接真实数据源时判定逻辑由此规则承接）。

**取舍**：阈值约定文档化（数据侧契约），fdc_tool 渲染依赖数据 status 不变。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/rca/data.py` | FDC_PARAMS 上方补阈值约定注释 | RCA 测试 **17 passed**（无逻辑改动） |

---

## 验证

- 相关测试 **17 passed**（RCA）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **模拟数据阈值要显式契约化**：判定阈值藏在数据里不可见，接真实数据源时无规则可循——注释记录约定，规则可见可承接。
