"""数据库迁移 CLI 的离线功能契约；占位脚本不能冒充可用入口。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("module", ["scripts.migrate", "scripts.init_db"])
@pytest.mark.xfail(strict=True, raises=AssertionError, reason="DB-F03：迁移与初始化 CLI 尚未实现")
def test_database_cli_help_exposes_baseline_without_database(module: str, tmp_path: Path) -> None:
    """两个正式入口均应无连接地提供共享迁移参数帮助。"""
    project_root = Path(__file__).resolve().parents[2]
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}
    }
    env.update(
        PYTHONPATH=str(project_root),
        PYTHONIOENCODING="utf-8",
        DATABASE_URL="invalid://cli-help-must-not-connect",
    )
    result = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, "帮助入口必须无数据库依赖地成功退出"
    assert "--baseline-existing" in result.stdout, "占位脚本未提供批准的迁移 CLI 帮助"
