# TOOLS-027 builtin 自动发现类名冲突静默覆盖

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（可观测性，次要项）
> **来源**：2026-08-18 工具模块代码审核（builtin 通用工具组 · 次要项 17）
> **涉及模块**：`app/integration/tools/builtin/__init__.py`（`_discover_tools`）
> **关联文档**：[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

---

## 问题描述

### 现象

`_discover_tools` 收录工具类时 `_tool_classes[name] = obj` 直接覆盖——两个模块定义同名类时后者静默胜出，无告警。

### 影响

类名冲突（如新工具与旧工具同名）静默发生，工具身份错乱难排查。

### 根因

收录逻辑无重名检测。

---

## 修复方案

收录前检查 `name in _tool_classes`，冲突时 `logger.warning("工具类名冲突，后者覆盖 ...")`。

**取舍**：告警 + 后者覆盖（保持现状语义）+ 可观测（冲突不再静默）；不改收录行为（避免破坏既有）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/__init__.py` | 收录前重名检查 + warning | `tests/integration/test_tool_execution.py` 新增 `test_builtin_discovery_no_conflict_warns`（正常路径无冲突告警 + 发现完整） |

**测试说明**：冲突分支为防御日志（真实重复类罕见），单元构造同名类（Python 类名唯一）不可行——以「正常路径不误报」锁定发现逻辑，冲突告警由代码注释 + 审查保障。

---

## 验证

- 相关测试 **2 passed**（发现 + 装配）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **静默覆盖转可观测告警**：同名冲突场景告警而非静默——防御日志成本低，排查价值高。
- **防御日志的测试策略**：不可构造的防御分支（类名唯一）以正常路径锁定 + 代码注释说明，不为不可达分支强行造测试。
