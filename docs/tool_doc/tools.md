# 工具模块说明文档

## 📋 目录

- [模块概述](#模块概述)
- [架构设计](#架构设计)
- [快速开始](#快速开始)
- [核心组件详解](#核心组件详解)
- [内置工具详解](#内置工具详解)
- [注册中心详解](#注册中心详解)
- [如何开发新工具](#如何开发新工具)
- [并发控制](#并发控制)
- [自动重试机制](#自动重试机制)
- [执行统计](#执行统计)
- [最佳实践](#最佳实践)
- [常见问题](#常见问题)

---

## 模块概述

### 核心功能

工具模块为 Agent 提供可执行的能力集合，让 LLM 能够与外部世界交互：

- **统一接口**：所有工具遵循 `BaseTool` 抽象基类
- **自动发现**：新增工具只需在 `builtin/` 目录下创建文件，系统自动识别
- **参数验证**：执行前自动校验参数是否符合定义的 JSON Schema
- **自动重试**：失败自动重试，带渐进式退避策略
- **执行统计**：自动记录调用次数、成功率、平均耗时
- **超时保护**：防止工具执行卡死整个系统
- **钩子机制**：支持在工具执行前后插入自定义逻辑（日志、监控）

### 模块结构

```
app/tools/
├── __init__.py          # 模块导出（BaseTool, ToolResult）
├── base.py              # 基类定义（BaseTool, ToolResult）
├── builtin/             # 内置工具（自动发现）
│   ├── __init__.py      # 自动扫描目录，发现 BaseTool 子类
│   ├── search.py        # 网络搜索（Tavily API）
│   ├── file_ops.py      # 文件读写
│   ├── code_exec.py     # 终端命令执行
│   └── web_browse.py    # 网页内容抓取
└── external/            # 外部工具（预留，按需加载）
    └── __init__.py

app/services/tool_service.py   ← 工具服务统一入口（ToolService，原 ToolRegistry 合并于此）
```

---

## 架构设计

### 设计理念

1. **面向接口编程**
   - 所有工具实现 `BaseTool` 抽象基类
   - Agent 层只依赖接口，不依赖具体实现
   - 新增工具不影响现有代码

2. **统一入口**
   - 所有工具通过 `ToolService` 执行（工具系统对外唯一入口）
   - 统一实现：参数验证、超时、重试、统计、日志
   - 调用方不需要关心内部复杂性

3. **分层设计**

   ```
   Agent 层 (LLM 运行时)
       │ 调用 tool_service.execute("search", ...)
       ▼
   服务层 (ToolService)
       │ 参数验证 → 执行 → 重试 → 统计
       ▼
   Tool 层 (BaseTool)
       │ execute()
       ▼
   基础设施 (Tavily / httpx / aiofiles / subprocess)
   ```

4. **builtin / external 分离**
   - **builtin**：系统内置工具，随系统发布，开箱即用
   - **external**：第三方工具预留，按需加载，可热插拔

---

## 快速开始

### 执行一个内置工具

```python
import asyncio
from app.tools.builtin import SearchTool
from app.services import ToolService

async def main():
    reg = ToolService()

    # 1. 注册工具
    reg.register(SearchTool())

    # 2. 执行工具
    result = await reg.execute("search", {"query": "Python asyncio 教程"})
    print(result.content)

    # 3. 查看统计
    stats = reg.get_stats("search")
    print(f"调用次数: {stats.call_count}")
    print(f"成功率: {stats.success_rate:.2%}")
    print(f"平均耗时: {stats.avg_time:.2f}s")

asyncio.run(main())
```

### 一次性注册所有内置工具

```python
import asyncio
from app.tools.builtin import __all__ as builtin_tools
from app.services import ToolService
from app.tools.base import BaseTool

async def main():
    reg = ToolService()

    # 根据 __all__ 中的类名动态导入并注册
    import importlib
    pkg = importlib.import_module("app.tools.builtin")
    for tool_name in builtin_tools:
        tool_cls: type[BaseTool] = getattr(pkg, tool_name)
        reg.register(tool_cls())

    # 查看已注册的工具
    print("已注册工具:", reg.list_tools())

asyncio.run(main())
```

---

## 核心组件详解

### `ToolResult` — 工具执行结果

```python
@dataclass
class ToolResult:
    success: bool                   # 是否执行成功
    content: str                    # 执行结果内容
    error: str | None               # 错误信息
    metadata: dict | None           # 额外元数据
    execution_time: float | None    # 执行耗时（秒），注册中心自动填充
    retry_count: int                # 实际重试次数，注册中心自动填充
```

**设计意图：**

| 字段             | 谁消费    | 用途                                          |
| ---------------- | --------- | --------------------------------------------- |
| `success`        | Agent 层  | 快速判断执行状态，而不是检查 `error` 是否为空 |
| `content`        | LLM       | 作为工具调用的观察结果传给 LLM                |
| `error`          | Agent 层  | 失败时的详细错误信息                          |
| `metadata`       | 钩子/日志 | 携带执行上下文（状态码、来源、数量等）        |
| `execution_time` | 统计/监控 | 工具性能分析                                  |
| `retry_count`    | 统计/调试 | 了解工具稳定性和重试效果                      |

### `BaseTool` — 工具抽象基类

```python
class BaseTool(ABC):
    name: str            # 工具名称（唯一标识），LLM 通过它调用
    description: str     # 工具描述，LLM 根据它判断何时该用
    parameters: dict     # JSON Schema，LLM 根据它构造参数
    execute(**kwargs)    # 异步执行逻辑
```

**每个抽象方法的职责：**

| 方法          | 返回值       | 谁消费        | 说明                                         |
| ------------- | ------------ | ------------- | -------------------------------------------- |
| `name`        | `str`        | LLM、注册中心 | 唯一标识，如 `"search"`、`"readFile"`        |
| `description` | `str`        | LLM           | 描述工具功能和适用场景，LLM 据此决定是否调用 |
| `parameters`  | `dict`       | LLM           | OpenAI Function Calling 格式的 JSON Schema   |
| `execute`     | `ToolResult` | 注册中心      | 实际执行逻辑，必须是异步方法                 |

### `to_openai_tool()` vs `to_openai_response()`

| 方法                   | 格式                                                                | 适用 API             |
| ---------------------- | ------------------------------------------------------------------- | -------------------- |
| `to_openai_tool()`     | `{"type": "function", "function": {name, description, parameters}}` | Chat Completions API |
| `to_openai_response()` | `{"type": "function", name, description, parameters}`               | Responses API        |

### `validate_parameters()` — 参数验证

每个工具 `execute()` 执行前，注册中心会自动调用验证：

```python
# 验证逻辑
# 1. 检查是否有未知参数（kwargs 中的 key 不在 properties 中）
# 2. 检查必填参数是否都传了（required 列表中的每个参数都存在）
```

验证不通过时**直接返回** `ToolResult(success=False, error="参数验证失败: ...")`，不会执行工具逻辑。

---

## 内置工具详解

### 1. `SearchTool` — 网络搜索

| 项目     | 说明                                                     |
| -------- | -------------------------------------------------------- |
| 名称     | `search`                                                 |
| 依赖     | Tavily API（需配置 `TAVILY_API_KEY`）                    |
| 参数     | `query: string`（必填）                                  |
| 执行方式 | 同步 SDK 通过 `asyncio.to_thread` 异步化，不阻塞事件循环 |
| 返回     | 优先返回 Tavily 直接答案，否则返回格式化结果列表         |

**使用场景：** 需要回答时事、事实、代码库中找不到的信息时。

### 2. `ReadFileTool` — 读取文件

| 项目     | 说明                                                                |
| -------- | ------------------------------------------------------------------- |
| 名称     | `readFile`                                                          |
| 参数     | `file_path: string`（必填，绝对路径）                               |
| 执行方式 | `aiofiles.open()` 异步读取                                          |
| 安全限制 | 输出长度受 `settings.tool_max_output_length` 控制（默认 100K 字符） |

### 3. `WriteFileTool` — 写入文件

| 项目     | 说明                                                     |
| -------- | -------------------------------------------------------- |
| 名称     | `writeFile`                                              |
| 参数     | `file_path: string`（必填）、`content: string`（必填）   |
| 执行方式 | `aiofiles.open()` 异步写入                               |
| 特性     | 自动创建父目录（`os.makedirs(dir_path, exist_ok=True)`） |

### 4. `CodeExecTool` — 终端命令执行

| 项目     | 说明                                                 |
| -------- | ---------------------------------------------------- |
| 名称     | `code_exec`                                          |
| 参数     | `command: string`（必填）、`workdir: string`（可选） |
| 执行方式 | `asyncio.create_subprocess_shell` 异步子进程         |
| 安全限制 | 危险命令黑名单、空命令拦截、输出截断 100K 字符       |

**危险命令黑名单（禁止执行）：**

```
rm -rf /, rm -rf /*, rm -rf ~, rm -rf .
mkfs., dd if=, :(){ :|:& };:
shutdown, reboot, halt, poweroff, init 0, init 6
mv /, > /dev/sda
```

**配置关联：**

- 输出截断长度：`settings.tool_max_output_length`
- 执行超时：通过注册中心 `timeout` 参数传递，默认 `settings.tool_timeout`

### 5. `WebBrowseTool` — 网页内容抓取

| 项目      | 说明                                                                       |
| --------- | -------------------------------------------------------------------------- |
| 名称      | `web_browse`                                                               |
| 参数      | `url: string`（必填）                                                      |
| 执行方式  | `httpx.AsyncClient`（全局单例客户端，复用连接池）                          |
| HTML 解析 | 自实现 `_HTMLToTextParser`，基于标准库 `html.parser`，零额外依赖           |
| 返回内容  | 标题、来源 URL、纯文本正文、页面链接列表                                   |
| 安全限制  | 内容截断 `settings.tool_max_content_length`、超时 15s、自动补全 `https://` |

**Parser 特性：**

- 跳过 `<script>` / `<style>` 内容
- 识别块级元素自动换行（p, div, h1-h6, li, blockquote, pre）
- 提取 `<a>` 链接并转为 Markdown 格式
- HTML 实体解码使用 `html.unescape()`（Python 3.9+ 兼容）
- 链接去重、展示文本截断 80 字符

---

## ToolService 详解

### `ToolService` 主要方法

| 方法                       | 作用                     | 使用场景               |
| -------------------------- | ------------------------ | ---------------------- |
| `register(tool)`           | 注册工具                 | 应用启动时注册         |
| `unregister(name)`         | 注销工具                 | 动态移除/热替换        |
| `get(name)`                | 查找工具                 | 直接获取工具实例       |
| `list_tools()`             | 列出所有注册工具         | 管理界面               |
| `get_openai_tools()`       | OpenAI Chat 格式列表     | 传给 LLM 的 tools 参数 |
| `get_openai_responses()`   | OpenAI Response 格式列表 | 传给 LLM 的 tools 参数 |
| `execute(name, params)`    | 执行工具（核心入口）     | Agent 层调用           |
| `get_stats(name)`          | 获取工具统计             | 监控/调试              |
| `get_all_stats_summary()`  | 获取全量统计摘要         | 监控面板               |
| `add_execution_hook(hook)` | 添加执行钩子             | 日志/监控              |

### `execute()` 执行流程

```
execute("search", {"query": "..."})
    │
    ├─ 1. 使用配置默认值（从 settings 读取 tool_timeout / tool_max_retries）
    │
    ├─ 2. 查找工具 get("search")
    │     └─ 未注册 → 返回 ToolResult(success=False)
    │
    ├─ 3. 解析参数（字符串 → 字典）
    │
    ├─ 4. 参数前置验证 tool.validate_parameters()
    │     └─ 不通过 → 返回 ToolResult(success=False)
    │
    ├─ 5. 循环：for attempt in range(max_retries)
    │     ├─ asyncio.wait_for(tool.execute(**params), timeout)
    │     ├─ 成功 → 填充 execution_time / retry_count
    │     │        → 记录统计 → 执行钩子 → 返回结果
    │     ├─ 返回 success=False → 记录错误，重试
    │     ├─ 超时 TimeoutError → 记录错误，重试
    │     └─ 异常 Exception → 记录错误，重试
    │
    └─ 6. 所有重试均失败 → 返回 last_error
```

### `ToolStats` — 执行统计

```python
@dataclass
class ToolStats:
    call_count: int          # 调用次数
    success_count: int       # 成功次数
    failed_count: int        # 失败次数
    total_time: float        # 总耗时（秒）
    last_call_time: float    # 最后调用时间戳

    # 计算属性
    success_rate -> float    # 成功率
    avg_time -> float        # 平均耗时（秒）
```

**统计摘要 `get_all_stats_summary()` 返回：**

```python
{
    "total_calls": 100,
    "total_success": 92,
    "total_failed": 8,
    "overall_success_rate": 0.92,
    "tools": {
        "search": {"call_count": 50, "success_rate": 0.98, "avg_time": 1.23, "last_call_time": 1.7e9},
        "readFile": {"call_count": 30, "success_rate": 0.85, "avg_time": 0.05, "last_call_time": 1.7e9},
        ...
    }
}
```

### 钩子机制

钩子函数用于在工具执行后插入自定义逻辑（日志、监控、审计）：

```python
async def logging_hook(tool_name: str, params: dict, result: ToolResult):
    """日志钩子示例"""
    print(f"[{tool_name}] 执行耗时: {result.execution_time}s, 成功: {result.success}")

# 注册钩子
reg.add_execution_hook(logging_hook)
```

钩子的关键特性：

- 支持同步和异步函数
- 钩子失败不影响工具执行
- 可以注册多个钩子，按注册顺序执行

---

## 如何开发新工具

### 步骤 1：在 `builtin/` 下创建文件

```python
# app/tools/builtin/current_time.py
from typing import Any
from datetime import datetime

from ..base import BaseTool, ToolResult


class CurrentTimeTool(BaseTool):
    """当前时间工具"""

    @property
    def name(self) -> str:
        return "get_current_time"

    @property
    def description(self) -> str:
        return "获取当前的日期和时间。当你需要知道现在是几点或什么日期时使用。"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "format": {
                    "type": "string",
                    "description": "时间格式，如 '%Y-%m-%d %H:%M:%S'",
                }
            },
            "required": [],
        }

    async def execute(self, **kwargs) -> ToolResult:
        if not self.validate_parameters(**kwargs):
            return ToolResult(success=False, content="", error="参数有误")

        fmt = kwargs.get("format", "%Y-%m-%d %H:%M:%S")
        now = datetime.now().strftime(fmt)
        return ToolResult(success=True, content=now)
```

### 步骤 2：系统自动发现

由于 `builtin/__init__.py` 使用自动发现机制，只需创建文件即可：

```python
from app.tools.builtin import CurrentTimeTool  # 直接可用
```

### 开发规范

1. **继承 `BaseTool`**，实现 4 个抽象方法
2. **`name`**：小写+下划线，全局唯一
3. **`description`**：清晰描述工具功能和适用场景，LLM 据此决定何时调用
4. **`parameters`**：使用 OpenAI Function Calling 的 JSON Schema 格式
5. **`execute`**：必须是 `async def`，返回 `ToolResult`
6. **参数验证**：总是调用 `self.validate_parameters(**kwargs)` 保护
7. **错误处理**：所有异常捕获为 `ToolResult(success=False, error=...)`，不要让异常抛出

---

## 并发控制

注册中心实现了**工具级并发信号量**，限制单任务内最大并发工具调用数：

```python
# ToolService.__init__
self._tool_semaphore = asyncio.Semaphore(settings.agent_max_concurrent_tools)
```

- **并发上限**：`agent_max_concurrent_tools`（默认 3），同一时刻最多 N 个工具同时执行
- **并发度来源**：`ReActAgent._execute_tool_calls` 用 `asyncio.gather` 并行执行工具，信号量限制并发数
- **超时/异常释放**：`async with self._tool_semaphore` 天然保证异常/取消时释放信号量，不会挂死占坑

> **信号量在 execute() 入口**：所有工具调用（含 Agent、未来非 Agent 调用方）统一经过此限制。

---

## 自动重试机制

注册中心实现了**渐进式退避**的重试策略：

| 重试次数      | 等待时间                |
| ------------- | ----------------------- |
| 第 1 次失败后 | `retry_delay * 2⁰` = 1s |
| 第 2 次失败后 | `retry_delay * 2¹` = 2s |
| 第 3 次失败后 | `retry_delay * 2²` = 4s |

**触发重试的条件：**

- 工具返回 `success=False`（文件不存在、API 错误等）
- 抛出 `TimeoutError`（执行超时）
- 抛出 `Exception`（运行时异常）

**不触发的条件：**

- 参数验证失败（不会执行，直接返回）
- 工具未注册（查找不到，直接返回）
- 参数 JSON 解析失败（直接返回）

---

## 执行统计

统计信息自动记录，无需额外配置。

### 按工具查询

```python
# 获取单个工具统计
stats = reg.get_stats("search")
print(stats.call_count)     # 调用次数
print(stats.success_rate)   # 成功率
print(stats.avg_time)       # 平均耗时

# 查看所有工具统计
all_stats = reg.get_stats()  # 返回 dict[str, ToolStats]
```

### 全量摘要

```python
summary = reg.get_all_stats_summary()
print(summary["total_calls"])             # 总调用数
print(summary["overall_success_rate"])    # 整体成功率
print(summary["tools"])                   # 各工具详情
```

### 统计重置时机

- 工具注销后，该工具统计自动清除
- 注册中心实例销毁后，统计重置
- 重启应用后，统计重置

---

## 最佳实践

### 1. 工具描述要精准

```python
# ❌ 不好的描述
description = "搜索工具"

# ✅ 好的描述
description = (
    "一个网页搜索引擎。当你需要回答关于时事、"
    "事实以及在你的知识库中找不到的信息时，应使用此工具。"
)
```

LLM 根据描述决定是否调用工具，足够详细的描述能提高 LLM 的调用准确率。

### 2. 参数格式用 OpenAI Schema

```python
# ✅ 标准的 Function Calling Schema
parameters = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "搜索关键词",
        }
    },
    "required": ["query"],
}
```

### 3. 内置 validate_parameters

```python
async def execute(self, **kwargs) -> ToolResult:
    if not self.validate_parameters(**kwargs):
        return ToolResult(success=False, content="", error=f"参数有误: {kwargs!s}")
```

虽然注册中心已做验证，但工具内部再加一道保障，解耦测试也更方便。

### 4. 小心同步 IO

```python
# ❌ 不要直接调用同步 SDK
response = tavily.search(query=query)  # 阻塞事件循环

# ✅ 用 asyncio.to_thread 包装
response = await asyncio.to_thread(tavily.search, query=query)
```

### 5. 输出截断

对于可能返回大量数据的工具（读文件、执行命令、抓网页），务必设置合理的截断长度，防止撑爆 Token 限制。

### 6. 资源复用

对于网络工具（如 `WebBrowseTool`），复用 HTTP 客户端连接池，避免每次执行都建立新连接。

---

## 常见问题

### Q1: 如何为工具设置不同的超时时间？

```python
# 注册中心 execute 方法接受 timeout 参数
result = await reg.execute("search", {"query": "..."}, timeout=60)
```

如果不传，默认使用 `settings.tool_timeout`（30 秒）。

### Q2: 如何禁用某个内置工具？

```python
reg = ToolService()
reg.register(SearchTool())
# 不注册 web_browse，Agent 就无法调用它
# reg.register(WebBrowseTool())
```

### Q3: 如何在工具执行前后加入自定义逻辑？

使用执行钩子：

```python
def audit_hook(tool_name, params, result):
    print(f"[AUDIT] {tool_name} 被调用, 参数: {params}, 结果: {result.success}")

reg.add_execution_hook(audit_hook)
```

### Q4: 工具执行失败会自动重试吗？

会。默认重试 3 次（`settings.tool_max_retries`），使用渐进式退避。只有以下情况不重试：

- 参数验证失败
- 工具未注册
- JSON 解析失败

### Q5: 如何添加一个使用外部 API 的工具？

```python
class WeatherTool(BaseTool):
    name = "get_weather"
    description = "获取指定城市的天气信息。"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "city": {"type": "string", "description": "城市名称"},
            },
            "required": ["city"],
        }

    async def execute(self, **kwargs) -> ToolResult:
        if not self.validate_parameters(**kwargs):
            return ToolResult(success=False, content="", error="参数有误")

        try:
            # 使用 httpx 异步请求外部 API
            async with httpx.AsyncClient() as client:
                resp = await client.get(f"https://api.weather.com/{kwargs['city']}")
                return ToolResult(success=True, content=resp.text)
        except Exception as e:
            return ToolResult(success=False, content="", error=str(e))
```

### Q6: 工具命名有什么规范？

- 使用小写字母 + 下划线：`search`、`read_file`、`get_current_time`
- 名称应反映工具功能，便于 LLM 理解
- 全局唯一，不能重复注册

### Q7: 如何关闭输出截断？

不推荐。但如果需要，可以在配置中调大截断阈值：

```bash
# .env
TOOL_MAX_OUTPUT_LENGTH=1000000
TOOL_MAX_CONTENT_LENGTH=500000
```

---

## 配置关联

工具模块与 `settings.py` 中的以下配置项关联：

| 配置项                    | 默认值  | 影响范围                        |
| ------------------------- | ------- | ------------------------------- |
| `TOOL_TIMEOUT`            | 30      | registry.execute() 默认超时     |
| `TOOL_MAX_RETRIES`        | 3       | registry.execute() 默认重试次数 |
| `TOOL_MAX_OUTPUT_LENGTH`  | 100000  | readFile、code_exec 输出截断    |
| `TOOL_MAX_CONTENT_LENGTH` | 50000   | web_browse 内容截断             |
| `TAVILY_API_KEY`          | ""      | SearchTool API 密钥             |
| `TAVILY_SEARCH_DEPTH`     | "basic" | SearchTool 搜索深度             |

---

## 相关文档

- [配置管理模块说明](../config_doc/config.md)
- [架构设计文档](../architecture.md)
- [API 文档](../api_doc/api.md)
- [内置工具详解](builtin_doc/builtin.md)（BaseTool + 5 个内置工具）
