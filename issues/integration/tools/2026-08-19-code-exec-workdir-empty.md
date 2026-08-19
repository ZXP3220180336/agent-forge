# TOOLS-022 code_exec workdir 空串传 cwd="" 抛异常

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P3（边界条件，次要项）
> **来源**：2026-08-18 工具模块代码审核（builtin 通用工具组 · 次要项 11）
> **涉及模块**：`app/integration/tools/builtin/code_exec.py`（CodeExecTool.execute）
> **关联文档**：[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

---

## 问题描述

### 现象

`workdir` 传空串时 `create_subprocess_shell(cwd="")` 抛异常被兜底捕获（归因「命令不存在或未找到可执行文件」等），LLM 传空 workdir 期望「用项目根」却得到失败。

### 影响

空 workdir 的合法调用误报失败，归因误导。

### 根因

`kwargs.get("workdir")` 直接透传，空串未归一为 None。

---

## 修复方案

`workdir: str | None = kwargs.get("workdir") or None`——空串归一为 None（`cwd=None` 用当前进程目录），非空路径行为不变。

**取舍**：`or None` 同时处理空串与 None 两态，最小改动。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/code_exec.py` | `workdir = kwargs.get("workdir") or None` + 注释 | `tests/integration/test_tool_execution.py` 新增 `test_code_exec_empty_workdir_ok`（空串正常执行） |
| 文档 | [builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md) 无需改（参数描述已含「留空用项目根」） | — |

---

## 验证

- 相关测试 **9 passed**（code_exec）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **可选字符串参数空串要归一**：`kwargs.get(x) or None` 统一空串与 None 两态，避免空串进入底层 API 抛错。
