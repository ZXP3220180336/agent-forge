# 外部工具热加载（ExternalToolLoader）说明文档

> **更新日期**：2026-08-17
> **模块**：`app/integration/tools/loader.py`
> **职责**：从外部目录发现 `BaseTool` 子类并纳入注册中心 —— 加载 / 重载 / 卸载 / 生命周期钩子 / 全链路留痕
> **状态**：✅ 已实现
> **工业级对照**：对齐工业热插拔「内嵌式可信插件」档（契约优先 / 生命周期钩子 / 全链路留痕 / 下次调用生效），详见 [ADR](../../../adr/integration/tools/2026-08-17-external-tool-hot-reload.md)

---

## 📋 目录

- [设计目标](#设计目标)
- [核心概念解释](#核心概念解释)
  - [execute 惰性检查](#execute-惰性检查)
  - [加载 / 重载 / 卸载](#加载--重载--卸载)
  - [生命周期钩子](#生命周期钩子)
  - [全链路留痕](#全链路留痕)
- [对外接口](#对外接口)
- [外部工具编写约定](#外部工具编写约定)
- [边界情况](#边界情况)
- [升级路径](#升级路径)
- [测试状态](#测试状态)
- [相关文档](#相关文档)

---

## 设计目标

1. **零重启动态加载**：外部工具文件放入目录 → 下一次工具调用（`execute` 入口惰性检查）即注册生效，无需重启进程
2. **对齐工业标准「变更 → 下次调用（最迟 TTL 1s）生效」**：无后台任务、无后台轮询；TTL 限制磁盘 stat 频率，热路径零 IO
3. **生命周期完整**：加载 `on_load()` / 卸载 `on_unload()` / 健康 `health_check()`，资源可完整回收
4. **全链路留痕**：加载 / 重载 / 卸载 / 冲突拒绝 / 失败均结构化日志，配合调用审计覆盖全链路
5. **内置工具权威**：与已注册工具重名 → 拒绝加载（防误覆盖 builtin）

## 核心概念解释

### execute 惰性检查

`ToolService.execute` 入口先调 `maybe_refresh()`：**TTL（`_DIR_SIGNATURE_TTL` = 1s）内零磁盘 IO**（复用上次签名结果），到期后对比**目录签名**（文件集 + 各文件 mtime/size，经 `asyncio.to_thread` 执行不阻塞事件循环）与上次扫描值，**无变化则零开销返回**，变化才重扫。因此：

- 外部工具文件新增 / 修改 / 删除后，**下一次工具调用（最迟 1s TTL 后）生效**（无需后台任务）
- **热路径 IO 上限**：磁盘 stat 频率 ≤ 1 次/秒（TTL 缓存），不随 execute 调用次数线性增长；stat 放线程池不阻塞事件循环
- 并发 execute 经 `asyncio.Lock` + 签名二次检查防重复扫描

### 加载 / 重载 / 卸载

- **加载**：`importlib.util.spec_from_file_location` 动态导入（快照 sys.modules 追踪工具模块 + 兄弟模块）→ 收集 `BaseTool` 子类（过滤规则与 builtin 一致）→ **配置注入（`CONFIG_KEYS` → `register_config`）** → 实例化 → `on_load()` → 冲突检查 → `service.register`
- **重载**（mtime 或 size 变化）：nuke-and-repave —— 先卸载旧实例再加载新实例；在飞 execute 持 per-tool 锁时 `prune_tool_lock` 跳过（executor 保证串行化不破坏）
- **卸载**（文件删除）：`on_unload()` → `service.unregister` → 清理 sys.modules（工具模块 + 兄弟模块，防旧缓存）
- **模块名**：合法标识符文件名用真实包名（`app.integration.tools.external.<stem>`，保证文件内相对导入与 loader 恒等）；非法标识符（`my-tool.py`）回退 sha1 哈希模块名
- **文件级原子性**：单文件多工具，任一实例化 / `on_load` / 注册失败 → 回滚本文件已注册实例，不留半加载态

### 生命周期钩子

`BaseTool` 提供三个非抽象钩子（默认 no-op，子类按需覆写）：

| 钩子 | 调用时机 | 用途 |
| --- | --- | --- |
| `async on_load()` | 实例化后、注册前 | 建立连接 / 加载配置；失败 → 该工具跳过并回滚 |
| `async on_unload()` | 注销前 | 释放连接 / 子进程 / 定时器；异常不影响卸载流程 |
| `async health_check() -> bool` | 预留，当前不自动调用 | 健康检查，供未来巡检隔离 |

### 全链路留痕

loader 每次操作记录结构化日志（`app.tools.external`）：加载成功 / 重载 / 卸载 / 冲突拒绝 / 导入失败 / 回滚。配合 executor 的 `tool_call` 调用审计（[security.md](security.md)），覆盖「加载、卸载、更新、调用、异常」全链路。

## 对外接口

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `maybe_refresh` | `async () -> None` | execute 入口调用：目录签名变化才重扫（签名不变零开销返回） |
| `scan_once` | `async () -> None` | 手动重扫：应用磁盘 diff（新增 / 修改 / 删除），幂等 |

`ToolService` 封装（对外入口）：`execute` 内部自动惰性检查；`refresh_external_tools()` 手动触发重扫。

## 外部工具编写约定

在 `app/integration/tools/external/`（或 `ToolService` 构造注入的目录）放置 `.py` 文件，每个文件可定义多个 `BaseTool` 子类：

```python
from app.integration.tools.base import BaseTool
from app.domain.ports.tool_gateway import ToolResult

CONFIG_KEYS = ("tool_http_timeout",)  # 可选：声明需要的 settings 配置键（loader 经 config_source 注入）

class MyTool(BaseTool):
    _timeout = 15.0  # 默认值（settings 未配置时兜底）

    @classmethod
    def register_config(cls, *, tool_http_timeout=None, **kwargs) -> None:
        """可选：接收 loader 注入的配置（对齐内置工具 register_config 风格）。"""
        if tool_http_timeout is not None:
            cls._timeout = tool_http_timeout

    @property
    def name(self) -> str: return "my_tool"          # 全局唯一，不与内置冲突
    @property
    def description(self) -> str: return "..."        # LLM 据此决定调用
    @property
    def parameters(self) -> dict: return {"type": "object", "properties": {...}}
    async def execute(self, **kwargs) -> ToolResult: ...   # 必须 async，返回 ToolResult

    # 可选：风险分级 / 分类 / 并发安全 / 超时 / 审批等元数据覆写
    # 可选：生命周期钩子 on_load / on_unload / health_check
```

**约定要点**：

1. **命名唯一**：`name` 不得与 builtin（10 个：`search` / `readFile` / `writeFile` / `code_exec` / `web_browse` / `query_batch_yield` / `query_equipment_alerts` / `query_fdc_params` / `query_defect_map` / `search_historical_rca`）或已加载工具冲突
2. **配置注入**：模块级声明 `CONFIG_KEYS`（settings 键元组）+ 类实现 `register_config`——loader 加载时从装配根绑定的 `config_source`（settings 读取器）取值注入，路径与内置工具一致；未声明 `CONFIG_KEYS` 或不实现 `register_config` 则跳过注入
3. **单文件自包含**：建议一个文件一个工具，共享逻辑放 `_` 前缀文件（不参与扫描）；跨文件 `from . import helper` 的兄弟模块随工具文件加载 / 卸载一起清理（`_load_file` 快照 sys.modules 追踪，`_unload_file` 一并 pop），改 helper 后重载工具文件即生效
4. **信任边界**：`external/` 与应用**同信任级别**——加载即执行任意 Python 代码，只放受信任工具
5. **无状态优先**：状态由 Agent 核心管理；持连接 / 子进程 / 定时器的工具必须实现 `on_unload()` 完整回收
6. **生命周期钩子禁反向调用**：`on_load` / `on_unload` 内禁止调用 `execute`（加载流程持 `_scan_lock`，反向调用会死锁，见 [TOOLS-014 问题记录](../../../issues/integration/tools/2026-08-19-loader-scan-lock-deadlock.md)）

> **完整示例**：`app/integration/tools/external/http_api.py`（`http_api`）是随附的热插拔示例——REST API 调用工具，演示了元数据覆写（L1 写 / category=http / timeout=15 / **`requires_approval=True` 写操作需审批**）+ 生命周期钩子（`on_load` 建立 httpx 连接池 / `on_unload` 释放）+ 参数 schema（method 枚举 + url 必填）+ 异常分类归因 + **配置注入**（`CONFIG_KEYS=("tool_http_timeout",)` → `register_config` 注入超时）+ **SSRF 防护**（复用 [security.md](security.md) `ssrf_on_request`，裸 IP / 内网目标拒绝），可作新外部工具模板。

## 边界情况

1. **语法 / 导入错误文件** → 跳过 + warning，其余文件照常加载
2. **目录不存在 / 无 .py** → 签名空，无操作
3. **文件级部分失败** → 回滚本文件全部已注册实例（文件级原子性）
4. **`__init__.py` 与 `_` 开头文件** → 不参与扫描
5. **重载失败降级**：文件改坏 → 工具暂不可用（旧实例已卸载）→ 修复文件即恢复
6. **在飞 execute 与重载**：旧实例引用跑完；持锁时 `prune_tool_lock` 跳过，串行化不破坏
7. **execute 热路径**：每次 execute 一次目录签名检查（glob+stat），无变化零重扫

## 升级路径

工业级热插拔完整体系的分层对齐（本模块实现「内嵌式可信插件」档），后续能力触发条件与落地形态见 [ADR](../../../adr/integration/tools/2026-08-17-external-tool-hot-reload.md)：

- 原子无损（引用计数 + 版本化实例）→ 出现「重载窗口不可接受」时
- 元数据 / 实现分离 + 懒加载 → 工具 > 数百、启动变慢时
- 沙箱隔离（子进程 / WASM / Sidecar）→ 引入不可信第三方工具时
- `health_check` 自动巡检 + 异常隔离 → 插件数量上升时

## 测试状态

`tests/unit/test_tool_loader.py`（22 用例）：加载（首扫 / 新增）/ 重载（mtime 变化）/ 卸载 / 冲突拒绝 / 语法错误 / 目录缺失 / 文件级回滚 / on_load / on_load 失败 / on_unload / health_check / maybe_refresh 惰性 / maybe_refresh TTL 短路 / maybe_refresh TTL 过期重检 / 排除规则 / 非法文件名 / 配置注入（CONFIG_KEYS → register_config，无 config_source 跳过）/ 兄弟模块清理（_drop_modules + 卸载清理）。executor 侧 `test_prune_tool_lock_skips_held`（重载锁竞态）。

## 相关文档

- [工具模块接口文档](tools.md)（BaseTool / ToolService / external 定位）
- [TOOLS-010 问题记录](../../../issues/integration/tools/2026-08-19-external-tool-config-injection.md)（外部工具配置注入通道）
- [TOOLS-012 问题记录](../../../issues/integration/tools/2026-08-19-external-maybe-refresh-io.md)（maybe_refresh TTL 限频）
- [TOOLS-013 问题记录](../../../issues/integration/tools/2026-08-19-external-sibling-module-cache.md)（兄弟模块缓存清理）
- [TOOLS-014 问题记录](../../../issues/integration/tools/2026-08-19-loader-scan-lock-deadlock.md)（生命周期钩子禁反向调用）
- [TOOLS-037 问题记录](../../../issues/integration/tools/2026-08-20-http-api-error-redaction.md)（http_api 错误脱敏）
- [ToolService 说明](tool_service.md)（execute 惰性检查入口 / refresh_external_tools）
- [生命周期钩子](builtin_doc/builtin.md)（BaseTool 基类详解）
- 决策记录：[ADR TOOLS-ADR-005](../../../adr/integration/tools/README.md)
