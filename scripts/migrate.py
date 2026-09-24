"""唯一离线迁移 CLI；父进程监督期限，单 worker 独占数据库资源。"""

import argparse
import asyncio
import json
import multiprocessing
import os
import sys
import time
from multiprocessing.connection import Connection, wait
from pathlib import Path
from typing import Any


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse 默认回显非法参数，参数可能包含误传的凭证。
        self.exit(2, "migration: arguments_invalid\n")


def _command_timeouts(config: dict[str, Any]) -> Any:
    """把 Settings 导出的迁移预算映射成命令预算；配置键漂移在这里立即失败。"""
    from app.infrastructure.database_migrations import MigrationTimeouts

    return MigrationTimeouts(
        file_timeout_seconds=config["migration_timeout_seconds"],
        total_timeout_seconds=config["migration_total_timeout_seconds"],
        cleanup_timeout_seconds=config["cleanup_timeout_seconds"],
    )


def _worker(
    channel: Connection,
    config: dict[str, Any],
    migrations: tuple | Path,
    baseline_existing: bool,
    deadline: float,
) -> None:
    """子进程只传稳定事实；异常不交给 multiprocessing 打印原始 traceback。"""
    progress = None
    safe_reasons = frozenset()
    try:
        from app.infrastructure.database import configure_database_logging
        from app.infrastructure.database_migrations import (
            MIGRATION_REASONS,
            SCHEMA_PREPARER,
            MigrationCommand,
            MigrationProgress,
            load_migrations,
        )

        safe_reasons = MIGRATION_REASONS
        progress = MigrationProgress(channel=channel)
        if SCHEMA_PREPARER is None:
            progress.reason = "schema_gate_unavailable"
            progress.cleanup_complete = True
        else:
            from sqlalchemy.ext.asyncio import create_async_engine

            snapshot = load_migrations(migrations) if isinstance(migrations, Path) else migrations
            if time.monotonic() >= deadline:
                progress.reason = "timeout"
                progress.cleanup_complete = True
                return

            async def execute() -> None:
                engine = create_async_engine(
                    config["url"],
                    pool_size=config["pool_size"],
                    max_overflow=config["max_overflow"],
                    pool_timeout=config["pool_timeout_seconds"],
                    pool_pre_ping=True,
                    echo=config["echo"],
                    hide_parameters=True,
                    logging_name=f"migration_{os.getpid()}",
                    pool_logging_name=f"migration_{os.getpid()}",
                    connect_args={
                        "timeout": config["connect_timeout_seconds"],
                        "command_timeout": config["operation_timeout_seconds"],
                    },
                )
                configure_database_logging(engine)
                command = MigrationCommand(
                    engine,
                    snapshot,
                    timeouts=_command_timeouts(config),
                    prepare_schema=SCHEMA_PREPARER,
                    baseline_existing=baseline_existing,
                    progress=progress,
                )
                await command.run(deadline=deadline)

            asyncio.run(execute())
    except BaseException as error:  # noqa: BLE001 — 最外层只传固定失败，不泄露配置或驱动异常链。
        if progress is not None:
            if progress.uncertain_version is not None:
                progress.reason = "commit_unknown"
            elif progress.reason in {"starting", "ok"}:
                progress.reason = str(error) if str(error) in safe_reasons else "worker_failed"
    finally:
        try:
            if progress is not None:
                progress.finish()
            else:
                channel.send_bytes(b"finished|worker_failed|0")
        except OSError, ValueError:
            pass  # 断管由父进程按缺失终态处理，不打印异常链。
        finally:
            channel.close()


