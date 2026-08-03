# ToolService 工具服务说明文档

> **更新日期**：2026-08-04
> **模块**：`app/services/tool_service.py`
> **文档定位**：ToolService 独立说明 —— 工具系统的对外统一入口（容器 + 执行 + 统计 + 钩子 + 内置工具装配），已合并原 ToolRegistry 职责。

---

## 📋 目录

- [模块概述](#模块概述)
- [核心类与方法](#核心类与方法)
- [关键实现详解](#关键实现详解)
- [使用示例](#使用示例)
- [配置关联](#配置关联)
- [相关文档](#相关文档)

---

## 模块概述

### 定位与职责

ToolService 是**工具系统的对外统一入口**（2026-08-02 重构后，原 `ToolRegistry` 职责合并于此），负责：

1. **工具容器**：注册 / 注销 / 查询 / 列表
2. **工具执行**：`execute()` 带参数验证、自动重试（指数退避）、超时保护、执行统计、并发控制
3. **执行统计**：`ToolStats` 记录调用次数 / 成功率 / 平均耗时 / 最后调用时间
4. **钩子机制**：工具执行成功后运行扩展钩子（异步 / 同步皆可，钩子失败不影响工具执行）
5. **内置工具装配**：`init_default_tools()` 用 importlib 扫描 `app.tools.builtin` 包，幂等注册
6. **Schema 导出**：`get_openai_tools()` / `get_openai_responses()` 导出 OpenAI 格式工具列表

### 与其它服务的关系

```text
Agent（ReActAgent，经 _execute_tool_calls 并行调用）
        │
        ▼
ToolService（统一入口：execute / get_openai_tools / get_stats ...）
        │
        └──────► app/tools/builtin/（内置工具实现）
                  ├── search.py     → 工具名 "search"
                  ├── file_ops.py   → "readFile" / "writeFile"
                  ├── code_exec.py  → "code_exec"
                  └── web_browse.py → "web_browse"
```

- `app_state` 持有单例实例，`get_tool_service` 依赖注入返回同一实例（见 [服务层总览](../service.md)）
- Agent / 路由均通过 ToolService 访问工具系统，不再有独立 ToolRegistry

### 内置工具清单（`app/tools/builtin/`）

| 类 | 注册名（`tool.name`） | 功能 |
| --- | --- | --- |
| `SearchTool` | `search` | 搜索（底层 Tavily） |
| `ReadFileTool` | `readFile` | 读取文本文件（超长按 `tool_max_output_length` 截断） |
| `WriteFileTool` | `writeFile` | 写入文件 |
| `CodeExecTool` | `code_exec` | 执行代码 |
| `WebBrowseTool` | `web_browse` | 网页抓取（按 `tool_max_content_length` 截断） |

> 工具实例化不依赖外部服务（API Key 在执行时才需要），因此个别工具注册失败不影响启动。

### 设计原则

1. **工具级并发信号量**：限制单任务内最大并发工具调用数（`agent_max_concurrent_tools`），是 **Agent 维度（GPU / 服务器资源）**，而非 LLM API 维度（RPM / TPM 由 `reservation_limiter` 覆盖）
2. **`async with` 保证释放**：信号量在 `execute` 入口获取，异常 / 取消时自动释放，不会挂死占坑
3. **幂等装配**：`init_default_tools` 按**注册 key（实例 `tool.name`）**判断是否已存在，重复调用不重复注册

---

## 核心类与方法

### `ToolStats` — 工具执行统计

| 字段 / 属性 | 类型 | 说明 |
| --- | --- | --- |
| `call_count` | `int` | 调用次数 |
| `success_count` | `int` | 成功次数 |
| `failed_count` | `int` | 失败次数 |
| `total_time` | `float` | 总耗时（秒） |
| `last_call_time` | `float \| None` | 最后调用时间戳 |
| `success_rate` | `property` | 成功率 = `success_count / call_count` |
| `avg_time` | `property` | 平均耗时 = `total_time / call_count` |

### `ToolService` 方法

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `register` | `(tool: BaseTool) -> None` | 注册工具；重名抛 `ValueError("工具 '...' 已存在")`，同时初始化统计 |
| `unregister` | `(name: str) -> bool` | 注销工具及其统计，返回是否成功 |
| `get` | `(name: str) -> BaseTool \| None` | 获取工具实例 |
| `list_tools` | `() -> list[str]` | 列出全部已注册工具名 |
| `get_openai_tools` | `() -> list[dict[str, Any]]` | OpenAI Tool Schema 列表（`type: "function"` + `function`） |
| `get_openai_responses` | `() -> list[dict[str, Any]]` | OpenAI Response Schema 列表（工具响应场景） |
| `execute` | `(name, parameters, timeout=None, max_retries=None, retry_delay=1.0) -> ToolResult` | 信号量保护内执行工具（见下方执行流程） |
| `_execute_impl` | 同 `execute`（不含信号量） | 实际执行逻辑，`execute` 在信号量内调用 |
| `get_stats` | `(name=None) -> dict[str, ToolStats] \| ToolStats \| None` | 单工具或全量统计 |
| `get_all_stats_summary` | `() -> dict[str, Any]` | 总调用 / 总成功 / 总失败 / 总成功率 / 各工具详情摘要 |
| `add_execution_hook` | `(hook: Callable) -> None` | 注册执行钩子 `async def hook(tool_name, parameters, result)` |
| `_run_hooks` | `(tool_name, parameters, result) -> None` | 运行全部钩子；单个钩子异常仅打印，不影响工具执行 |
| `init_default_tools` | `() -> list[str]` | 注册全部内置工具（幂等），返回本次新增的工具**类名**列表 |

> **注意**：`init_default_tools` 返回的是 `app.tools.builtin.__all__` 中的**类名**（如 `SearchTool`），而非注册 key（`tool.name`，如 `search`）。

---

## 关键实现详解

### `execute` 执行流程

```text
execute(name, parameters, timeout, max_retries, retry_delay)
  async with self._tool_semaphore            # 工具级并发信号量
    1. 补默认：timeout=settings.tool_timeout（30s）、max_retries=settings.tool_max_retries（3）
    2. 查工具：未注册 → ToolResult(success=False, error="工具 '...' 未注册")
    3. 参数解析：str → json.loads（失败 → "参数 JSON 解析失败"）
    4. 前置验证：tool.validate_parameters(**parameters)
       · 失败 → ToolResult(success=False, error="参数验证失败")
    5. 重试循环 for attempt in range(max_retries)：
       · asyncio.wait_for(tool.execute(**parameters), timeout)
       · 成功 → 填充 execution_time / retry_count → _record_stats + _run_hooks → 返回
       · 返回失败（如文件不存在）→ 记 error，_record_stats(success=False)
       · 超时 / 异常 → 记 error，_record_stats(success=False)
       · attempt < max_retries-1 → 指数退避 asyncio.sleep(retry_delay * 2^attempt)（1s, 2s, 4s...）
    6. 全部失败 → 返回 last_result 或新构造 ToolResult，retry_count = actual_retries
```

**重试语义**：

- 循环 `range(max_retries)`：默认 `tool_max_retries=3` 意味着最多执行 3 次（含首次）
- **成功返回 `result.success == True` 立即返回**；`success == False`（工具正常返回失败）也进入重试
- 退避为**渐进式指数退避**（`retry_delay * 2^attempt`），与 LLM 层的「退避 + 抖动」不同——工具层无抖动
- `execution_time` 记录单次真实执行耗时（`time.monotonic()` 差值），`retry_count` 记录实际尝试次数

### 并发控制语义

- `_tool_semaphore = asyncio.Semaphore(settings.agent_max_concurrent_tools)`（默认 3）
- 限制的是**单任务内最大并发工具调用数**，保护 GPU / 服务器资源
- `async with` 天然保证异常 / 取消时释放信号量，不会挂死占坑

### 统计记录 `_record_stats`

- 每次真实尝试（成功 / 失败 / 超时 / 异常）都会 `call_count += 1`，累计 `total_time`、更新 `last_call_time`
- 成功则 `success_count += 1`，否则 `failed_count += 1`——`call_count == success_count + failed_count`
- 未被 `register` 过的工具名（理论上不会发生）会惰性创建 `ToolStats()`

### 钩子机制 `_run_hooks`

- 签名约定：`async def hook(tool_name, parameters, result)`（`tool_name` 为注册 key）
- `asyncio.iscoroutinefunction(hook)` 判断异步 / 同步，分别 `await` 或直接调用
- 单个钩子抛异常仅打印 `钩子执行失败: {e}`，**不影响工具执行结果与后续钩子**
- 钩子只在**执行成功**（`result.success`）时运行

### Schema 导出

```python
# get_openai_tools（OpenAI Chat Completions tools 参数）
{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}

# get_openai_responses（OpenAI Responses / tool_calls 响应场景）
{"type": "function", "name": ..., "description": ..., "parameters": ...}
```

- 均委托 `BaseTool.to_openai_tool()` / `to_openai_response()`，数据源为工具的 `name` / `description` / `parameters`（见 [工具层文档](../../tool_doc/tools.md)）

### 内置工具装配 `init_default_tools`

```python
pkg = importlib.import_module("app.tools.builtin")
for name in builtin_tool_names:          # 来自 app.tools.builtin.__all__（自动发现）
    tool_cls = getattr(pkg, name)
    tool = tool_cls()
    if self.get(tool.name) is not None:  # 幂等：按实例 tool.name 判断，而非类名
        continue
    self.register(tool)
    registered.append(name)
```

- `app.tools.builtin.__all__` 由 `_discover_tools()` 自动扫描 `builtin` 包下所有 `BaseTool` 子类生成
- **幂等判断用实例 `tool.name`**（如 `"search"`），而非类名（`SearchTool`）——两者不同，避免重复注册
- 单工具实例化失败会中断装配循环，`AppState` 捕获后降级（`[WARN] 工具初始化失败`）

### 边缘情况

- `parameters` 传 JSON 字符串时，解析失败返回失败结果而非抛异常
- `validate_parameters` 默认实现只做「异常参数」与「必填缺失」检查（见 [base.py](../../../app/tools/base.py)）
- 并发下统计 `_record_stats` 为同步字典更新，`ToolService` 实例全局共享，多任务并发时统计为尽力而为（无锁）

---

## 使用示例

```python
# 执行内置工具（search / readFile / writeFile / code_exec / web_browse）
result = await app_state.tool_service.execute(
    name="search",
    parameters={"query": "良率 RCA 案例"},
    timeout=30,
    max_retries=3,
)
print(result.success, result.content, result.execution_time, result.retry_count)

# 获取 OpenAI 格式工具列表（注入 LLM tools 参数）
tools = app_state.tool_service.get_openai_tools()

# 注册自定义钩子（钩子失败不影响工具执行）
async def log_hook(tool_name, parameters, result):
    print(f"[hook] {tool_name} -> {result.success}")

app_state.tool_service.add_execution_hook(log_hook)

# 统计摘要
summary = app_state.tool_service.get_all_stats_summary()
# → {"total_calls": ..., "total_success": ..., "total_failed": ..., "overall_success_rate": ...,
#    "tools": {"search": {"call_count": ..., "success_rate": ..., "avg_time": ..., "last_call_time": ...}}}

# 内置工具装配（幂等，AppState 启动时调用）
registered = app_state.tool_service.init_default_tools()
```

> 实际调用方：`app/core/agent/executor.py`（ReActAgent 并行执行工具）、`app/api/routes/chat.py`（构造 ReActAgent 时注入 `tool_service`）。

---

## 配置关联

相关配置集中在 `app/config/settings.py`（详见 [config 文档](../../config.md)）：

| 配置项 | 默认值 | 使用位置 | 说明 |
| --- | --- | --- | --- |
| `agent_max_concurrent_tools` | `3` | `__init__` 信号量 | 单任务最大并发工具数 |
| `tool_timeout` | `30` | `execute` 默认超时（秒） | 单次执行超时 |
| `tool_max_retries` | `3` | `execute` 默认重试次数 | 最大执行次数（含首次） |
| `tool_max_output_length` | `100000` | 内置工具（readFile / code_exec） | 工具输出最大字符数 |
| `tool_max_content_length` | `50000` | 内置工具（web_browse） | 网页抓取最大字符数 |

> ToolService 在构造时读取 `agent_max_concurrent_tools` 创建信号量；`timeout` / `max_retries` 为调用方可覆盖的默认值。内置工具在各自 `execute` 内读取 `tool_max_output_length` / `tool_max_content_length`。

---

## 相关文档

- [服务层总览](../service.md)（ToolService 在服务层的定位）
- [工具层说明](../../tool_doc/tools.md)（BaseTool / ToolResult / 内置工具）
- [LLM 层说明](../llm_doc/llm.md)（`get_openai_tools()` 产物的下游消费方）
- [Agent 模块](../../core_doc/agent_doc/agent.md)（`_execute_tool_calls` 并行执行，本模块上游调用方）
- [核心层说明](../../core_doc/core.md)
- [架构设计](../../architecture.md)
- [配置说明](../../config.md)
- [HANDOFF](../../HANDOFF.md)
