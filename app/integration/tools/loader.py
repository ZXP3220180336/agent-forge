"""外部工具热加载器 — 从目录发现 BaseTool 并纳入注册中心。

对齐工业级热插拔「内嵌式可信插件」档（见 ADR 2026-08-17-external-tool-hot-reload）：

- **execute 惰性检查**：无后台任务，`maybe_refresh()` 在工具调用入口对比目录签名，
  变化才重扫——对齐工业标准「变更 → 下次调用生效」；
- **生命周期钩子**：加载前 `on_load()` / 卸载前 `on_unload()`，`health_check()` 预留巡检；
- **全链路留痕**：加载 / 重载 / 卸载 / 冲突拒绝 / 失败均结构化日志；
- **同名拒绝**：与已注册工具重名 → 跳过 + warning（builtin 权威）。

依赖注入 `service`（ToolService Facade），经其公共接口 `get/register/unregister` 操作，
不破坏 Facade 封装，也不直接依赖内部组件。
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import inspect
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from app.integration.tools.base import BaseTool
from app.platform.observability.logger import get_logger

if TYPE_CHECKING:
    from app.integration.tools.tool_service import ToolService

logger = get_logger("tools.external")

# 外部工具包名（模块名空间；文件内相对导入依赖该包已注册）
_EXTERNAL_PKG = "app.integration.tools.external"
# 默认扫描目录：external/ 包的物理路径（可经构造注入其它目录）
_DEFAULT_EXTERNAL_DIR = str(Path(__file__).parent / "external")
# 目录签名检查 TTL（秒）：热路径磁盘 stat 频率上限（1s 内不重扫，变更最多延迟 1s 生效）
_DIR_SIGNATURE_TTL = 1.0


def _collect_tool_classes(module: ModuleType) -> list[type[BaseTool]]:
    """从模块收集可实例化的 BaseTool 子类（过滤规则与 builtin 自动发现一致）。"""
    classes: list[type[BaseTool]] = []
    for obj in vars(module).values():
        if (
            inspect.isclass(obj)
            and issubclass(obj, BaseTool)
            and obj is not BaseTool
            and not getattr(obj, "__abstractmethods__", None)
        ):
            classes.append(obj)
    return classes


class ExternalToolLoader:
    """外部工具加载器：目录发现 → 注册中心，execute 惰性检查感知变化。

    无后台任务。每次工具调用经 `maybe_refresh()` 对比目录签名（文件集 + mtime/size），
    变化才应用 diff（新增加载 / 修改重载 / 删除卸载）。
    """

    def __init__(
        self,
        service: ToolService,
        *,
        default_directory: str | None = None,
        config_source: Callable[[str], Any] | None = None,
    ) -> None:
        self._service = service
        self._config_source = config_source  # 配置提供者：键 → 值（装配根绑定 settings）
        self._directory = (
            Path(default_directory)
            if default_directory
            else Path(_DEFAULT_EXTERNAL_DIR)
        )
        self._signature: tuple[Any, ...] | None = None  # 上次扫描的目录签名
        self._file_tools: dict[str, list[str]] = {}  # 绝对路径 -> 该文件拥有的工具名
        self._file_modules: dict[str, set[str]] = {}  # 绝对路径 -> 该文件导入的模块名（工具模块 + 兄弟模块）
        self._file_sigs: dict[
            str, tuple[int, int]
        ] = {}  # 绝对路径 -> 上次扫描的 (mtime, size)
        self._last_dir_check = 0.0  # 上次目录签名 stat 时间（monotonic，TTL 用）
        self._scan_lock = asyncio.Lock()

    # ===== 对外接口 =====

    async def maybe_refresh(self) -> None:
        """execute 入口调用：TTL 内零磁盘 IO，签名变化才重扫（变更最多延迟 1s 生效）。

        目录签名（glob + stat）经 `asyncio.to_thread` 执行，不阻塞事件循环；
        TTL（`_DIR_SIGNATURE_TTL`）限制热路径 stat 频率（1s 内最多一次）。
        """
        now = time.monotonic()
        if now - self._last_dir_check < _DIR_SIGNATURE_TTL:
            return  # TTL 内复用上次签名结果，不做磁盘 IO
        self._last_dir_check = now
        if await asyncio.to_thread(self._dir_signature) == self._signature:
            return
        await self.scan_once()

    async def scan_once(self) -> None:
        """应用磁盘 diff（新增 / 修改 / 删除），更新目录签名。可手动调用（幂等）。

        注意：`_scan_lock` 为 asyncio.Lock（不可重入），加载流程在锁内 await 用户
        `on_load` / `on_unload`——生命周期钩子内**禁止反向调用 execute**
        （会经 maybe_refresh → scan_once 二次加锁死锁）。
        """
        async with self._scan_lock:
            sig = self._dir_signature()
            if sig == self._signature:
                return  # 并发下另一路径已完成扫描
            current = self._dir_files()

            # 1. 删除：磁盘不再存在 → 卸载
            for path in list(self._file_tools):
                if path not in current:
                    await self._unload_file(path)

            # 2. 新增 / 修改：与上次文件签名比较（mtime 或 size 变化 → 重载）
            for path, file_sig in current.items():
                if path not in self._file_tools:
                    ok = await self._load_file(path, file_sig)
                    if ok:
                        self._file_sigs[path] = file_sig
                elif self._file_sigs.get(path) != file_sig:
                    await self._reload_file(path, file_sig)

            self._signature = sig

    # ===== 目录与签名 =====

    def _dir_files(self) -> dict[str, tuple[int, int]]:
        """扫描目录 *.py（排除 __init__.py 与 _ 开头），返回 {绝对路径: (mtime_ns, size)}。"""
        files: dict[str, tuple[int, int]] = {}
        try:
            entries = list(self._directory.glob("*.py"))
        except OSError:
            logger.warning("外部工具目录不可访问: %s", self._directory)
            return files
        for p in entries:
            if p.name == "__init__.py" or p.name.startswith("_"):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            files[str(p)] = (st.st_mtime_ns, st.st_size)
        return files

    def _dir_signature(self) -> tuple[Any, ...]:
        """目录签名：文件集 + 各文件 (mtime, size)，用于惰性变更检测。"""
        return tuple(sorted((path, *sig) for path, sig in self._dir_files().items()))

    # ===== 加载 / 重载 / 卸载 =====

    def _module_name(self, path: str) -> str:
        """模块名：合法标识符 stem 用真实包名（保证文件内相对导入与 loader 恒等）；
        非法标识符（my-tool.py / 中文.py）回退 sha1 哈希（合法、唯一、跨重载稳定）。"""
        stem = Path(path).stem
        if stem.isidentifier():
            return f"{_EXTERNAL_PKG}.{stem}"
        digest = hashlib.sha1(path.encode("utf-8")).hexdigest()[:10]
        return f"{_EXTERNAL_PKG}._{digest}"

    @staticmethod
    def _exec_module_sync(module_name: str, path: str) -> ModuleType:
        """同步导入模块（放线程池执行，防模块顶层代码阻塞事件循环）。"""
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法为 {path} 创建模块 spec")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module  # 先插入，处理模块内自引用 / 相对导入
        spec.loader.exec_module(module)
        return module

    def _inject_config(self, cls: type[BaseTool], module: ModuleType) -> None:
        """外部工具配置注入：模块声明 `CONFIG_KEYS` 时，从 config_source 取值调 register_config。

        内置工具由装配根 `register_config` 注入（settings）；外部工具自动加载无装配根，
        配置经 loader 的 `config_source`（装配根绑定的 settings 读取器）注入——路径对齐内置风格，
        避免外部工具配置被静默忽略（见 TOOLS-010）。
        """
        register_config = getattr(cls, "register_config", None)
        keys: tuple[str, ...] = getattr(module, "CONFIG_KEYS", ())
        if not keys or not register_config or self._config_source is None:
            return
        config = {
            key: value
            for key in keys
            if (value := self._config_source(key)) is not None
        }
        if config:
            register_config(**config)

    @staticmethod
    def _drop_modules(before: set[str]) -> None:
        """清理导入过程中新增的 sys.modules 条目（工具模块 + 其兄弟模块）。

        工具文件相对导入的兄弟模块（如 `_helper.py`）若不清理，重载时
        `from . import _helper` 命中旧缓存，多文件工具「变更 → 下次生效」失效。
        """
        for name in set(sys.modules) - before:
            sys.modules.pop(name, None)

    async def _load_file(self, path: str, file_sig: tuple[int, int]) -> bool:
        """加载一个外部工具文件：导入模块 → 收集工具类 → 实例化 + on_load → 注册。

        返回是否注册了至少一个工具（用于更新文件签名）。文件级失败 → 回滚 + False。
        导入时快照 sys.modules，卸载 / 回滚时清理工具模块及其兄弟模块（防旧缓存）。
        """
        module_name = self._module_name(path)
        sys.modules.pop(module_name, None)  # 防御上次残留
        logger.info("外部工具加载: %s", path)

        try:
            importlib.import_module(_EXTERNAL_PKG)  # 确保包已注册（相对导入依赖）
        except Exception as e:  # noqa: BLE001
            logger.warning("外部工具包导入失败，跳过: %s: %s", path, e)
            return False

        before = set(sys.modules)  # exec 前快照：导入新增条目（工具模块 + 兄弟）待追踪
        try:
            module = await asyncio.to_thread(self._exec_module_sync, module_name, path)
        except Exception as e:  # noqa: BLE001
            self._drop_modules(before)
            logger.warning("外部工具文件导入失败，跳过: %s: %s", path, e)
            return False
        imported = set(sys.modules) - before

        tool_classes = _collect_tool_classes(module)
        if not tool_classes:
            self._drop_modules(before)
            logger.warning("外部工具文件无 BaseTool 子类，跳过: %s", path)
            return False

        # 文件级原子性：逐个实例化 + on_load + 注册；任一失败回滚本文件已注册实例
        registered: list[BaseTool] = []
        try:
            for cls in tool_classes:
                self._inject_config(cls, module)
                tool = cls()
                await tool.on_load()
                if self._service.get(tool.name) is not None:
                    logger.warning(
                        "外部工具与已注册工具重名，跳过 %s: %s", tool.name, path
                    )
                    await tool.on_unload()  # 释放 on_load 建立的资源
                    continue
                self._service.register(tool)
                registered.append(tool)
        except Exception as e:  # noqa: BLE001
            for tool in registered:
                try:
                    await tool.on_unload()
                except Exception as unload_err:  # noqa: BLE001
                    logger.warning(
                        "外部工具回滚时 on_unload 失败: %s: %s", tool.name, unload_err
                    )
                self._service.unregister(tool.name)
            self._drop_modules(before)
            logger.warning("外部工具加载失败，已回滚本文件: %s: %s", path, e)
            return False

        if not registered:
            self._drop_modules(before)
            logger.warning("外部工具文件无可用工具，跳过: %s", path)
            return False

        self._file_tools[path] = [t.name for t in registered]
        self._file_modules[path] = imported  # 追踪工具模块 + 兄弟模块（卸载时清理）
        logger.info("外部工具已注册: %s", [t.name for t in registered])
        return True

    async def _unload_file(self, path: str) -> None:
        """卸载一个文件拥有的全部工具：on_unload → 注销 → 清理模块缓存。"""
        tool_names = self._file_tools.pop(path, [])
        self._file_sigs.pop(path, None)
        for name in tool_names:
            tool = self._service.get(name)
            if tool is not None:
                try:
                    await tool.on_unload()
                except Exception as e:  # noqa: BLE001 — 卸载清理失败不影响注销
                    logger.warning(
                        "外部工具 on_unload 失败（继续卸载）: %s: %s", name, e
                    )
                self._service.unregister(name)
            logger.info("外部工具卸载: %s", name)
        # 清理本次导入的全部模块（工具模块 + 兄弟模块），防重载用到旧兄弟代码
        for module_name in self._file_modules.pop(path, set()):
            sys.modules.pop(module_name, None)

    async def _reload_file(self, path: str, file_sig: tuple[int, int]) -> None:
        """重载：nuke-and-repave（先卸载旧实例再加载新实例）。"""
        await self._unload_file(path)
        ok = await self._load_file(path, file_sig)
        if ok:
            self._file_sigs[path] = file_sig