def _supervise(
    config: dict[str, Any],
    migrations: tuple | Path,
    baseline_existing: bool,
    *,
    worker: Any = _worker,
) -> tuple[Any, int | None]:
    """限时监督；强退保留已确认版本，未确认提交不得解释为已回滚。"""
    from app.infrastructure.database_migrations import MIGRATION_REASONS, MigrationProgress

    progress = MigrationProgress()
    max_versions = 9999 if isinstance(migrations, Path) else len(migrations)
    end = time.monotonic() + config["migration_total_timeout_seconds"]
    reserve = min(config["cleanup_timeout_seconds"], config["migration_total_timeout_seconds"] / 4)
    worker_end = end - reserve
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(
        target=worker, args=(writer, config, migrations, baseline_existing, worker_end), daemon=True
    )
    finished = False
    received = 0
    reader_open = True
    terminal_reason = None

    def receive() -> None:
        nonlocal finished, received, reader_open
        try:
            packet = reader.recv_bytes(maxlength=128).decode("ascii").split("|")
        except EOFError:
            reader_open = False
            return
        received += 1
        if finished or received > 2 * max_versions + 1:
            raise ValueError
        if len(packet) == 3 and packet[0] == "finished":
            if packet[1] not in MIGRATION_REASONS or packet[2] not in {"0", "1"}:
                raise ValueError
            progress.reason, progress.cleanup_complete = packet[1], packet[2] == "1"
            if progress.reason == "ok" and progress.uncertain_version is not None:
                raise ValueError
            finished = True
            return
        if len(packet) != 2:
            raise ValueError
        version = int(packet[1])
        if not 1 <= version <= max_versions:
            raise ValueError
        if packet[0] == "commit_started" and progress.uncertain_version is None:
            if progress.confirmed and version <= progress.confirmed[-1]:
                raise ValueError
            progress.commit_started(version)
        elif packet[0] == "committed" and progress.uncertain_version == version:
            progress.commit_confirmed(version)
        else:
            raise ValueError

    started = False
    forced = False
    alive_pid = None
    try:
        process.start()
        started = True
        writer.close()
        while time.monotonic() < worker_end:
            handles = [process.sentinel] + ([reader] if reader_open else [])
            ready = wait(handles, timeout=max(0, worker_end - time.monotonic()))
            if reader in ready:
                receive()
            if process.sentinel in ready:
                break
    except OSError, ValueError, UnicodeError:
        terminal_reason = "worker_protocol_error"
    except KeyboardInterrupt:
        terminal_reason = "cancelled"
    finally:
        writer.close()
        if started:
            if process.is_alive():
                forced = True
                process.terminate()
                process.join(timeout=max(0, end - time.monotonic()))
            if process.is_alive():
                process.kill()
                process.join(timeout=max(0, end - time.monotonic()))
            if process.is_alive():
                alive_pid = process.pid
            else:
                process.join(timeout=0)
                # 终止之前成功发送的事实仍必须接管，不以 exitcode 代替提交证据。
                try:
                    while reader_open and reader.poll():
                        receive()
                except OSError, ValueError, UnicodeError:
                    terminal_reason = terminal_reason or "worker_protocol_error"
                if process.exitcode != 0 or not finished:
                    progress.reason = "commit_unknown"
                    progress.cleanup_complete = False
                process.close()
        if forced:
            progress.forced_termination = True
            progress.reason = "commit_unknown"
            progress.cleanup_complete = False
        if terminal_reason is not None and not forced and progress.uncertain_version is None:
            # 父进程终态标签不得盖掉「提交结果未知」：强退，或已有未确认提交时，一律以未知为准（G0-4/G0-7）。
            progress.reason = terminal_reason
        reader.close()
    return progress, alive_pid


def main(argv: list[str] | None = None) -> int:
    """解析帮助后才加载配置；两个正式入口共用同一迁移路径。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = _Parser(description="执行共享数据库迁移；结构/基线门禁不完整时拒绝升级。")
    parser.add_argument("--baseline-existing", action="store_true", help="严格验证后接管已有兼容表")
    parser.add_argument("--migrations-dir", type=Path, default=Path(__file__).resolve().parents[1] / "migrations")
    args = parser.parse_args(argv)
    # 帮助路径不导入 Settings；包括其模块级实例的校验异常在此统一脱敏。
    try:
        from app.config.settings import settings
        from app.infrastructure.database_migrations import (
            MIGRATION_REASONS,
            SCHEMA_PREPARER,
            MigrationError,
            MigrationProgress,
        )
    except Exception:  # noqa: BLE001 — 配置/依赖加载边界禁止回显输入与 traceback。
        print('{"status":"failed","reason":"configuration_invalid"}')
        return 2
    try:
        if SCHEMA_PREPARER is None:
            progress = MigrationProgress(reason="schema_gate_unavailable", cleanup_complete=True)
            alive_pid = None
        else:
            progress, alive_pid = _supervise(settings.database_config, args.migrations_dir, args.baseline_existing)
    except MigrationError as error:
        reason = str(error) if str(error) in MIGRATION_REASONS else "internal_error"
        progress = MigrationProgress(reason=reason, cleanup_complete=True)
        alive_pid = None
    success = progress.reason == "ok" and progress.cleanup_complete and alive_pid is None
    print(
        json.dumps(
            {
                "status": "ok" if success else "failed",
                "reason": progress.reason,
                "confirmed_versions": progress.confirmed,
                "uncertain_version": progress.uncertain_version,
                "cleanup_complete": progress.cleanup_complete,
                "forced_termination": progress.forced_termination,
                "commit_outcome_unknown": progress.uncertain_version is not None
                or progress.forced_termination
                or progress.reason == "commit_unknown",
                "worker_pid": alive_pid,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if alive_pid is not None:
        # OS 未确认终止时避免解释器 atexit 再次无界 join；明确报告仍存活的进程。
        os._exit(1)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
