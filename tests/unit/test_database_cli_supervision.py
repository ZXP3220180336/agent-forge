"""真实 spawn 的期限与提交事实验收，另有确定性协议竞态复现。"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


def _test_worker(channel, config, migrations, baseline_existing, deadline) -> None:
    """顶层函数允许 Windows spawn；所有挂起最终由外层 subprocess 时限兜底。"""
    scenario = config["scenario"]
    if scenario == "crash":
        os._exit(3)
    if scenario == "bad":
        channel.send_bytes(b"invalid|secret-sentinel")
        channel.close()
        return
    channel.send_bytes(b"commit_started|1")
    channel.send_bytes(b"committed|1")
    if scenario == "success":
        channel.send_bytes(b"finished|ok|1")
        channel.close()
        return
    if scenario == "pending":
        channel.send_bytes(b"commit_started|2")
    time.sleep(30)


@pytest.mark.parametrize("scenario", ["success", "confirmed", "pending", "crash", "bad"])
def test_spawn_worker_is_bounded_and_preserves_facts(scenario: str, tmp_path: Path) -> None:
    """真实 worker 被确认结束；已确认版本不会因超时、异常退出而消失。"""
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP"}
    }
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), scenario],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
        check=False,
    )
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert report["alive"] is None
    assert report["elapsed"] < 7
    assert report["confirmed"] == ([1] if scenario in {"success", "confirmed", "pending"} else [])
    assert report["uncertain"] == (2 if scenario == "pending" else None)
    if scenario == "success":
        assert report["reason"] == "ok"
        assert report["cleanup"] is True
    else:
        assert report["reason"] != "ok"
    assert "secret-sentinel" not in result.stdout + result.stderr


def test_protocol_failure_cannot_be_overwritten_by_late_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """父进程拒绝非法消息后，排空迟到的 finished 不得改写其终态。"""
    from scripts import migrate

    packets = [b"invalid", b"finished|ok|1"]
    reader = SimpleNamespace(
        recv_bytes=lambda maxlength: packets.pop(0),
        poll=lambda: bool(packets),
        close=lambda: None,
    )
    writer = SimpleNamespace(close=lambda: None)
    process = SimpleNamespace(
        sentinel=object(),
        start=lambda: None,
        is_alive=lambda: False,
        join=lambda timeout: None,
        close=lambda: None,
        exitcode=0,
    )
    context = SimpleNamespace(Pipe=lambda duplex: (reader, writer), Process=lambda **kwargs: process)
    monkeypatch.setattr(migrate.multiprocessing, "get_context", lambda mode: context)
    monkeypatch.setattr(migrate, "wait", lambda handles, timeout: [reader])
    progress, alive = migrate._supervise(
        {"migration_total_timeout_seconds": 1.0, "cleanup_timeout_seconds": 0.1},
        (object(),),
        False,
    )
    assert alive is None
    assert progress.reason == "worker_protocol_error"


def test_keyboard_interrupt_cannot_hide_unknown_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """父进程强退时保留“提交结果未知”，不得用终态标签盖掉该事实。"""
    from scripts import migrate

    packets = [b"commit_started|1"]
    reader = SimpleNamespace(
        recv_bytes=lambda maxlength: packets.pop(0),
        poll=lambda: False,
        close=lambda: None,
    )
    writer = SimpleNamespace(close=lambda: None)
    holder = {"alive": True}

    def terminate() -> None:
        holder["alive"] = False

    process = SimpleNamespace(
        sentinel=object(),
        start=lambda: None,
        is_alive=lambda: holder["alive"],
        join=lambda timeout: None,
        terminate=terminate,
        kill=lambda: None,
        close=lambda: None,
        exitcode=1,
    )
    context = SimpleNamespace(Pipe=lambda duplex: (reader, writer), Process=lambda **kwargs: process)
    waits: list[float] = []

    def wait(handles, timeout):
        waits.append(timeout)
        if len(waits) == 1:
            return [reader]
        raise KeyboardInterrupt

    monkeypatch.setattr(migrate.multiprocessing, "get_context", lambda mode: context)
    monkeypatch.setattr(migrate, "wait", wait)
    progress, alive = migrate._supervise(
        {"migration_total_timeout_seconds": 1.0, "cleanup_timeout_seconds": 0.1},
        (object(),),
        False,
    )
    assert alive is None
    assert progress.uncertain_version == 1 and progress.confirmed == []
    assert progress.forced_termination is True
    assert progress.reason == "commit_unknown"


def test_worker_cleanup_cancellation_preserves_main_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """收尾取消不能把先前权限失败改写为通用 worker 失败。"""
    import sqlalchemy.ext.asyncio

    from app.infrastructure import database, database_migrations
    from scripts.migrate import _worker

    packets: list[bytes] = []
    channel = SimpleNamespace(send_bytes=packets.append, close=lambda: None)

    def command_factory(engine, migrations, **kwargs):
        async def run(*, deadline):
            kwargs["progress"].reason = "permission_denied"
            raise asyncio.CancelledError

        return SimpleNamespace(run=run)

    monkeypatch.setattr(database_migrations, "SCHEMA_PREPARER", object())
    monkeypatch.setattr(database_migrations, "MigrationCommand", command_factory)
    monkeypatch.setattr(sqlalchemy.ext.asyncio, "create_async_engine", lambda *args, **kwargs: object())
    monkeypatch.setattr(database, "configure_database_logging", lambda engine: None)
    config = dict.fromkeys(
        (
            "pool_size",
            "max_overflow",
            "pool_timeout_seconds",
            "connect_timeout_seconds",
            "operation_timeout_seconds",
            "migration_timeout_seconds",
            "migration_total_timeout_seconds",
            "cleanup_timeout_seconds",
        ),
        1,
    )
    config.update(url="postgresql+asyncpg://localhost/test", echo=False)
    _worker(channel, config, (), False, time.monotonic() + 1)
    assert packets == [b"finished|permission_denied|0"]


if __name__ == "__main__":
    from scripts.migrate import _supervise

    start = time.monotonic()
    progress, alive = _supervise(
        {"scenario": sys.argv[1], "migration_total_timeout_seconds": 3.0, "cleanup_timeout_seconds": 0.5},
        (None, None),
        False,
        worker=_test_worker,
    )
    print(
        json.dumps(
            {
                "reason": progress.reason,
                "confirmed": progress.confirmed,
                "uncertain": progress.uncertain_version,
                "cleanup": progress.cleanup_complete,
                "alive": alive,
                "elapsed": time.monotonic() - start,
            }
        )
    )
