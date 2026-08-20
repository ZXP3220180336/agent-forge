# 工具模块接口文档

> **更新日期**：2026-08-17
> **模块**：`app/integration/tools/`
> **文档定位**：工具系统对外接口契约 + 六大子组件导航。执行细节（并发 / 重试 / 截断 / 审计）见对应子文档，本文不重复。
> **状态**：✅ 已实现
> **工业级对照**：对齐工业界六大子组件（注册中心 / 选择器 / 校验器 / 调度器 / 结果处理 / 安全审计），详见 [ADR](../../../adr/integration/tools/2026-08-17-six-component-alignment.md)

---

## 📋 目录

- [模块概述](#模块概述)
- [对外接口](#对外接口)
- [内部实现组织（六大子组件）](#内部实现组织六大子组件)
- [内置工具](#内置工具)
- [配置关联](#配置关联)
- [相关文档](#相关文档)

---

## 模块概述

工具系统是 Agent 从「语言推理」落到「真实执行」的枢纽：为 LLM 提供可调用的能力集合，负责工具注册、Schema 导出、参数校验、执行调度、结果处理与安全审计。领域层只依赖 `ToolGateway` 端口（依赖倒置），装配根 `container.py` 注入单例 `ToolService`。

### 模块结构

```text
app/integration/tools/
├── tool_service.py        ← ToolService（Facade，唯一对外入口，实现 ToolGateway）
├── registry.py            ← ToolRegistry 注册中心（容器 + Schema 导出 + 元数据查询）
├── selector.py            ← ToolSelector 选择器（协议 + DefaultToolSelector 全量注入）
├── validator.py           ← ParameterValidator 参数校验器（jsonschema 严格校验）
├── executor.py            ← ToolExecutor 执行调度器（信号量 / 重试 / 超时 / 校验 / 截断 / 审计 / 审批拦截）
├── result_processor.py    ← ResultProcessor 结果处理器（head+tail 截断 + 错误归一化）
├── security.py            ← RiskLevel / ToolAuditor / ApprovalGate 安全审计（分级 + 审计 + 审批通道）
├── stats.py               ← ToolStats / ToolStatsCollector 执行统计
├── hooks.py               ← ExecutionHooks 执行钩子
├── assembler.py           ← ToolAssembler 内置工具装配
├── loader.py              ← ExternalToolLoader 外部工具热加载（惰性检查 + 生命周期钩子）
├── base.py                ← BaseTool 抽象基类（元数据 + 校验委托 + 生命周期钩子）
├── builtin/               ← 内置工具（自动发现）
└── external/              ← 外部工具（热加载，见 external.md）
```

### 设计原则

1. **Facade 模式**：`ToolService` 是唯一对外入口，内部六大子组件不对外暴露
2. **依赖倒置**：领域层只依赖 `ToolGateway` 端口；`ToolService` 结构实现之（非显式继承）
3. **零 settings 依赖**：tools 模块不直接 import settings，配置经 `container.py` 的 `register_config` / 构造参数注入
4. **工具级并发信号量**：限制单任务内最大并发工具数（`agent_max_concurrent_tools`），保护 GPU / 服务器资源
5. **外部工具惰性检查**：`execute` 入口对比 external 目录签名，变化才重扫（无后台任务，对齐「变更 → 下次调用生效」）

## 对外接口

### `ToolGateway` 协议（领域端口，`app/domain/ports/tool_gateway.py`）

领域层消费的唯一契约，`ToolService` 结构满足：

```python
@runtime_checkable
class ToolGateway(Protocol):
    def get_openai_tools(self) -> list[dict[str, Any]]: ...
    async def execute(self, name, parameters, timeout=None, max_retries=None, retry_delay=1.0) -> ToolResult: ...
```

### `ToolResult`（领域契约）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `success` | `bool` | 是否成功 |
| `content` | `str` | 结果内容（LLM 观察） |
| `error` | `str \| None` | 失败错误信息（中文归因，供 LLM 下一轮修正） |
| `error_code` | `ErrorCode \| None` | 系统级失败分类（业务错误为 None，见下） |
| `metadata` | `dict \| None` | 元数据（截断标记 `truncated` 等） |
| `execution_time` | `float \| None` | 执行耗时（executor 填充） |
| `retry_count` | `int` | 实际尝试次数（executor 填充） |

`ErrorCode`（[`app/domain/ports/tool_gateway.py`](../../../app/domain/ports/tool_gateway.py)）系统级 6 码：`NOT_REGISTERED`（未注册）/ `JSON_PARSE`（参数 JSON 解析失败）/ `VALIDATION`（校验失败）/ `REJECTED`（审批拒绝）/ `TIMEOUT`（执行超时）/ `UNKNOWN`（未捕获异常）。工具业务错误为 `None`（`error` 字符串承载 LLM 归因）——**错误码 + 中文归因并存**：错误码供审计聚合与证据链可审计性，`error` 供 LLM 修正。

### `ToolService` 方法

ToolService 全部方法签名 / 说明见 [ToolService 说明](tool_service.md#toolservice-方法)；本处只列领域契约 `ToolGateway`（见上）。

### `BaseTool` 抽象契约

| 方法 / 属性 | 说明 |
| --- | --- |
| `name` / `description` / `parameters` | 抽象；`parameters` 为 OpenAI Function Calling JSON Schema |
| `execute(**kwargs) -> ToolResult` | 抽象；业务错误走返回值而非抛异常 |
| `to_openai_tool` / `to_openai_response` | Schema 导出（Chat / Responses 格式） |
| `validate_parameters(**kwargs) -> bool` | 委托 jsonschema 校验器（完整校验） |
| `validation_issues(**kwargs) -> list[str]` | 中文归因问题列表（executor 错误信息用） |
| `risk_level` | 风险分级（默认 L0，见 [security.md](security.md)） |
| `category` | 功能域（默认 "general"，供按域查询） |
| `concurrency_safe` | 是否允许自身并发（写 / 子进程类应为 False → 串行化） |
| `requires_approval` | 是否需人工审批（executor 经 ApprovalGate 确认，默认放行） |
| `max_output_length` | 结果截断上限（ResultProcessor 消费，默认 100_000） |
| `timeout` | 工具自声明默认超时（秒；None = 沿用全局配置，调用方显式传入可覆盖） |
| `on_load` / `on_unload` | 可选异步钩子：加载后初始化 / 卸载前释放资源（外部工具加载器消费，默认 no-op） |
| `health_check` | 可选异步钩子：健康检查，返回可用性（默认 True，预留巡检） |
| `register_config` | 可选类方法：装配根注入运行配置（避免直接依赖 settings） |

## 内部实现组织（六大子组件）

| 子组件 | 文件 | 职责 | 文档 |
| --- | --- | --- | --- |
| 工具注册中心 | registry.py | 容器 + Schema 导出 + 按风险/分类查询 | [registry.md](registry.md) |
| 工具选择器 | selector.py | 选注入子集（默认全量，预留召回） | [selector.md](selector.md) |
| 参数校验器 | validator.py | jsonschema 严格校验 + 错误归因 | [validator.md](validator.md) |
| 执行调度器 | executor.py | 信号量 / 重试 / 超时 / 校验 / 截断 / 审计 / 审批拦截编排 | [executor.md](executor.md) |
| 结果处理器 | result_processor.py | head+tail 截断 + 错误归一化 | [result_processor.md](result_processor.md) |
| 安全审计 | security.py | 风险分级 + 审计留痕 + 审批通道 | [security.md](security.md) |

辅助组件：`stats.py`（执行统计，见 [stats.md](stats.md)）、`hooks.py`（执行钩子）、`assembler.py`（内置工具装配）、`loader.py`（外部工具热加载，见 [external.md](external.md)），详见 [ToolService 说明](tool_service.md)。人工审批通道（`ApprovalGate` / `AutoApprovalGate`）随安全审计子组件见 [security.md](security.md)。

## 内置工具

`builtin/` 自动发现 `BaseTool` 子类（无需注册代码）。10 个内置工具及其注册名：

| 工具 | 注册名 | 职责 |
| --- | --- | --- |
| SearchTool | `search` | 网络搜索（Tavily） |
| ReadFileTool | `readFile` | 读取文件 |
| WriteFileTool | `writeFile` | 写入文件 |
| CodeExecTool | `code_exec` | 终端命令执行 |
| WebBrowseTool | `web_browse` | 网页内容抓取 |
| QueryBatchYieldTool | `query_batch_yield` | 批次良率查询 |
| QueryEquipmentAlertsTool | `query_equipment_alerts` | 设备告警 / PM 查询 |
| QueryFdcParamsTool | `query_fdc_params` | FDC 参数偏离查询 |
| QueryDefectMapTool | `query_defect_map` | 缺陷模式查询 |
| SearchHistoricalRcaTool | `search_historical_rca` | 历史案例检索 |

各工具实现细节（风险分级 / 分类 / 并发安全 / 默认超时 / 参数 Schema）见 [内置工具说明](builtin_doc/builtin.md)；RCA 场景工具契约见 [RCA 工具说明](builtin_doc/rca.md)。工具实例化不依赖外部服务（API Key 执行时才需要），个别注册失败不影响启动。

## 外部工具（热加载）

`external/` 目录下的 `BaseTool` 子类由 `ExternalToolLoader` 动态发现并注册（对齐工业热插拔「内嵌式可信插件」档）。惰性检查 / 生命周期钩子 / 配置注入 / 信任边界 / 编写约定见 [外部工具热加载](external.md)——状态只在此维护，本文不重复。

## 配置关联

配置经 `container.py` 注入（tools 模块零 settings 依赖）。配置项 / 默认值 / 使用场景见 [config 文档](../../config_doc/config.md)（工具配置 / Agent 并发控制 / Tavily 配置节），本处不重复。

## 相关文档

- [ToolService 说明](tool_service.md)（Facade 装配 / 执行流程 / 并发语义）
- [内置工具说明](builtin_doc/builtin.md)（BaseTool + 10 内置工具）· [外部工具热加载](external.md)（ExternalToolLoader）
- [validator.md](validator.md) · [result_processor.md](result_processor.md) · [security.md](security.md) · [selector.md](selector.md)
- [集成层总览](../README.md) · [架构设计](../../architecture.md)
- 决策记录：[ADR 索引](../../../adr/integration/tools/README.md)
