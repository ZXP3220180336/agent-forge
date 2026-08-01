# 💡 研发教训与踩坑记录

> **项目**：AI Agent 系统（AsyncioDemo）
> **用途**：集中记录开发过程中踩过的坑，供后续开发避免重蹈。按"坑 → 原因 → 修复"格式记录。
> **定位**：跨模块的调试经验，按所属模块归类；LLM/retry 相关细节另见 [retry.md](llm/retry.md) 附录。

---

## 1. 通用 / 项目级

### 坑 1：文件名错误

**`app/models/__init__.py` 写成了 `__ini__.py`（少了个 t），导入时报 `ImportError: cannot import name 'XX' from 'app.models' (unknown location)`。**

排查了 5 轮才找到。出现 `(unknown location)` 的导入报错，**先检查 `__init__.py` 文件名是否正确**。

### 坑 5：测试脚本运行时路径问题

**`uv run .\scripts\test_xxx.py` 会将 `scripts/` 加入 sys.path，找不到 `app` 模块。** 一律用 `uv run python -m scripts.test_xxx` 从项目根目录运行。

### 坑 6：Python 2 `except` 语法

**`except json.JSONDecodeError, IndexError:` 在 Python 3 中崩溃。** 必须用 `except (JSONDecodeError, IndexError):`。

### 坑 10：`os.getenv()` vs Pydantic Settings

**`os.getenv()` 读不到 `.env` 中的配置，因为 `.env` 由 Pydantic 加载不写入 `os.environ`。** 统一通过 `from app.config import settings` 读取。

### 坑 19：`except A, B:` 在 Python 3.14 合法

**`except json.JSONDecodeError, KeyError:` 在 Python 3.14 下编译通过，语义等价 `except (A, B):`**（实测能捕获两者）——交接文档坑 6 的信息已过时。但 3.8~3.13 下这是 `except A as B` 的绑定语法，会静默绑定变量而不捕获 B 类。已全库规范为元组形式 `except (A, B):`，**新代码一律用元组形式，不要在 3.14 上赌它的兼容性**。

### 坑 20：Windows GBK 控制台 print 崩溃

**Windows 控制台默认 GBK 编码，`print("  ⚠ 错误...")` 里的 `⚠`（U+26A0）不在 GBK 内 → `UnicodeEncodeError`，服务启动直接崩。** 这也是"本地无 Redis/DB 时 app_state.initialize() 走降级路径会崩"的隐藏原因（降级分支打印 `⚠`）。修复：

1. `app/main.py` 入口统一 `sys.stdout/stderr.reconfigure(encoding="utf-8", errors="replace")`
2. 代码内符号字符（⚠✓✗🔧✅❌）改为 ASCII 占位（[WARN]/[OK]/[TOOL] 等），兜底非 UTF-8 环境

> LLM 返回内容（可能含 emoji）打印到控制台同样会崩，独立脚本（如 `scripts/test_agent.py`）顶部也要 reconfigure。

### 坑 26：markdown 中文表格对齐 lint

**markdownlint 的 MD060 按字符宽度（中文算 2 格）校验表格对齐，手写中文表格极易误报。** 用脚本按 east_asian_width 计算列宽自动对齐（见会话中的 `align_tables` 脚本）。**新改 retry.md 表格后重跑对齐脚本。**

---

## 2. 数据模型 / ORM

### 坑 2：SQLAlchemy 保留属性 `metadata`

**ORM 模型中 `metadata = Column(JSON)` 导致 `'metadata' is reserved when using the Declarative API` 运行时错误。** 改名为 `meta`。

### 坑 3：两个 `declarative_base()` 实例

**`messages.py` 和 `session.py` 各自 `Base = declarative_base()`，导致 FK 引用时 mapper 冲突。** 统一到 `models/database/base.py` 共享一个 Base。

---

## 3. 配置模块

### 坑 7：Pydantic v2 的 pydantic-settings

**Pydantic v2 将 `BaseSettings` 移到了独立的 `pydantic_settings` 包。** 需 `pip install pydantic-settings`，然后 `from pydantic_settings import BaseSettings`。

---

## 4. 工具模块

### 坑 8：`HTMLParser.unescape()` 已移除

**Python 3.9+ 移除了 `HTMLParser.unescape()`，必须用 `html.unescape()` 替代。**

---

## 5. 应用入口 / 路径

### 坑 4：路由导入路径与文件结构不一致

**`api/routes/` 的文件名是 `chat.py`，但 `__init__.py` 写的是 `from .chat_router import ...`。路由内用 `from ..dependencies import ...`，但 `dependencies.py` 在 `app/` 下。** 解决：`__init__.py` 的导入名匹配实际文件名；路由内用绝对导入 `from app.dependencies import ...`。

### 坑 9：直接运行 `python app/main.py` 会报错

**`sys.path` 变成 `app/` 目录而非项目根目录。** 当前 `main.py` 已用 `sys.path.insert(0, 项目根目录)` 修复，但仍推荐 `python -m app.main`。

---

## 6. Agent 层

### 坑 11：async generator 不能 `return` 值

**`ReActAgent._strategy_cycle()` 试图用 `return AgentResult(...)` 返回结果，但 async generator 的 return 值无法被 `async for` 消费。** 必须将结果存到实例变量 `self._result`，消费完事件后通过 `agent.result` 属性获取。

### 坑 12：LLM 和 Agent 的事件构建重复

**`llm_service.py` 和 `base.py` 各自有一套 `_build_*_event()` 方法，事件格式不一致。** 统一到 `app/core/events.py`。

### 坑 13：异质 async generator 增加消费负担

