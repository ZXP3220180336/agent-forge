"""verify_alignment 校验逻辑的单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import verify_alignment  # noqa: E402


def _make_repo(tmp_path: Path, rows: list[str], files: dict[str, str]) -> Path:
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    header = "| 代码模块 | 状态 | 文档 | 测试 | 说明 |\n| --- | --- | --- | --- | --- |\n"
    alignment = tmp_path / "docs" / "ALIGNMENT.md"
    alignment.parent.mkdir(parents=True, exist_ok=True)
    alignment.write_text(header + "\n".join(rows), encoding="utf-8")
    return tmp_path


def _row(
    code: str,
    status: str = "✅",
    doc: str = "docs/x_doc/x.md",
    test: str = "tests/unit/test_x.py",
    note: str = "",
) -> str:
    return f"| {code} | {status} | {doc} | {test} | {note} |"


def _done_files() -> dict[str, str]:
    return {
        "app/x.py": "def f():\n    return 1\n",
        "docs/x_doc/x.md": "# X\n",
        "tests/unit/test_x.py": "def test_x():\n    pass\n",
    }


def test_valid_done_passes(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, [_row("app/x.py")], _done_files())
    assert verify_alignment.check_repo(repo) == []


def test_done_requires_test_and_doc(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        [_row("app/x.py", test="(无)", doc="(无)")],
        {"app/x.py": "def f():\n    return 1\n"},
    )
    errors = verify_alignment.check_repo(repo)
    assert any("缺少文档" in error for error in errors)
    assert any("缺少测试" in error for error in errors)


def test_partial_requires_doc_allows_missing_test(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        [_row("app/x.py", status="🔶", test="(无)")],
        {
            "app/x.py": "def f():\n    return 1\n",
            "docs/x_doc/x.md": "# X\n",
        },
    )
    assert verify_alignment.check_repo(repo) == []


def test_todo_allows_empty_code_with_note(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        [_row("app/x.py", status="⬜", doc="(无)", test="(无)", note="空文件待实现")],
        {"app/x.py": ""},
    )
    assert verify_alignment.check_repo(repo) == []


def test_todo_requires_note(tmp_path: Path) -> None:
    repo = _make_repo(
        tmp_path,
        [_row("app/x.py", status="⬜", doc="(无)", test="(无)", note="")],
        {"app/x.py": ""},
    )
    errors = verify_alignment.check_repo(repo)
    assert any("缺少说明" in error for error in errors)


def test_empty_code_with_done_status_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, [_row("app/x.py")], _done_files() | {"app/x.py": ""})
    errors = verify_alignment.check_repo(repo)
    assert any("代码为空但状态不是" in error for error in errors)


def test_empty_test_for_done_fails(tmp_path: Path) -> None:
    files = _done_files()
    files["tests/unit/test_x.py"] = ""
    repo = _make_repo(tmp_path, [_row("app/x.py")], files)
    errors = verify_alignment.check_repo(repo)
    assert any("测试为空但状态是" in error for error in errors)


def test_unregistered_module_fails(tmp_path: Path) -> None:
    files = _done_files() | {"app/unregistered.py": "y = 2\n"}
    repo = _make_repo(tmp_path, [_row("app/x.py")], files)
    errors = verify_alignment.check_repo(repo)
    assert any("未在 ALIGNMENT.md 登记" in error for error in errors)


def test_missing_code_path_fails(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, [_row("app/missing.py")], {})
    errors = verify_alignment.check_repo(repo)
    assert any("代码路径不存在" in error for error in errors)


def test_current_repo_passes() -> None:
    assert verify_alignment.check_repo(verify_alignment.ROOT) == []


def test_link_checker_detects_broken_link(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir(parents=True)
    (tmp_path / "docs" / "a.md").write_text("[x](../missing.md)\n", encoding="utf-8")
    errors = verify_alignment.check_markdown_links(tmp_path)
    assert any("docs/a.md" in error and "missing.md" in error for error in errors)


def test_link_checker_ignores_url_examples_and_anchors(tmp_path: Path) -> None:
    (tmp_path / "docs").mkdir(parents=True)
    (tmp_path / "docs" / "a.md").write_text("[text](url)\n[anchor](#section)\n", encoding="utf-8")
    assert verify_alignment.check_markdown_links(tmp_path) == []


def test_current_repo_links_pass() -> None:
    assert verify_alignment.check_markdown_links(verify_alignment.ROOT) == []
