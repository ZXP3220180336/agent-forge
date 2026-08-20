# TOOLS-039 rca.md 文档状态与代码不符（证据链 metadata + 测试数）

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（文档同步，次要项）
> **来源**：2026-08-20 工具模块文档↔代码状态审核（A 类 #2 / #6）
> **涉及模块**：`docs/integration_doc/tools_doc/builtin_doc/rca.md`
> **关联文档**：[rca.md](../../../docs/integration_doc/tools_doc/builtin_doc/rca.md)

---

## 问题描述

### 现象

1. 设计目标 3 写「证据链 metadata：每个工具结果带 `source` / 查询键 / `timestamp`」——但 `search_historical_rca` 实际 metadata 为 `{source, query, top_k, top_confidence}`，**无 timestamp 字段**（`history_tool.py:87-92`）；仅该工具不符，其余 4 工具均带 timestamp。
2. 测试状态写 `tests/unit/test_rca_tools.py`（**15 用例**），实际 **17 个**——漏列 `_in_range` 单侧纯时间与混合窗口 2 个纯函数测试。

### 影响

证据链字段是产品「证据链」亮点的可引用契约，描述失真误导 Agent 集成方与测试断言；测试用例数失真。

### 根因

TOOLS-034（timestamp 锚点统一）后仅 yield/alerts/fdc/defect 四工具带 timestamp，history 工具本身无此字段，文档未同步；`_in_range` 单侧测试新增后未更新计数。

---

## 修复方案

- 设计目标 3 修正：`search_historical_rca` 以 `top_confidence` 替代 timestamp（metadata 为 source / query / top_k / top_confidence），其余 4 工具带 timestamp 锚点
- 测试状态改 17 用例，补充时间窗口过滤（FDC 偏离发展、yield 过滤、单侧缺省、end 短格式、`_in_range` 单侧纯时间与混合窗口）

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `docs/integration_doc/tools_doc/builtin_doc/rca.md` | 设计目标 3 证据链字段修正；测试数 15→17 | `tests/unit/test_rca_tools.py`（17 用例） |

## 验证

- `tests/unit/test_rca_tools.py` 17 用例全过；全量 **542 passed**
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

## 教训沉淀

- **契约描述须逐工具核对**：「每个工具都 X」这类泛化表述，遇到一个例外即失真——RCA 5 工具 metadata 字段本就不同，应按工具级契约描述。
- **计数器同步**：新增纯函数测试（`_in_range`）后同步文档用例数。