**`async_generate()` 同时 yield `str`（SSE 事件）和 `StreamResult`（数据对象）。** 改为通过参数传入 `StreamResult` 对象，generator 只 yield str。

### 坑 14：LLM 层越界做了 Agent 层的决策

**旧 `async_generate()` 在内部管理 messages 历史 + 判断 finish_reason + 决定是否重试。** 分开：`async_generate()` 只做单轮推理 + 返回原始数据；策略循环由 `ReActAgent._strategy_cycle()` 控制。

### 坑 21：assistant 消息缺失 `tool_calls` → OpenAI 兼容 API 400

**ReAct 循环把 tool 消息回传前，前置 assistant 消息必须带 `tool_calls` 字段（OpenAI 兼容 API 硬性规则）。** 修复前 executor 只回传 content + reasoning_content，DeepSeek 第二轮必报 `400 - Messages with role 'tool' must be a response to a previous message with 'tool_calls'`，Agent 空转重试到迭代上限。修复：`assistant_msg["tool_calls"] = stream_result.tool_calls`（见 `app/core/agent/executor.py` 步骤 2）。已加测试断言防回归。

---

## 7. LLM 层（详见 `docs/llm/retry.md` 附录）

### 坑 15：构造函数的形参名 vs 调用时的关键字参数名不一致

**`structured.py` 中 `_try_extract()` 和 `_fallback_extract()` 的参数叫 `model`，但调用 `generate(model=model)` 时形参名是 `model_key`。** 造成运行时 TypeError。**方法签名改为 `model_key`，调用时也传 `model_key=model_key`。**

### 坑 16：`**extra` 被静默吞没

**`client.py` 的 `register_config(**extra)` 存入配置后，`get_client()` 只取 `api_key` 和 `base_url`，调用方传入的 `organization`、`timeout`、`max_retries` 等参数永远不会传给 `AsyncOpenAI`。** 定义白名单 `_OPENAI_CLIENT_KWARGS`，`get_client()` 过滤出可传参的字段。

### 坑 17：半开探针逻辑错误

**`CircuitBreaker` 初始实现中：**

1. **探针计数 off-by-one**：`OPEN→HALF_OPEN` 时不计数，导致 `half_open_max_requests=3` 实际放行 4 个探针
2. **一个探针成功就关闭**：第一个探针成功后 `record_success()` 无条件 `_state=CLOSED`，`half_open_max_requests` 实际只用了 1 个

**修复**：

- `OPEN→HALF_OPEN` 时 `_half_open_requests=1`（当前请求算第一个探针）
- 新增 `_consecutive_successes`，半开下需要**全部探针连续成功**才关闭，任何一个失败回到 OPEN

### 坑 18：`RETRYABLE` 错误不计入熔断计数

**`classify_error()` 有 `RETRYABLE` 和 `RATE_LIMITED` 两种可重试分类，但 `execute()` 只对 `RATE_LIMITED` 和已删除的 `CIRCUIT_TRIGGER` 调 `record_failure()`。** 意味着超时和 5xx 连续 100 次也不会触发熔断。修复：所有非 `NON_RETRYABLE` 错误都调用 `record_failure()`。

### 坑 22：openai SDK 异常构造需要 message + response，非 HTTP 异常需真实对象

**测试里构造 `openai.APIStatusError` 子类（如 `BadRequestError`/`InternalServerError`/`RateLimitError`）需要 `message` + `response` 两个参数**（`InternalServerError` 无字面量 status_code，值来自传入的 `httpx.Response`）。`LengthFinishReasonError` 需要真实 `ChatCompletion` 对象（访问 `.usage`），不能传 None。**构造测试异常统一用 `httpx.Response(status_code, request=...)` 传参。**

### 坑 23：`InternalServerError` 没有硬编码 status_code

**openai `InternalServerError` 继承 `APIStatusError` 但无字面量状态码**（不像 `BadRequestError` 硬编码 400），`status_code` 是响应里的实际 5xx 值。`classify_error` 不能依赖 `isinstance(exc, InternalServerError)` 判定，必须走 `status_code` 分支（5xx → RETRYABLE）。

### 坑 24：半开探针"失败处理"三个方案演进，别走回头路

**半开探针收到 429/4xx 的处理经历了三次设计，前两个都踩了坑：**

1. **「可达性成功」方案（429/4xx 计入成功）**：连续 3 次 429 → 误关闭熔断器，流量涌入仍过载的下游 → **错误**
2. **「中性归还槽位」方案（`abandon_probe`）**：429 返回后槽位归还、请求立即再来探测 → **每个请求都变探针持续压过载下游，下游更不易恢复**（用户明确指出）→ **错误**
3. **「失败一律回 OPEN」方案（最终）**：429/超时/5xx → `record_failure()` 回 OPEN + 冷却（停止探测让下游喘息）；4xx/未知 → 回 OPEN + 异常抛给上层（客户端问题）→ **正确**

**教训：探针一旦被放行就必须推进状态机（成功或失败），绝不存在"放行后不记录"的路径（否则 HALF_OPEN 死锁）；429 是过载信号，需要的是停止探测（回 OPEN 冷却）而非归还槽位后继续探测。**

### 坑 25：`httpx.TimeoutException` 与 `httpx.NetworkError` 无继承关系

**`httpx.TimeoutException` 不是 `httpx.NetworkError` 的子类**（`ConnectTimeout` 是 `TimeoutException` 子类，`ConnectError`/`ReadError` 是 `NetworkError` 子类）。`classify_error` 捕获 httpx 网络异常必须同时匹配两者，不能只匹配 `NetworkError`。
