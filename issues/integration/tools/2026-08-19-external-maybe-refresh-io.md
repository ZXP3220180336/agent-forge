# TOOLS-012 maybe_refresh 每次 execute 同步磁盘 IO 上事件循环

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P3（性能，次要项）
> **来源**：2026-08-18 工具模块代码审核（编排核心层 · 次要项 1）
> **涉及模块**：`app/integration/tools/loader.py`（ExternalToolLoader.maybe_refresh）
> **关联文档**：[external.md](../../../docs/integration_doc/tools_doc/external.md)

---

## 问题描述

### 现象

`maybe_refresh()` 在**每次** `ToolService.execute` 时同步执行 `Path.glob("*.py")` + 逐文件 `p.stat()` 磁盘 IO，直接在事件循环上——外部工具增多时热路径开销线性增长，与 `_exec_module_sync` 放线程池的做法不一致。

### 影响

高频工具调用下事件循环被同步磁盘 IO 周期性阻塞（stat 为微秒-百微秒级，累积可观）；外部工具文件多时更明显。

### 根因

目录签名检查（glob + stat）无频率限制且同步执行；`maybe_refresh` 只在 execute 入口调用，无 TTL 缓存。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| 文件监视节流（watchdog / chokidar / Vue 文件监听） | mtime 检查带 debounce / TTL——高频轮询限频，磁盘 IO 不随调用次数线性增长 |
| asyncio 不阻塞原则 | 同步磁盘 IO 移 `asyncio.to_thread`，事件循环不被 stat 阻塞 |

**核心**：TTL 限频（1s 内零磁盘 IO）+ to_thread（到期那次不阻塞事件循环）。

---

## 修复方案（含决策取舍）

**决策**：`maybe_refresh` 改 TTL + to_thread：

```python
async def maybe_refresh(self) -> None:
    now = time.monotonic()
    if now - self._last_dir_check < _DIR_SIGNATURE_TTL:  # TTL 内零磁盘 IO
        return
    self._last_dir_check = now
    if await asyncio.to_thread(self._dir_signature) == self._signature:  # stat 放线程池
        return
    await self.scan_once()
```

- `_DIR_SIGNATURE_TTL = 1.0`：磁盘 stat 频率上限 1 次/秒；
- 目录签名（glob + stat）经 `asyncio.to_thread` 执行，不阻塞事件循环；
- 变更生效语义从「下次调用立即」变为「下次调用（最迟 1s TTL 后）」——可接受延迟，换热路径零 IO。

**取舍理由**：

1. **TTL 优于纯 to_thread**：仅 to_thread 仍会每次 execute 发起一次线程池 stat（1s 内 100 次调用 = 100 次 stat）；TTL 缓存把磁盘 IO 频率封顶；
2. **1s 延迟对齐「变更生效」语义**：外部工具热更新容忍 ≤1s 延迟，换热路径稳定性。

**语义边界**：首次调用总是检查（`_last_dir_check` 初始 0）；`scan_once`（变更后应用）保持同步实现（低频，不做 TTL）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/loader.py` | `import time`；`_DIR_SIGNATURE_TTL = 1.0`；`__init__` 加 `_last_dir_check`；`maybe_refresh` 改 TTL 短路 + `asyncio.to_thread(self._dir_signature)` | `tests/unit/test_tool_loader.py` 新增 2 用例：`test_maybe_refresh_ttl_skips_within_interval`（连续调用第二次不 stat）+ `test_maybe_refresh_expired_ttl_rechecks`（TTL 过期后重检生效） |
| 文档 | [external.md](../../../docs/integration_doc/tools_doc/external.md) 惰性检查节 + 设计目标更新（TTL / to_thread / 最迟 1s 生效）；测试状态 18 → 20 | — |

---

## 验证

- 相关测试 **20 passed**（含 2 个新增 TTL 用例，既有 maybe_refresh 用例不受 TTL 影响——首次调用总是检查）
- 全量测试待提交前确认（增量改动：仅 maybe_refresh 限频，无回归面）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **热路径磁盘 IO 必须限频**：execute 高频路径的签名检查加 TTL 缓存，频率封顶 1 次/秒，不随调用次数线性增长。
- **TTL + to_thread 结合**：仅限频（同步阻塞）或仅线程池（仍高频 stat）都不完整——TTL 降频率、to_thread 防阻塞，双管齐下。
- **变更延迟是工程权衡**：热更新容忍 ≤1s 生效延迟，换热路径稳定性——文档明确语义，避免「立即生效」的误读。
