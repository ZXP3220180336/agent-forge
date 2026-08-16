# 全局日志框架

> **模块**：`app/utils/logger.py`
> **定位**：项目级日志基础设施 —— 全项目所有模块（LLM / 服务 / Agent / API / 基础设施）的统一日志入口
> **职责**：双 handler 输出、业务事件机制、结构化日志（JSON）

---

## 📋 目录

- [设计理念](#设计理念)
- [快速开始](#快速开始)
- [API 说明](#api-说明)
- [输出形态](#输出形态)
- [业务事件机制](#业务事件机制)
- [配置项清单](#配置项清单)
- [字段与保留键约束](#字段与保留键约束)
- [各模块日志清单](#各模块日志清单)
- [相关文档](#相关文档)

---

## 设计理念

1. **横切关注点**：日志是全局基础设施，不归属任何业务模块。各模块用 `get_logger()` 拿命名空间 Logger，最终输出统一走本框架的双 handler。
2. **双 handler**：控制台人类可读（`[OK]/[WARN]` 前缀 + 字段摘要），文件结构化（JSON 默认，可切 text）——开发调试友好 + 可进 ELK。
3. **业务事件**：以 `log_event("llm_call", **fields)` 记录领域事件（如 LLM 调用），事件名作 message、字段进结构化输出，跨模块可检索。
4. **异步写入**：`log_event_async` 用 `asyncio.to_thread` 兑现「日志不阻塞主流程」，文件 IO 移出事件循环。
5. **幂等配置**：`setup_logging()` 清空 root handlers 重建，可重复调用。

---

## 快速开始

```python
from app.utils.logger import get_logger, setup_logging, log_event_async

# 1. 应用入口（main.py）配置一次
setup_logging()

# 2. 各模块普通日志
logger = get_logger("container")
logger.warning("Redis 不可用，服务降级: %s", e)

# 3. 业务事件（结构化字段进 JSON）
await log_event_async("llm_call", success=True, duration=2.3, total_tokens=120)
```

---

## API 说明

### `setup_logging() -> None`

按 `settings` 配置根 logger 的双 handler（幂等，可重复调用）：

- 控制台：`StreamHandler(sys.stdout)` + `ConsoleFormatter`（人类可读）
- 文件：`FileHandler(settings.log_file, encoding="utf-8")` + `JsonFormatter`（默认）或 `ConsoleFormatter`（`log_format="text"`）

**时序**：必须在应用入口（`app/main.py`）模块级调用，早于 `Container.initialize()` 与静态目录检查，否则这些模块的 print→logging 会静默丢失。

### `get_logger(name: str) -> logging.Logger`

返回 `app.{name}` 命名空间下的标准 `logging.Logger`，不挂 handler（统一走 root）。示例：`get_logger("container")` → `app.container`。

### `log_event(event_name: str, level=INFO, **fields) -> None`

同步记录一条业务事件。`event_name` 作为 LogRecord 的 message，`fields` 经 extra 注入成为记录的自定义属性。

### `log_event_async(event_name: str, level=INFO, **fields) -> None`

异步版本：`await asyncio.to_thread(log_event, ...)`，文件 IO 移出事件循环。**需运行中的事件循环**；无循环场景（脚本/测试）用同步 `log_event`。

### `fill_llm_event_fields(event_fields, *, success, duration, error=None, usage=None, finish_reason=None) -> None`

LLM 调用事件的通用填充 + 记录工具：填充 `success`/`error`/`duration`/`prompt_tokens`/`completion_tokens`/`total_tokens`/`finish_reason` 到 `event_fields` 并 `await log_event_async("llm_call")`。被 `llm_service.py`（generate）与 `streaming_rectifier.py`（整流循环）复用，统一各调用点的日志填充与记录。

---

## 输出形态

### 控制台（ConsoleFormatter）

```
18:42:08 [OK]   app.events: llm_call success=True duration=1.5
18:42:08 [WARN] app.container: Redis 不可用（服务降级）: ...
```

- 级别前缀全 ASCII（`[OK]/[WARN]/[ERR]/[DBG]/[CRIT]`），避免非 GBK 字符在 Windows 控制台触发编码错误
- 附加字段以 `key=value` 紧凑摘要追加

### 文件（JsonFormatter，默认）

```json
{"timestamp": "2026-08-04T18:42:08+0800", "level": "INFO", "logger": "app.events", "message": "llm_call", "success": false, "error": "超时", "duration": 1.5}
```

- 每行一条 JSON，含标准字段（`timestamp`/`level`/`logger`/`message`）+ 事件自定义字段
- `ensure_ascii=False`（中文可读）、`default=str`（兜底异常/dataclass）、None 保留为 `null`

---

## 业务事件机制

**用途**：记录领域事件（LLM 调用、任务流转等），事件名即 message，字段进结构化输出。

**LLM 调用事件 `llm_call`**（由 `fill_llm_event_fields` 产生——`app/utils/logger.py` 的通用 LLM 事件日志工具，被 `llm_service.py` 与 `streaming_rectifier.py` 复用）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `model` | str | 模型名 |
| `messages_count` | int | 消息条数 |
| `temperature` | float | 温度 |
| `has_tools` | bool | 是否携带工具 |
| `stream` | bool | 是否流式 |
| `success` | bool | 是否成功 |
| `error` | str\|None | 失败原因（`None` 表示成功） |
| `duration` | float | 耗时（秒） |
| `prompt_tokens` | int\|None | 输入 Token |
| `completion_tokens` | int\|None | 输出 Token |
| `total_tokens` | int\|None | 总计 Token |
| `finish_reason` | str\|None | 停止原因 |

**用法**：`await fill_llm_event_fields(event_fields, success=True, duration=2.3, usage=..., finish_reason=...)` —— 填充 success/error/duration/tokens/finish_reason 并 `log_event_async("llm_call")` 落盘，统一各调用点（LLMService.generate、StreamingRectifier 整流循环）。

**脱敏**：只记元数据（消息数/Token），不记 messages 内容本身。

**语义**：整流重试成功 = 失败尝试各 1 条 + 成功 1 条（同一事件 dict 复用，成功路径清掉残留 error）。

---

## 配置项清单

| 配置项 | 默认值 | 说明 | 使用 |
| --- | --- | --- | --- |
| `LOG_LEVEL` | `INFO` | 日志级别（DEBUG/INFO/WARNING/ERROR/CRITICAL） | ✅ 已用 |
| `LOG_FORMAT` | `json` | 文件输出格式（json/text） | ✅ 已用 |
| `LOG_FILE` | `logs/app.log` | 日志文件路径 | ✅ 已用 |

---

## 字段与保留键约束

`log_event` 的 `fields` 经 extra 注入 LogRecord。**字段名不能与 LogRecord 保留键冲突**（`name`/`msg`/`args`/`levelname`/`pathname`/`module`/`threadName`/`processName`/`message`/`taskName` 等）——若冲突，logging 会在调用点抛 `KeyError`（开发期即暴露）。`JsonFormatter` 侧用白名单排除保留键，只输出自定义字段。

---

## 各模块日志清单

| 模块 | logger 名 | 日志内容 |
| --- | --- | --- |
| `app/utils/logger.py` | `app.events` | 业务事件（`llm_call` 等） |
| `app/main.py` | `app.main` | 启动/关闭/静态目录警告 |
| `app/container.py` | `app.container` | 基础设施初始化（Redis/DB/工具） |
| `app/application/session/session_manager.py` | `app.services.session_manager` | 缓存降级警告 |
| `app/integration/tools/tool_service.py` | `app.services.tool_service` | 工具钩子失败 |

> 早期 `LLMLogger`（`app/integration/llm/logger.py`）已移除，其「LLM 调用记录」职责并入本框架的业务事件机制（`log_event_async("llm_call")`），输出通道统一走全局双 handler。

---

## 相关文档

- [配置管理模块](../config_doc/config.md)（`LOG_*` 配置项）
- [LLM 服务层说明](../integration_doc/llm_doc/llm.md)（LLM 调用业务事件）
- [服务层说明](../application_doc/README.md)（各模块日志归属）
- [架构设计](../architecture.md)
