"""共享编码工具：字节输出的双编码解码（UTF-8 优先 + 系统 locale 回退）。

供 code_exec（子进程输出）/ readFile（文件内容）复用——Windows 中文环境
系统命令 / GBK 文本文件输出按 UTF-8 硬解码会乱码，双解码覆盖两类来源。
"""

import locale


def decode_output(raw: bytes) -> str:
    """解码字节输出：优先 UTF-8（现代工具 / Python 脚本），非法字节回退系统 locale 编码。

    Windows 中文环境 `locale.getpreferredencoding(False)` 返回 cp936（GBK），
    匹配 cmd 系统命令 / GBK 文本文件的默认输出编码。
    """
    if not raw:
        return ""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        encoding = locale.getpreferredencoding(False) or "utf-8"
        return raw.decode(encoding, errors="replace")
