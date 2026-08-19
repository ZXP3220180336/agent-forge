# TOOLS-005 code_exec 子进程输出按 UTF-8 硬解码，Windows 中文环境 GBK 乱码

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P1（正确性，产品主链路中文数据直接失真）
> **来源**：2026-08-18 工具模块代码审核（builtin 通用工具组 · 重要项 5）
> **涉及模块**：`app/integration/tools/builtin/code_exec.py`（CodeExecTool 输出解码）
> **关联文档**：[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

---

## 问题描述

### 现象

运行环境为 win32，`create_subprocess_shell` 走 cmd.exe（默认 GBK / cp936）。子进程输出（良率日志、`type 报告.txt` 中文内容）按 `utf-8` 解码时非 ASCII 字节变替换符乱码——`errors="replace"` 只防崩溃不保内容，产品主链路的良率数据直接失真。

### 影响

RCA 场景中文日志 / 报告经 code_exec 读取后乱码，Agent 拿到的观察结果不可用；错误信息同样失真，排查链被污染。

### 根因

固定按 UTF-8 解码，未考虑 Windows 系统命令（cmd）默认输出编码为 GBK；`errors="replace"` 把「解码错误」静默替换为 `�`，掩盖了编码不匹配。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| Windows 编码事实 | cmd.exe 默认 ANSI 代码页（zh-CN = cp936）；Python 3 脚本 / 现代工具输出默认 UTF-8——**单编码无法同时覆盖两类输出源** |
| `locale.getpreferredencoding(False)` | 返回系统 ANSI 代码页，匹配 cmd 系统命令输出编码 |
| 双解码回退实践（chardet 的轻量版） | 优先常见 UTF-8（strict 校验），失败再回退 locale 编码——无第三方依赖的可判定方案 |

**核心**：优先 UTF-8（现代工具），非法字节回退系统 locale 编码（系统命令 GBK）——`strict` 校验保证「回退发生在真乱码时」。

---

## 修复方案（含决策取舍）

**决策**：新增 `_decode_output(raw)` 双解码：

```python
def _decode_output(raw: bytes) -> str:
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")          # 优先 UTF-8（现代工具 / Python 脚本）
    except UnicodeDecodeError:
        encoding = locale.getpreferredencoding(False) or "utf-8"
        return raw.decode(encoding, errors="replace")  # 回退系统 locale（Windows 中文 = cp936）
```

execute 中 stdout/stderr 统一改调 `_decode_output`。

**取舍理由**：

1. **UTF-8 优先 + strict**：合法 UTF-8（Python 脚本、现代工具）走正确路径，无性能损失；`strict` 保证只有真非法字节才回退，不乱猜；
2. **回退 locale 编码**：对齐审查建议「按 `locale.getpreferredencoding(False)` 解码」，覆盖 cmd 系统命令 GBK 输出；
3. **不引入第三方检测库**（chardet）：双编码回退覆盖产品主场景（现代工具 + 系统命令），成本最低。

**语义边界**：

- GBK 字节序列恰为合法 UTF-8 的边界无法完美区分（概率低，产品可接受）；
- 非 GBK 环境的系统命令输出由该环境 locale 编码解码（可移植）；
- 空输出返回 `""`（保持原行为）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/code_exec.py` | `import locale`；新增 `_decode_output`（UTF-8 strict 优先 + locale 回退）；execute 两处 decode 改调 `_decode_output` | `test_tool_execution.py` 新增 `test_code_exec_decode_output_utf8`（参数化 3 例）+ `test_code_exec_decode_output_gbk_fallback`（GBK 输出回退，skipif 非 GBK locale） |
| 文档 | [builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md) code_exec 实现要点「输出解码」更新为双解码说明 | — |

---

## 验证

- 相关测试 **8 passed**（含 UTF-8 / GBK 回退 / 空输出用例）
- 全量测试待提交前确认（增量改动：仅解码路径，无回归面）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **`errors="replace"` 不是解码方案**：它只防崩溃，把编码不匹配静默变成替换符乱码，掩盖根因——应显式处理编码（回退 / 检测）。
- **双编码回退是 Windows 中文环境的必要项**：系统命令（GBK）与 Python/现代工具（UTF-8）并存，单编码必然有一方乱码。
- **`strict` 校验是回退的判据**：用 UnicodeDecodeError 区分「合法该编码」与「不是该编码」，避免替换符污染后才回退。
