"""数据库迁移 CLI 的离线功能契约；占位脚本不能冒充可用入口。"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _run_cli(module: str, args: list[str], cwd: Path, url: str) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}
    }
    env.update(PYTHONPATH=str(Path(__file__).resolve().parents[2]), PYTHONIOENCODING="ascii", DATABASE_URL=url)
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )


@pytest.mark.parametrize("module", ["scripts.migrate", "scripts.init_db"])
def test_invalid_configuration_is_redacted(module: str, tmp_path: Path) -> None:
    """非法配置只返回稳定原因，不显示凭证或 traceback。"""
    result = _run_cli(module, [], tmp_path, "invalid://user:secret-sentinel@localhost/db")
    assert result.returncode != 0
    assert json.loads(result.stdout)["reason"] == "configuration_invalid"
    assert "secret-sentinel" not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.parametrize("module", ["scripts.migrate", "scripts.init_db"])
def test_invalid_arguments_are_redacted(module: str, tmp_path: Path) -> None:
    """误传凭证的参数不会由 argparse 原样回显。"""
    result = _run_cli(module, ["--secret-sentinel"], tmp_path, "invalid://unused")
    assert result.returncode == 2
    assert result.stderr == "migration: arguments_invalid\n"
    assert "secret-sentinel" not in result.stdout + result.stderr


@pytest.mark.parametrize("module", ["scripts.migrate", "scripts.init_db"])
def test_baseline_requires_valid_files_before_connection(module: str, tmp_path: Path) -> None:
    """baseline 不能绕过文件校验；连接前拒绝不存在的迁移目录。"""
    result = _run_cli(
        module,
        ["--baseline-existing", "--migrations-dir", str(tmp_path / "missing")],
        tmp_path,
        "postgresql+asyncpg://localhost/gate_test",
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["reason"] == "migration_files_unavailable"
    assert report["confirmed_versions"] == []
    assert report["worker_pid"] is None


def test_init_db_delegates_same_main() -> None:
    """初始化入口直接复用迁移函数，没有另一个执行实现。"""
    from scripts import init_db, migrate

    assert init_db.main is migrate.main


def test_command_budget_is_built_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """命令预算直接由 Settings 导出的键构造；键名或取值漂移会在这里失败。"""
    from app.config.settings import Settings
    from scripts.migrate import _command_timeouts

    for key in list(os.environ):
        if key.lower().startswith("database_"):
            monkeypatch.delenv(key)
    settings = Settings(
        _env_file=None,
        database_migration_timeout_seconds=12.5,
        database_migration_total_timeout_seconds=34.5,
    )
    timeouts = _command_timeouts(settings.database_config)
    assert timeouts.file_timeout_seconds == 12.5
    assert timeouts.total_timeout_seconds == 34.5
    assert timeouts.cleanup_timeout_seconds == settings.database_cleanup_timeout_seconds


def test_missing_schema_gate_never_starts_worker_or_engine(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """门禁不完整时既不创建 engine，也不启动可产生副作用的 worker。"""
    import sqlalchemy.ext.asyncio

    from app.infrastructure import database_migrations
    from scripts import migrate

    def forbidden(*args, **kwargs):
        raise AssertionError("schema gate must prevent database access")

    monkeypatch.setattr(database_migrations, "SCHEMA_PREPARER", None)
    monkeypatch.setattr(migrate, "_supervise", forbidden)
    monkeypatch.setattr(sqlalchemy.ext.asyncio, "create_async_engine", forbidden)
    assert migrate.main(["--baseline-existing", "--migrations-dir", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "schema_gate_unavailable"


def test_default_migration_directory_is_anchored_to_project(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """开启门禁后仍以项目路径发现迁移，不能跟随调用者工作目录漂移。"""
    from app.infrastructure import database_migrations
    from scripts import migrate

    observed = []

    def supervise(config, migrations, baseline_existing):
        observed.append((migrations, baseline_existing))
        return database_migrations.MigrationProgress(reason="ok", cleanup_complete=True), None

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(database_migrations, "SCHEMA_PREPARER", object())
    monkeypatch.setattr(migrate, "_supervise", supervise)
    assert migrate.main(["--baseline-existing"]) == 0
    assert observed == [(Path(migrate.__file__).resolve().parents[1] / "migrations", True)]
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


@pytest.mark.parametrize("module", ["scripts.migrate", "scripts.init_db"])
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
        PYTHONIOENCODING="ascii",
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
    assert "严格验证" in result.stdout
