"""校验 docs/ALIGNMENT.md 与代码、文档、测试的实际对齐。

规则：
- 表格中每个代码路径必须真实存在；
- ✅ 条目必须同时存在非空文档与非空测试；
- 🔶 条目必须有文档，测试可缺失；
- ⬜ 条目必须写明说明，文档/测试可缺失；
- app/ 下所有非空 .py（除 __init__.py）必须在表格中登记。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

STATUS_DONE = "✅"
STATUS_PARTIAL = "🔶"
STATUS_TODO = "⬜"

DOC_EXTENSIONS = {".md", ".py", ".json", ".yml", ".yaml", ".png", ".svg", ".jpg", ".jpeg", ".gif", ".txt"}


@dataclass(frozen=True)
class Entry:
    code: str
    status: str
    doc: str
    test: str
    note: str


def parse_entries(text: str) -> list[Entry]:
    """解析对齐表 Markdown 表格，跳过表头与分隔行。"""
    entries: list[Entry] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        if cells[0].startswith("---") or cells[0] == "代码模块":
            continue
        entries.append(Entry(cells[0], cells[1], cells[2], cells[3], cells[4]))
    return entries


def scan_app_modules(app_root: Path) -> set[str]:
    """返回 app 下 .py 相对路径集合，排除 __init__.py 与 __pycache__。"""
    modules: set[str] = set()
    for path in sorted(app_root.rglob("*.py")):
        if path.name == "__init__.py" or "__pycache__" in path.parts:
            continue
        modules.add(path.relative_to(app_root.parent).as_posix())
    return modules


def check_repo(root: Path = ROOT) -> list[str]:
    """返回全部对齐差距；空列表表示通过。"""
    errors: list[str] = []
    alignment = root / "docs" / "ALIGNMENT.md"
    if not alignment.exists():
        return [f"缺少对齐表: docs/ALIGNMENT.md（相对 {root}）"]

    entries = parse_entries(alignment.read_text(encoding="utf-8"))
    for entry in entries:
        code_path = root / entry.code
        if not code_path.exists():
            errors.append(f"代码路径不存在: {entry.code}")
            continue
        if code_path.stat().st_size == 0 and entry.status != STATUS_TODO:
            errors.append(f"代码为空但状态不是 ⬜: {entry.code}")

        if entry.doc != "(无)":
            doc_path = root / entry.doc
            if not doc_path.exists():
                errors.append(f"文档不存在: {entry.doc}（代码 {entry.code}）")
            elif entry.status == STATUS_DONE and doc_path.stat().st_size == 0:
                errors.append(f"文档为空但状态是 ✅: {entry.doc}（代码 {entry.code}）")

        if entry.test != "(无)":
            test_path = root / entry.test
            if not test_path.exists():
                errors.append(f"测试不存在: {entry.test}（代码 {entry.code}）")
            elif entry.status == STATUS_DONE and test_path.stat().st_size == 0:
                errors.append(f"测试为空但状态是 ✅: {entry.test}（代码 {entry.code}）")

        if entry.status == STATUS_DONE:
            if entry.doc == "(无)":
                errors.append(f"✅ 条目缺少文档: {entry.code}")
            if entry.test == "(无)":
                errors.append(f"✅ 条目缺少测试: {entry.code}")
        elif entry.status == STATUS_PARTIAL:
            if entry.doc == "(无)":
                errors.append(f"🔶 条目缺少文档: {entry.code}")
        elif entry.status == STATUS_TODO:
            if not entry.note:
                errors.append(f"⬜ 条目缺少说明: {entry.code}")
        else:
            errors.append(f"未知状态 {entry.status!r}: {entry.code}")

    registered = {entry.code for entry in entries}
    for rel in scan_app_modules(root / "app"):
        if rel not in registered:
            errors.append(f"app 模块未在 ALIGNMENT.md 登记: {rel}")
    return errors


def check_markdown_links(root: Path = ROOT) -> list[str]:
    """返回文档内失效的相对 Markdown 链接；空列表表示通过。"""
    errors: list[str] = []
    ignored_parts = {".venv", ".git", "__pycache__", "node_modules"}
    for md in sorted(root.rglob("*.md")):
        if any(part in ignored_parts for part in md.parts) or ".pytest-tmp" in md.parts:
            continue
        text = md.read_text(encoding="utf-8")
        for match in re.finditer(r"\]\(([^)]+)\)", text):
            link = match.group(1).strip()
            if not link or link.startswith(("#", "http://", "https://", "mailto:", "data:", "javascript:")):
                continue
            path_part = link.split("#", 1)[0].split("?", 1)[0]
            if not path_part or re.match(r"^[a-zA-Z]:", path_part) or path_part.startswith("/"):
                continue
            if "/" not in path_part and Path(path_part).suffix.lower() not in DOC_EXTENSIONS:
                continue
            target = (md.parent / path_part).resolve()
            if not target.exists():
                errors.append(f"死链: {md.relative_to(root).as_posix()}: {link}")
    return errors


def main() -> int:
    # Windows 控制台默认 GBK，reconfigure 为 UTF-8 避免打印含 emoji/符号时 UnicodeEncodeError
    _reconfigure = getattr(sys.stdout, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    errors = check_repo() + check_markdown_links()
    if errors:
        print("ALIGNMENT 校验失败:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("ALIGNMENT 校验通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
