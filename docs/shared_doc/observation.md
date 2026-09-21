# 非关键观测隔离（app/shared/observation.py）

> **模块**：`app/shared/observation.py`
> **定位**：共享内核横切能力 —— 让日志、指标、审计等非关键观测的失败不替换主业务异常或终态（G0-6）

## 职责

`isolate_observation(operation: Callable[[], object]) -> None`：执行一次非关键观测，隔离其异常。

- 只捕获 `Exception`。`asyncio.CancelledError`、`GeneratorExit`、`KeyboardInterrupt` 属 `BaseException`，继续传播——隔离边界不改取消与生成器收尾语义。
- **只隔离，不限时**。同步日志在调用方线程内执行、阻塞不可中断，因此本模块**没有**对应的异步有界原语；把两者合成一个带可选 `timeout` 的签名，会让调用方误以为同步观测也受时限保护。需要限时的异步观测保留各自的显式预算（如 LLM 事件日志按私有常量做 `wait_for`）。
- 自身不记日志：观测失败即静默返回，避免「隔离失败再触发一次观测」的递归。
- 只依赖标准库，不 import `app.platform`——`app.domain` 与 `app.shared` 均不可依赖 platform。

**用法约束**：整个日志表达式必须写进 `operation`（含参数求值、`LogRecord` 构造、handler 与 filter 的执行），否则参数求值会逃出隔离范围。

```python
isolate_observation(lambda: logger.warning("裁剪 %d 条", dropped))
```

该边界只用于日志、指标与审计；业务异常必须在调用之前抛出，不得借它吞掉业务失败。

## 消费方

| 消费方 | 场景 | 位置 |
| --- | --- | --- |
| `ReActStrategy._finalize_unknown` | UNKNOWN 终态日志（失败会让终态与已接管进度整体丢失） | `app/domain/reasoning/react.py` |
| `ErrorHandlerRegistry.dispatch` | handler 异常的降级告警（返回值即终态决策） | `app/shared/error_handling.py` |
| `ToolExecutor._audit` | 审计因观察预算耗尽被跳过的告警 | `app/integration/tools/executor.py` |
| `ToolExecutor._observe` | 观测失败或超预算的兜底告警（位于 `supervisor.wait` 保护区间之外） | `app/integration/tools/executor.py` |
| `ToolExecutor._record_stats` | 工具统计记录失败的告警 | `app/integration/tools/executor.py` |
| `_close_stream` | 关闭 LLM 流失败的告警（在展开原始终止信号期间，覆盖会破坏 cancel 优先级） | `app/integration/llm/stream_consumption.py` |
| `ReservationLimiter._acquire` | TPM 预留超桶截断的告警（发生在任何桶扣减之前） | `app/integration/llm/reservation_limiter.py` |
| `ContextManager.build_messages` | 上下文超限裁剪的告警（请求关键路径） | `app/application/context/context_manager.py` |

## 边界与未覆盖面

- **同步路径只能证明异常隔离，不能证明有界**。同步阻塞不可中断，这是覆盖边界而非遗漏。
- 未纳入本边界的入口：`structured.py`、`loader.py`、`client.py`、`assembler.py`、`hooks.py`、`session_manager.py` 等 except 分支内的降级告警——它们不在终态、请求或收尾路径上，失败只影响该分支自身。
- `security.py` 的 `await log_event_async("tool_call", ...)` 方法体自身无界，但其唯一生产调用点位于 `ToolExecutor._observe` 的 `supervisor.wait` 之内，有界性与后台任务 Owner 由 Supervisor 边界提供，不在此重复设界。

## 相关文档

- [G0-6 观测隔离](../engineering/ai-engineering-rules.md#g0)（唯一规则正文）
- [单一来源治理 ADR](../../adr/2026-09-12-single-source-governance.md)（「其他观测入口仍须逐入口核验」的后续符合性工作）
- [error_handling.md](error_handling.md)（同层的错误分类与分发契约）
- [工具执行观测所有权问题](../../issues/integration/tools/2026-09-19-observation-cancel-ownership.md)（TOOLS-054，同步阻塞不可中断的依据）
