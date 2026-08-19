# TOOLS-029 RCA _in_range 单边纯时间过滤静默失效

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（正确性，次要项）
> **来源**：2026-08-18 工具模块代码审核（RCA 组 · 次要项 1）
> **涉及模块**：`app/integration/tools/builtin/rca/data.py`（`_in_range`）
> **关联文档**：[rca.md](../../../docs/integration_doc/tools_doc/builtin_doc/rca.md)

---

## 问题描述

### 现象

`_in_range` 的「缺日期补对端日期」只在 start / end 两段都存在时生效；单边仅时间输入（`'08:00~'` / `'~20:00'` / `'08:00~20:00'`）静默失效——纯时间与全日期字符串字典序比较无意义（`'2026-08-12 08:00' < '08:00'` 恒 False，等于不过滤；双时间端则全部命中返回 False）。

### 影响

Agent 用 LLM 直觉写法（`'08:00~20:00'`）过滤时间窗口时静默无效，FDC / 告警数据过滤失真。

### 根因

单边纯时间未补日期，与全日期 timestamp 比较无意义。

---

## 修复方案

单边 / 双边纯时间用**记录自身日期**补全（当天窗口）：

```python
if start and " " not in start:
    start = timestamp[:10] + " " + start  # 单边纯时间 → 补记录当日日期
if end and " " not in end:
    end = timestamp[:10] + " " + end
```

- 一端完整另一端缺日期 → 仍补对端日期（原语义保留）；
- 单边 / 双边纯时间 → 补记录当日日期（`'08:00~20:00'` 匹配当天窗口）。

**取舍**：补记录自身日期（非固定「今天」）——模拟数据为固定时间戳，记录自身日期语义最合理。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/rca/data.py` | `_in_range` 单边纯时间补记录日期 + docstring | `tests/unit/test_rca_tools.py` 新增 2 用例：`test_in_range_single_time_only`（单边/双边纯时间窗口）+ `test_in_range_mixed_full_and_time`（一端完整日期原语义保留） |
| 文档 | [rca.md](../../../docs/integration_doc/tools_doc/builtin_doc/rca.md) 无需改（time_range 契约未变，仅补全逻辑） | — |

---

## 验证

- 相关测试 **17 passed**（RCA，含 2 个新增）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **同格式字符串比较需两端格式对齐**：纯时间与全日期字典序比较无意义——单边缺日期必须补全，否则过滤静默失效。
- **LLM 直觉写法要鲁棒**：`'08:00~20:00'` 是 Agent 高频写法，契约必须支持（补当日日期），否则主链路数据过滤失真。
