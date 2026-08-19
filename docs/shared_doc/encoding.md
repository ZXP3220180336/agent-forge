# 共享编码工具（app/shared/encoding.py）

> **模块**：`app/shared/encoding.py`
> **定位**：共享层编码工具 —— 字节输出的双编码解码（UTF-8 优先 + 系统 locale 回退）

## 职责

`decode_output(raw: bytes) -> str`：解码字节输出——优先 UTF-8（现代工具 / Python 脚本输出），非法字节回退系统 locale 编码（Windows 中文环境 `locale.getpreferredencoding(False)` = cp936，匹配 cmd 系统命令 / GBK 文本文件）。

## 消费方

| 消费方 | 场景 |
| --- | --- |
| `code_exec` | 子进程 stdout/stderr 输出解码 |
| `readFile` | 文件内容解码（GBK 文本文件不乱码） |

## 相关文档

- [问题记录](../../issues/integration/tools/2026-08-19-code-exec-gbk-decode.md)（TOOLS-005 双解码来源）
- [builtin 工具说明](../integration_doc/tools_doc/builtin_doc/builtin.md)（code_exec / readFile 解码消费）
