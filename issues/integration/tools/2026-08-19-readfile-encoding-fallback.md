# TOOLS-021 readFile 仅 UTF-8 解码无 GBK 回退，中文文件乱码

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P3（正确性，次要项）
> **来源**：2026-08-18 工具模块代码审核（builtin 通用工具组 · 次要项 10）
> **涉及模块**：`app/integration/tools/builtin/file_ops.py`（ReadFileTool）+ `app/shared/encoding.py`（新增共享）
> **关联文档**：[encoding.md](../../../docs/shared_doc/encoding.md) · [builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

---

## 问题描述

### 现象

`ReadFileTool` 读取仅按 UTF-8 解码，Windows 中文环境 GBK 文本文件（良率数据 / 报告）乱码——与 code_exec 同源编码问题（TOOLS-005）。

### 影响

RCA 场景 GBK 数据文件经 readFile 读取后乱码，证据链数据失真。

### 根因

readFile 未复用 code_exec 的双编码解码逻辑（UTF-8 优先 + locale 回退）。

---

## 修复方案

**抽取共享**：新建 `app/shared/encoding.py`（`decode_output`），code_exec 与 readFile 复用：

- `code_exec`：删除本地 `_decode_output`，import 共享 `decode_output`（行为不变）；
- `file_ops.readFile`：分段 / 正常路径 decode 改 `decode_output`（UTF-8 优先 + GBK 回退）。

**取舍**：共享模块（DRY + 一处维护）优于两工具各自内联；编码逻辑归 shared 层（非安全横切，不占 security.py）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/shared/encoding.py`（新） | `decode_output`（从 code_exec 移来） | 现有 `test_code_exec_decode_output_*`（import 改 shared）+ file 测试覆盖 |
| `app/integration/tools/builtin/code_exec.py` | 删本地 `_decode_output`；import + 调用共享 `decode_output` | `test_tool_execution.py` 解码测试通过 |
| `app/integration/tools/builtin/file_ops.py` | readFile decode 改 `decode_output` | file 测试通过 |
| 文档 | `docs/shared_doc/encoding.md`（新，模块导航）；[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md) 输出解码描述改共享引用 | — |

---

## 验证

- 相关测试 **46 passed**（code_exec / file / loader）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **编码逻辑共享一处**：code_exec 与 readFile 同源编码问题（GBK），抽取到 shared 层复用——DRY 且修复一处全局生效。
- **模块归属清晰**：编码工具归 shared（通用工具）而非 security（安全横切），避免语义错位。
