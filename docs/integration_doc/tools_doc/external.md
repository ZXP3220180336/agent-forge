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
2. **对齐工业标准「变更 → 下次调用生效」**：无后台任务，避免后台轮询的生命周期管理成本
3. **生命周期完整**：加载 `on_load()` / 卸载 `on_unload()` / 健康 `health_check()`，资源可完整回收
4. **全链路留痕**：加载 / 重载 / 卸载 / 冲突拒绝 / 失败均结构化日志，配合调用审计覆盖全链路
5. **内置工具权威**：与已注册工具重名 → 拒绝加载（防误覆盖 builtin）

## 核心概念解释

### execute 惰性检查

`ToolService.execute` 入口先调 `maybe_refresh()`：对比**目录签名**（文件集 + 各文件 mtime/size）与上次扫描值，**无变化则零开销返回**，变化才重扫。因此：

- 外部工具文件新增 / 修改 / 删除后，**下一次任意工具调用即生效**（无需后台任务）
- 目录工具文件少（<几十个），每次 execute 的 glob+stat 为微秒-百微秒级
- 并发 execute 经 `asyncio.Lock` + 签名二次检查防重复扫描

### 加载 / 重载 / 卸载

- **加载**：`importlib.util.spec_from_file_location` 动态导入 → 收集 `BaseTool` 子类（过滤规则与 builtin 一致）→ 实例化 → `on_load()` → 冲突检查 → `service.register`
- **重载**（mtime 或 size 变化）：nuke-and-repave —— 先卸载旧实例再加载新实例；在飞 execute 持 per-tool 锁时 `prune_tool_lock` 跳过（executor 保证串行化不破坏）
- **卸载**（文件删除）：`on_unload()` → `service.unregister` → 清理 `sys.modules`
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

class MyTool(BaseTool):
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

1. **命名唯一**：`name` 不得与 builtin（`search` / `readFile` / `writeFile` / `code_exec` / `web_browse`）或已加载工具冲突
2. **配置自取**：外部工具**自行读环境变量**等配置（容器不知道其存在，不注入 `register_config`）
3. **单文件自包含**：建议一个文件一个工具，共享逻辑放 `_` 前缀文件（不参与扫描，需重启生效——跨文件 `from . import helper` 存在传递性陈旧，只改 helper 不会重载依赖方）
4. **信任边界**：`external/` 与应用**同信任级别**——加载即执行任意 Python 代码，只放受信任工具
5. **无状态优先**：状态由 Agent 核心管理；持连接 / 子进程 / 定时器的工具必须实现 `on_unload()` 完整回收

> **完整示例**：`app/integration/tools/external/http_api.py`（`http_api`）是随附的热插拔示例——REST API 调用工具，演示了元数据覆写（L1 写 / category=http / timeout=15）+ 生命周期钩子（`on_load` 建立 httpx 连接池 / `on_unload` 释放）+ 参数 schema（method 枚举 + url 必填）+ 异常分类归因，可作新外部工具模板。

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

`tests/unit/test_tool_loader.py`（15 用例）：加载（首扫 / 新增）/ 重载（mtime 变化）/ 卸载 / 冲突拒绝 / 语法错误 / 目录缺失 / 文件级回滚 / on_load / on_load 失败 / on_unload / health_check / maybe_refresh 惰性 / 排除规则 / 非法文件名。executor 侧 `test_prune_tool_lock_skips_held`（重载锁竞态）。

## 相关文档

- [工具模块接口文档](tools.md)（BaseTool / ToolService / external 定位）
- [ToolService 说明](tool_service.md)（execute 惰性检查入口 / refresh_external_tools）
- [生命周期钩子](builtin_doc/builtin.md)（BaseTool 基类详解）
- 决策记录：[ADR TOOLS-ADR-005](../../../adr/integration/tools/README.md)
