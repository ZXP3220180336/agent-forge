# TOOLS-041 registry.md 测试覆盖表述夸大 + 注销/重名无测试

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（测试缺口 + 文档同步，次要项）
> **来源**：2026-08-20 工具模块文档↔代码状态审核（A 类 #3）
> **涉及模块**：`docs/integration_doc/tools_doc/registry.md` + `tests/unit/test_tool_registry_metadata.py`
> **关联文档**：[registry.md](../../../docs/integration_doc/tools_doc/registry.md)

---

## 问题描述

### 现象

registry.md 测试节写「注册 / 注销 / 重名路径经 `test_tools.py` 与集成测试间接覆盖」——但全 `tests/` 目录**没有任何 `.unregister(` 调用**，也没有触发「工具 '...' 已存在」`ValueError` 的重名注册用例。

### 影响

文档夸大了测试覆盖，且**真实存在测试缺口**：注销（返回 True/False）与重名注册拒绝两条核心行为无测试保护，后续重构可能静默破坏。

### 根因

注册中心拆分为独立组件时补了元数据过滤测试，注销 / 重名路径未补；文档以「间接覆盖」为其背书。

---

## 修复方案

1. **补测试**（`tests/unit/test_tool_registry_metadata.py` 3→6 用例）：注销已注册工具返回 True 且移除；注销不存在返回 False 不抛异常；重名注册抛 `ValueError` 且不覆盖原实例
2. **文档修正**：测试数改 6 用例，如实列出新增覆盖

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `tests/unit/test_tool_registry_metadata.py` | +3 用例（注销 / 注销不存在 / 重名拒绝），补 `pytest` 导入 | 6 用例全过 |
| `docs/integration_doc/tools_doc/registry.md` | 测试节改 6 用例 + 覆盖描述修正 | — |

## 验证

- `tests/unit/test_tool_registry_metadata.py` 6 用例全过；全量 **542 passed**（+3）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

## 教训沉淀

- **文档不得声称不存在的测试覆盖**：「间接覆盖」若无真实断言即是虚构——写覆盖描述前 grep 测试目录核实。
- **核心行为必须有直接测试**：注册 / 注销 / 重名是容器最基础契约，应直接测试而非依赖间接覆盖。
