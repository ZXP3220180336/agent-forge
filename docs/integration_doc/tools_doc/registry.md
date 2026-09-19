# 工具注册中心（ToolRegistry）说明文档

> **更新日期**：2026-09-19
> **模块**：`app/integration/tools/registry.py`
> **职责**：工具容器 —— 注册 / 注销 / 查询 / 列表 / 元数据过滤
> 状态与验证见 [ALIGNMENT](../../ALIGNMENT.md)。
> **工业级对照**：工业界「工具注册中心」管理工具元数据并支持动态注册 / 下线（Hermes AST 发现、阿里 MCP 动态注册）；本组件为纯容器，Schema 导出 / 选择 / 校验 / 执行不在此

---

## 📋 目录

- [定位与职责](#定位与职责)
- [接口契约](#接口契约)
- [行为边界](#行为边界)
- [使用示例](#使用示例)
- [设计决策](#设计决策)
- [测试](#测试)
- [相关文档](#相关文档)

---

## 定位与职责

ToolRegistry 是工具模块的**容器层**：持有全部已注册 `BaseTool` 实例，提供注册 / 注销 / 查询 / 列表，并按**风险等级** / **功能域**过滤（供安全审计与预留管理界面）。Schema 导出不在此——模型可见性只由 Facade [ToolService](tool_service.md) 决定，容器若也导出会绕过那里的启用过滤（[TOOLS-060](../../../issues/integration/tools/2026-09-19-registry-export-dead-code.md)）。

不承担执行职责——参数校验 / 重试 / 统计 / 并发控制由 [executor.md](executor.md) 负责（本组件是纯容器，无副作用）。注册入口唯一做一次**定义预检**（[本地 Schema 契约](../../shared_doc/json_schema.md)）：非法定义在此被拒绝，使内置装配的逐工具兜底与外部加载的文件级回滚各自生效，不会推迟到导出或执行时让同批工具一起失效。

## 接口契约

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `register` | `(tool: BaseTool) -> None` | 注册工具；重名抛 `ValueError("工具 '...' 已存在")`，参数 Schema 定义非法抛 `jsonschema.SchemaError`（[本地 Schema 契约](../../shared_doc/json_schema.md)） |
| `unregister` | `(name: str) -> bool` | 注销工具，返回是否成功 |
| `get` | `(name: str) -> BaseTool \| None` | 获取工具实例 |
| `list_tools` | `() -> list[str]` | 列出全部工具名 |
| `all_tools` | `() -> list[BaseTool]` | 全部工具实例（注册顺序） |
| `list_by_risk` | `(risk_level: RiskLevel) -> list[BaseTool]` | 按风险等级过滤 |
| `list_by_category` | `(category: str) -> list[BaseTool]` | 按功能域过滤 |

内部存储：`dict[str, BaseTool]`（key = 实例 `tool.name`），保持注册顺序（Python dict 有序）。

## 行为边界

| 场景 | 行为 |
| --- | --- |
| 重复注册同名工具 | 抛 `ValueError`（不覆盖） |
| 注销不存在的工具 | 返回 `False`，不抛异常 |
| 查询不存在工具 | 返回 `None` |
| 空容器查询 | 返回空列表 |
| `list_by_risk` / `list_by_category` | 无匹配返回空列表 |

## 使用示例

```python
service = ToolService()
service.register(SearchTool())          # ToolService.register → registry.register + stats.init
dangerous = service.list_by_risk(RiskLevel.L2_DANGEROUS)   # → [code_exec]
web_tools = service.list_by_category("web")                # → [web_browse]
```

Schema 导出不经此处：模型可见的工具列表由 `ToolService.get_openai_tools()` / `get_openai_responses()` 决定（选择器 + 启用过滤，见 [tool_service.md](tool_service.md)）。

## 设计决策

- 注册中心为纯容器（注册 / 查询 / 元数据过滤）：执行职责独立到 executor，Schema 导出由 Facade 独占 → [ADR](../../../adr/integration/tools/2026-08-17-six-component-alignment.md)（文档导航见 [工具模块接口文档](tools.md)）

## 测试

`tests/unit/test_tool_registry_metadata.py`（8 用例）：`all_tools` / `list_by_risk` / `list_by_category` 过滤正确性、注销已注册工具、注销不存在工具返回 False、重名注册抛 `ValueError`（不覆盖）、定义非法注册抛 `SchemaError` 且不入容器、单个非法定义不影响同批工具的 Schema 导出。

## 相关文档

- [工具模块接口文档](tools.md)（ToolService 方法表）
- [executor.md](executor.md)（执行编排，本组件是 executor 的依赖）
- [security.md](security.md)（`list_by_risk` 消费方）
