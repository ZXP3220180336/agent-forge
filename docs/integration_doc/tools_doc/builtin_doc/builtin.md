# builtin 内置工具子模块说明

> **更新日期**：2026-08-17
> **文档定位**：工具层 `app/integration/tools/builtin/` 子模块 —— 内置工具的定义、自动发现机制与各工具实现详解。
> **实现状态**：SearchTool（✅）/ ReadFileTool（✅）/ WriteFileTool（✅）/ CodeExecTool（✅）/ WebBrowseTool（✅）/ RCA 5 工具（✅，见 [rca.md](rca.md)）
> **前置阅读**：[工具模块总览](../tools.md)（ToolService / ToolExecutor 并发控制、重试机制在此说明，本文不重复）

---

## 📋 目录

- [模块概述](#模块概述)
- [自动发现机制](#自动发现机制)
- [BaseTool 基类详解](#basetool-基类详解)
- [ToolResult 数据结构](#toolresult-数据结构)
- [各内置工具详解](#各内置工具详解)
- [外部工具（热加载）](#外部工具热加载)
- [开发新工具要点](#开发新工具要点)
- [相关文档](#相关文档)

---

## 模块概述

`builtin` 是工具层 `app/integration/tools/` 下的**内置工具子模块**，存放随系统发布、开箱即用的工具实现，与 `external` 子模块（第三方工具，热加载，见 [external.md](../external.md)）互为补充。

```text
app/integration/tools/
├── __init__.py          # 模块导出（BaseTool / ToolService / ResultProcessor 等 11 项）
├── base.py              # 基类定义（BaseTool + 元数据；ToolResult 定义于领域端口）
├── builtin/             # 内置工具（自动发现）        ← 本文档
│   ├── __init__.py      # 自动扫描目录，发现 BaseTool 子类
│   ├── search.py        # SearchTool    网络搜索（Tavily API）
│   ├── file_ops.py      # ReadFileTool / WriteFileTool  文件读写
│   ├── code_exec.py     # CodeExecTool  终端命令执行（危险命令黑名单）
│   ├── web_browse.py    # WebBrowseTool 网页内容抓取（HTML 解析）
│   └── rca/             # 良率 RCA 场景工具（5 工具，见 rca.md）
└── external/            # 外部工具（热加载，见 ../external.md）
```

> 完整工具模块目录（含 executor / registry / validator / result_processor / security / selector / stats / hooks / assembler / tool_service）见 [工具模块接口文档](../tools.md)。

**核心特性：**

- **零注册成本**：新增工具只需在 `builtin/` 下创建文件并继承 `BaseTool`，无需修改任何注册代码
- **自动发现**：`builtin/__init__.py` 在包导入时自动扫描目录，收集所有可实例化的 `BaseTool` 子类
- **惰性属性访问**：通过模块级 `__getattr__` / `__dir__` 支持 `from app.integration.tools.builtin import SearchTool` 式导入
- **独立职责**：每个工具只实现 `BaseTool` 的抽象接口，参数验证、超时、重试、统计、并发控制统一由 [ToolExecutor](../executor.md)（经 ToolService 门面）负责

**架构定位**：工具层在架构分层中位于服务层之下、基础设施之上。

```text
Agent 层 (LLM 运行时)
    │ 调用 tool_service.execute("search", ...)
    ▼
执行调度 (ToolExecutor，经 ToolService 门面)  ← 参数验证 / 超时 / 重试 / 统计 / 信号量
    ▼
Tool 层 (BaseTool)            ← builtin 子模块，每个工具一个 execute()
    ▼
基础设施 (Tavily / httpx / aiofiles / subprocess)
```

---

## 自动发现机制

`builtin/__init__.py`（62 行）实现了一套「扫描目录 + 反射收集 + 惰性访问」的自动发现机制。

### 发现流程

```python
_tool_classes: dict[str, type[BaseTool]] = {}   # 类名 → 工具类 的缓存

def _discover_tools() -> dict[str, type[BaseTool]]:
    if _tool_classes:
        return _tool_classes          # 已发现则直接返回缓存

    package_path = __path__[0]        # builtin 包的物理目录
    for _, module_name, _ in pkgutil.iter_modules([package_path]):
        if module_name.startswith("_"):
            continue                  # 跳过下划线开头的私有模块

        try:
            module = importlib.import_module(f".{module_name}", __package__)
        except Exception as e:  # noqa: BLE001
            logger.warning("内置工具模块导入失败，跳过 %s: %s", module_name, e)
            continue                  # 单个模块导入失败不影响其它模块

        for name, obj in inspect.getmembers(module, inspect.isclass):
            if (issubclass(obj, BaseTool)
                    and obj is not BaseTool
                    and not getattr(obj, "__abstractmethods__", None)):
                _tool_classes[name] = obj
    return _tool_classes
```

**过滤规则（三个条件同时满足才收录）：**

| 条件 | 作用 |
| --- | --- |
| `issubclass(obj, BaseTool)` | 必须是 `BaseTool` 的子类（也排除了普通辅助类，如 `web_browse.py` 内部的 `_HTMLToTextParser`） |
| `obj is not BaseTool` | 排除基类本身 |
| `not getattr(obj, "__abstractmethods__", None)` | 排除仍含抽象方法（未实现 4 个抽象接口）的类，只收集可实例化的具体工具类 |

**注意事项：**

- 字典的键是**类名**（如 `"SearchTool"`），不是工具的 `name` 属性（如 `"search"`）——二者在 `BaseTool` 设计中是分离的
- 模块导入失败会**记录 warning 后跳过**（`except Exception` + `logger.warning`），因此单个工具文件有语法/依赖错误时，其余工具仍可正常发现，且失败会暴露在日志中（`tools.builtin`）便于排障
- `inspect.getmembers` 会遍历模块内的所有类（含被 import 进来的类），但最终收录仍由 `issubclass` 过滤，实践中只有本文件定义的工具类符合条件

### 惰性访问与触发时机

```python
def __getattr__(name: str) -> type[BaseTool]:
    tools = _discover_tools()
    if name in tools:
        return tools[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

def __dir__() -> list[str]:
    return list(_discover_tools().keys())

# 模块加载时执行发现
__all__ = list(_discover_tools().keys())
```

- 文件末尾的 `__all__ = list(_discover_tools().keys())` 在**包导入时立即触发一次全量发现**并填充缓存，因此 `_discover_tools` 的缓存分支在后续调用中直接命中
- `__getattr__` 是 Python 模块级属性访问兜底：`from app.integration.tools.builtin import SearchTool` 在普通属性查找失败后走到这里，从缓存返回工具类；未收录的名字抛出 `AttributeError`
- `__dir__` 让 `dir(app.integration.tools.builtin)` 能列出所有已发现的工具类

**典型用法：**

```python
from app.integration.tools.builtin import SearchTool, WebBrowseTool   # 惰性加载单个类
from app.integration.tools.builtin import __all__ as builtin_tools    # 或遍历 __all__ 批量注册
```

---

## BaseTool 基类详解

`BaseTool`（`app/integration/tools/base.py`）是所有工具的抽象基类，定义了 4 个必须实现的抽象成员，并提供元数据属性、Schema 导出与参数校验委托。

### 抽象接口（子类必须实现）

```python
class BaseTool(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...                      # 工具名称（唯一标识）

    @property
    @abstractmethod
    def description(self) -> str: ...               # 工具描述（供 LLM 理解何时使用）

    @property
    @abstractmethod
    def parameters(self) -> dict[str, Any]: ...     # 工具参数 JSON Schema

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult: ...  # 执行逻辑（必须 async）
```

| 成员 | 返回值 | 谁消费 | 说明 |
| --- | --- | --- | --- |
| `name` | `str` | LLM、注册中心 | 唯一标识，如 `"search"`、`"readFile"` |
| `description` | `str` | LLM | 描述工具功能与适用场景，LLM 据此决定是否调用 |
| `parameters` | `dict` | LLM | OpenAI Function Calling 格式的 JSON Schema，LLM 据此构造参数 |
| `execute` | `ToolResult` | executor | 实际执行逻辑，必须是 `async def` |

### 元数据属性（分级标注 + 审计用，默认值由内置工具覆写）

| 属性 | 默认值 | 说明 |
| --- | --- | --- |
| `risk_level` | `L0_READONLY` | 风险分级（L0 只读 / L1 写 / L2 危险 / L3 禁用），见 [security.md](../security.md) |
| `category` | `"general"` | 功能域（search / file / code / web / ...），供按域查询 |
| `concurrency_safe` | `True` | 是否允许自身并发（写 / 子进程类应为 False → 串行化） |
| `requires_approval` | `False` | 是否需人工审批（executor 经 ApprovalGate 确认，默认放行） |
| `max_output_length` | `100_000` | 结果截断上限（ResultProcessor 消费），见 [result_processor.md](../result_processor.md) |
| `timeout` | `None` | 工具自声明默认超时（秒；None = 沿用全局 `tool_timeout`，调用方显式传入可覆盖），见 [executor.md](../executor.md) |

### 具体方法（基类提供，子类可覆写）

**`to_openai_tool()` → `to_openai_response()`**

两个方法都将工具描述转换成 OpenAI 兼容格式，区别仅在适配的 API：

| 方法 | 返回格式 | 适配 API |
| --- | --- | --- |
| `to_openai_tool()` | `{"type": "function", "function": {name, description, parameters}}` | Chat Completions API |
| `to_openai_response()` | `{"type": "function", name, description, parameters}` | Responses API |

**`validate_parameters(**kwargs) -> bool`**

委托 jsonschema 校验器做完整校验（类型 / 必填 / 枚举 / 范围 / 未知参数拒绝），返回布尔；`validation_issues(**kwargs)` 返回中文归因问题列表，供 executor 构造可归因错误（见 [validator.md](../validator.md)）。

> 实际执行链路上，executor 先调 `tool.validation_issues()` 做可归因校验（失败即返回错误），工具内部 `execute()` 开头再调 `validate_parameters()` 作为兜底。

---

## ToolResult 数据结构

`ToolResult`（`app/domain/ports/tool_gateway.py`）是工具执行的统一返回结构，`BaseTool.execute()` 必须返回它：

```python
@dataclass
class ToolResult:
    success: bool                   # 是否执行成功
    content: str                    # 执行结果内容（传给 LLM 的观察结果）
    error: str | None = None        # 失败时的详细错误信息
    error_code: ErrorCode | None = None   # 系统级失败分类（业务错误为 None，见 tools.md）
    metadata: dict[str, Any] | None = None   # 额外元数据（来源、状态码、截断标记等）
    execution_time: float | None = None      # 执行耗时（秒），executor 自动填充
    retry_count: int = 0                     # 实际执行次数（含首次，成功/失败口径一致），executor 自动填充
```

- `__str__`：成功时返回 `content`，失败时返回 `错误: {error}`
- **约定**：工具内所有异常都应捕获并包装为 `ToolResult(success=False, error=...)` 返回，**不要让异常抛出**；`execution_time` 与 `retry_count` 由 executor 填充，工具自身不设置
- `metadata["truncated"]`：结果被 ResultProcessor 截断时置 `True`

---

## 各内置工具详解

### 1. SearchTool — 网络搜索

**文件**：`app/integration/tools/builtin/search.py`｜**名称**：`search`

| 项目 | 说明 |
| --- | --- |
| 描述 | 网页搜索引擎，处理时事、事实及知识库外信息 |
| 参数 | `query: string`（必填） |
| 依赖 | Tavily API，需配置 `TAVILY_API_KEY`；未配置时直接返回失败 |
| 执行方式 | 同步 SDK 经 `asyncio.to_thread` 包装，**不阻塞事件循环** |
| 风险级 | L0 只读（category=search，并发安全） |
| 默认超时 | 15s（executor 外层保护） |

**实现要点：**

```python
response = await asyncio.to_thread(
    tavily.search,
    query=kwargs["query"],
    search_depth=self._search_depth,   # register_config 注入，默认 "basic"
    include_answer=True,
)
```

- 搜索深度取 `self._search_depth`（由装配根经 `register_config` 注入 settings 值，可选 `"basic"` / `"advanced"`）
- 返回**优先取直接答案**：`response.get("answer")` 非空时直接返回，`metadata["source"] = "tavily_answer"` 且 `metadata["urls"]` = 前 3 条来源 URL（证据链可回溯）
- 否则格式化搜索结果列表（`- 标题: 内容`，行尾追加 `（来源: url）`），`metadata` 记录 `source="tavily_search"` 与 `count`
- 无结果时返回 `"抱歉，没有找到相关信息。"`
- 任何异常统一捕获为 `ToolResult(success=False, error="执行 Tavily 搜索失败: ...")`

### 2. ReadFileTool — 读取文件

**文件**：`app/integration/tools/builtin/file_ops.py`｜**名称**：`readFile`

| 项目 | 说明 |
| --- | --- |
| 描述 | 读取指定路径的文本文件内容 |
| 参数 | `file_path: string`（必填，绝对路径） |
| 执行方式 | `aiofiles.open(path, encoding="utf-8")` 异步读取 |
| 风险级 | L0 只读（category=file，并发安全） |
| 默认超时 | 5s（本地读快） |
| 结果截断 | ResultProcessor 统一 head+tail 截断（`max_output_length`，默认 100_000） |
| 安全限制 | 允许目录白名单（register_config 注入，默认项目根）；白名单外拒绝访问 |

**实现要点：**

- 返回完整文件内容，截断由 [ResultProcessor](../result_processor.md) 统一处理（head+tail）
- **大文件分段读取**：`os.path.getsize` 预检，超阈值（单段 `max_output_length×3` 字节 × 2）时二进制 seek 分段读 head+tail（内存受限），保留首尾；最终截断标记仍由 ResultProcessor 统一生成
- **路径白名单**：`file_path` 经 `abspath + normcase` 规范化后必须位于允许目录内（等于或为子路径，`os.sep` 分隔防 `/data` 误放行 `/database`），防 `..` 穿越；白名单外返回业务错误
- `FileNotFoundError` → `"文件 '...' 未找到"`；其它异常 → `"读取文件失败: ..."`

### 3. WriteFileTool — 写入文件

**文件**：`app/integration/tools/builtin/file_ops.py`｜**名称**：`writeFile`

| 项目 | 说明 |
| --- | --- |
| 描述 | 将指定内容写入文本文件，文件不存在则创建 |
| 参数 | `file_path: string`（必填）、`content: string`（必填） |
| 执行方式 | `aiofiles.open(path, "w", encoding="utf-8")` 异步写入 |
| 特性 | **自动创建父目录**（`os.makedirs(dir_path, exist_ok=True)`） |
| 风险级 | L1 写（category=file，**非并发安全** → 同工具串行化） |
| 默认超时 | 5s（本地写快） |
| 安全限制 | 允许目录白名单（register_config 注入，默认项目根）；白名单外拒绝访问 |

**实现要点：**

- 写入前对 `file_path` 的目录部分执行 `os.makedirs(..., exist_ok=True)`，无需调用方预先建目录
- **路径白名单**：同 ReadFileTool，白名单外拒绝（不能覆盖项目源码等）
- 成功后返回 `"成功写入 '<file_path>'"`；异常 → `"写入文件失败: ..."`

### 4. CodeExecTool — 终端命令执行

**文件**：`app/integration/tools/builtin/code_exec.py`｜**名称**：`code_exec`

| 项目 | 说明 |
| --- | --- |
| 描述 | 在系统终端中执行代码/命令并返回输出，适用于运行脚本、Shell 命令、编译等 |
| 参数 | `command: string`（必填）、`workdir: string`（可选，绝对路径，留空用项目根目录） |
| 执行方式 | `asyncio.create_subprocess_shell` 异步子进程 |
| 风险级 | L2 危险（category=code，**非并发安全** → 同工具串行化） |
| 默认超时 | 60s（编译 / 运行可较久） |
| 安全限制 | **危险命令黑名单**、空命令拦截（结果截断由 ResultProcessor 统一处理） |

**危险命令黑名单（`FORBIDDEN_PREFIXES`，17 项）：**

```text
rm -rf /, rm -rf /*, rm -rf ~, rm -rf .
mkfs., dd if=, :(){ :|:& };:
> /dev/sda, | shutdown, | reboot
shutdown, reboot, halt, poweroff, init 0, init 6
mv /
```

**实现要点：**

```python
# 安全检查：命令统一 lower 后与黑名单前缀（亦已 lower）做 startswith 匹配
command_lower = command.lower().strip()
for prefix in self.FORBIDDEN_PREFIXES:
    if command_lower.startswith(prefix.lower()):
        return ToolResult(success=False, content="", error=f"命令被安全策略拦截（高危操作）: {prefix}")

proc = await asyncio.create_subprocess_shell(
    command,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    cwd=workdir,
)
stdout, stderr = await proc.communicate()
```

- **黑名单匹配方式**：`startswith` 前缀匹配（大小写不敏感），例如 `rm -rf /` 会拦截 `RM -RF /xxx`；但**不拦截** `... && rm -rf /` 这类非前缀位置的危险子串，属已知局限
- 空命令（`command` 去除空白后为空）直接返回失败
- **超时 / 取消清理**：stdout/stderr 读取经 `asyncio.wait_for` 以工具自声明超时（60s）兜底；超时或 executor 外层取消（`CancelledError`）时先 `proc.kill()` 再 `await proc.wait()` 回收后重抛（由 executor 归为 `TIMEOUT`），避免孤儿进程泄漏
- **流式读取限制内存**：`communicate()` 全量读已废弃，改为 `_read_stream_capped` 流式读 stdout/stderr（保留前 `max_output_length×3` 字节，超出丢弃并继续 drain 防管道阻塞）
- **输出解码**：`_decode_output` 双解码——优先 UTF-8（现代工具 / Python 脚本），非法字节回退系统 locale 编码（Windows 中文环境 cp936，匹配 cmd 系统命令输出）；结果截断由 ResultProcessor 统一处理
- 返回内容：有输出则拼接 stdout（及 `--- stderr ---` 段），否则 `"(无输出)"`
- `success = proc.returncode == 0`；`metadata` 记录 `return_code` 与 `command`
- 异常分类：`FileNotFoundError` → `"命令不存在或未找到可执行文件"`；其它 → `"命令执行失败: ..."`

### 5. WebBrowseTool — 网页内容抓取

**文件**：`app/integration/tools/builtin/web_browse.py`｜**名称**：`web_browse`

| 项目 | 说明 |
| --- | --- |
| 描述 | 获取指定 URL 网页内容并返回纯文本版本，用于阅读文章、查看文档、获取在线信息 |
| 参数 | `url: string`（必填，完整 URL；缺协议时自动补 `https://`） |
| 执行方式 | `httpx.AsyncClient`（**全局单例**，复用连接池） |
| HTML 解析 | 自实现 `_HTMLToTextParser`，基于标准库 `html.parser`，零额外依赖 |
| 风险级 | L0 只读（category=web，并发安全） |
| 默认超时 | 15s（与内部 httpx 超时一致） |
| 安全限制 | 超时 15s、最大重定向 5、**SSRF 防护**（裸 IP / 内网 TLD / 内网站段拒绝，含重定向跳；结果截断由 ResultProcessor 统一处理，`max_output_length` 默认 50_000） |

**HTTP 客户端（全局单例，连接池复用）：**

```python
_http_client = httpx.AsyncClient(
    follow_redirects=True,
    max_redirects=5,
    timeout=15.0,
    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ... Chrome/120.0.0.0 Safari/537.36"},
    event_hooks={"request": [_ssrf_on_request]},
)
```

- 单例由 `_get_http_client()` 维护，避免每次执行新建连接
- 连接池随应用关闭由 `on_unload()` 回收（`ToolService.shutdown` 调用，见 [tool_service.md](../tool_service.md)）
- `url` 不以 `http://` / `https://` 开头时自动补全 `https://`
- **SSRF 防护**：client 注入 `event_hooks["request"]` 校验，每个请求（含重定向跳）拒绝：裸 IP（保守策略含公网）、内网保留域名（`.internal` / `.local` / `.corp` 等）、解析后命中内网·环回·链路本地·保留网段的域名（防 DNS rebinding）；DNS 解析经 `asyncio.to_thread` 不阻塞事件循环
- **流式读取限制内存**：`client.stream` + `aiter_bytes` 流式读响应体，HTML 解析器增量 feed，累计超 `max_content_length×4` 字节即停；body 过大时 content 追加提示、metadata 置 `truncated`

**`_HTMLToTextParser`（`HTMLParser` 子类）特性：**

- **跳过 `<script>` / `<style>` 内容**（`_skip_tag` 标志），只保留正文
- 提取 `<title>` 页面标题；`pre` 标签保留原始空白（不 strip）
- 块级元素（`BLOCK_TAGS`：p, div, br, h1-h6, li, tr, td, th, blockquote, pre）自动换行
- 收集 `<a>` 链接：过滤 `#` 与 `javascript:` 开头 href；链接显示文本截断 80 字符；`get_links_formatted(max_links=20)` 用 `urljoin(base_url, url)` 转绝对地址并**去重**，按 Markdown `[text](url)` 格式输出
- HTML 实体解码用 `html.unescape()`（Python 3.9+ 移除了 `HTMLParser.unescape`，注释中已注明）

**返回内容结构（多行拼接）：**

```text
标题: {title}
来源: {final_url}

{纯文本正文（截断由 ResultProcessor 统一处理）}

页面链接：
  - [text](url)
  ...
```

- HTTP 状态码 >= 400 → 失败，`metadata` 记录 `url` 与 `status_code`
- 成功时 `metadata` 记录 `url`、`title`、`status_code`、`content_type`
- 异常分类：`httpx.TimeoutException` → `"请求超时（15 秒）"`；`httpx.TooManyRedirects` → `"重定向次数过多"`；`httpx.RequestError` → `"请求失败"`；其它 → `"页面解析失败"`

---

## 良率 RCA 工具（RCA 场景）

`builtin/rca/` 子包实现 5 个**产品场景工具**（产品主链路「良率异常 → 并行排查 → 证据链根因报告」的直接支撑）：

`query_batch_yield`（批次良率）/ `query_equipment_alerts`（设备告警）/ `query_fdc_params`（FDC 参数偏离）/ `query_defect_map`（缺陷模式）/ `search_historical_rca`（历史案例检索）。

- 全部 **L0 只读**，返回内容带**证据链 metadata**（`source` / 查询键 / `timestamp`）
- 数据为**固定模拟数据**（LOT-A123 根因故事 + 对照组，可复现）；`search_historical_rca` 当前为关键词匹配，RAG（embedding 召回）列为后续增强
- 契约 / 排查链示例 / 模拟数据详见 [RCA 工具说明](rca.md)

---

## 外部工具（热加载）

`app/integration/tools/external/` 为**外部工具**目录（含随附示例 `http_api`），由 `ExternalToolLoader` 动态发现注册。加载 / 重载 / 卸载 / 生命周期钩子 / 编写约定见 [外部工具热加载](../external.md)——**状态只在此维护，本文不重复**。

---

## 开发新工具要点

`builtin` 的自动发现机制让新增工具只需两步：

1. **在 `builtin/` 下新建文件**，继承 `BaseTool` 并实现 4 个抽象成员（`name` / `description` / `parameters` / `execute`）
2. **无需任何注册代码**，重启后 `from app.integration.tools.builtin import YourTool` 即可使用

**开发规范（详见 [工具模块接口文档](../tools.md)）：**

1. `name` 使用小写+下划线且全局唯一（现有 10 个已占用：`search` / `readFile` / `writeFile` / `code_exec` / `web_browse` + `query_batch_yield` / `query_equipment_alerts` / `query_fdc_params` / `query_defect_map` / `search_historical_rca`）
2. `description` 清晰描述功能与适用场景，LLM 据此决定调用
3. `parameters` 使用 OpenAI Function Calling 的 JSON Schema 格式
4. `execute` 必须为 `async def` 并返回 `ToolResult`
5. 开头总是调用 `self.validate_parameters(**kwargs)` 做参数校验（executor 已做，此为兜底）
6. **按需覆写元数据**：`risk_level`（默认 L0）、`category`、`concurrency_safe`（写 / 子进程类设为 `False`）、`max_output_length`（结果截断上限）、`timeout`（工具自声明默认超时，None 沿用全局 30s）、`requires_approval`（需人工审批时设为 `True`）
7. 所有异常捕获为 `ToolResult(success=False, error=...)`，不让异常抛出
8. 同步 IO（如第三方 SDK）用 `asyncio.to_thread` 包装，避免阻塞事件循环

---

## 相关文档

- [工具模块总览](../tools.md)（ToolService / ToolExecutor 并发控制、重试、统计、配置关联）
- [RCA 工具说明](rca.md)（良率根因分析场景工具，模拟数据源）
- [service 模块](../../../application_doc/README.md)（ToolService 所在的服务层）
- [架构设计](../../../architecture.md)（工具层在整体架构中的定位）
- [配置管理](../../../config_doc/config.md)（`TAVILY_API_KEY`、`TOOL_MAX_OUTPUT_LENGTH` 等配置项）
- [核心层](../../../domain_doc/README.md)（Agent 推理循环如何消费工具）
- [API 文档](../../../api_doc/api.md)
- [TOOLS-001 问题记录](../../../../issues/integration/tools/2026-08-18-subprocess-orphan-on-cancel.md)（子进程超时 / 取消清理）
- [TOOLS-002 问题记录](../../../../issues/integration/tools/2026-08-19-file-tools-allowed-dirs.md)（文件工具允许目录白名单）
- [TOOLS-003 问题记录](../../../../issues/integration/tools/2026-08-19-web-browse-ssrf.md)（web_browse SSRF 防护）
- [TOOLS-004 问题记录](../../../../issues/integration/tools/2026-08-19-memory-capped-reads.md)（工具读取内存峰值限制）
- [TOOLS-005 问题记录](../../../../issues/integration/tools/2026-08-19-code-exec-gbk-decode.md)（code_exec 输出 GBK 解码）
- [TOOLS-009 问题记录](../../../../issues/integration/tools/2026-08-19-search-source-urls.md)（search 结果来源 URL）
