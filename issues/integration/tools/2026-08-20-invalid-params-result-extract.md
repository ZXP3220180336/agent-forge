# TOOLS-031 工具参数校验失败分支复制 6 次

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（DRY，次要项）
> **来源**：2026-08-18 工具模块代码审核（RCA 组 · 次要项 3）
> **涉及模块**：`app/integration/tools/base.py` + 全部内置 / 示例工具
> **关联文档**：[tools.md](../../../docs/integration_doc/tools_doc/tools.md) · [builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

---

## 问题描述

### 现象

5 个 RCA 工具 + http_api（实际共 11 处，含 search / file_ops / code_exec / web_browse）重复 `if not self.validate_parameters(**kwargs): return ToolResult(success=False, content="", error=f"参数有误: {kwargs!s}")`。

### 影响

校验失败分支的返回格式多处维护，错误格式演进（如脱敏）需改全量调用点。

### 根因

BaseTool 无统一「校验失败结果」辅助方法。

---

## 修复方案

`BaseTool` 增加 `_invalid_params_result(**kwargs)`：返回统一格式的 `ToolResult(success=False, content="", error=f"参数有误: {kwargs!s}")`；全部工具校验兜底改调 `self._invalid_params_result(**kwargs)`。

**取舍**：单一结果工厂（DRY）；错误格式演进只改 base.py 一处。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/base.py` | 新增 `_invalid_params_result` | 现有工具测试覆盖校验失败路径 |
| 全部工具（http_api / search / file_ops / code_exec / web_browse / rca 5 工具） | 校验失败分支改调 `_invalid_params_result` | 相关测试 **72 passed** |

---

## 验证

- 相关测试 **72 passed**（工具全量）
- 残留检查：`参数有误` 仅存在于 base.py 定义（代码零残留）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **失败分支也抽统一工厂**：不只成功路径，错误返回同样 DRY——格式演进一处改全量生效。
