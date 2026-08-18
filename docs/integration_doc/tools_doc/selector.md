# 工具选择器（ToolSelector）说明文档

> **更新日期**：2026-08-17
> **模块**：`app/integration/tools/selector.py`
> **职责**：从注册工具集中选出本次注入 LLM 的工具子集（预留接口，默认全量注入）
> **状态**：✅ 已实现（仅接口 + 默认实现，向量召回未实现）

---

## 📋 目录

- [定位与职责](#定位与职责)
- [接口契约](#接口契约)
- [预留路径](#预留路径)
- [设计决策](#设计决策)
- [测试](#测试)
- [相关文档](#相关文档)

---

## 定位与职责

LLM 每轮 Function Calling 都收到全部工具定义，工具集超过阈值（工业界约 50 个）后选择准确率下降、Token 成本与首 token 延迟上升。选择器在「注入 LLM」前按需裁剪工具子集。

当前 10 个内置工具属**小体量（<50）**，工业标准是全量注入 + LLM 原生 Function Calling，故只定义协议 + 默认全量实现，**向量召回（embedding 粗排 + LLM 精排）留待工具膨胀后实现**。

## 接口契约

| 类型 | 签名 | 说明 |
| --- | --- | --- |
| `ToolSelector` | `Protocol`，`def select(tools: list[BaseTool]) -> list[BaseTool]` | 选择器协议：从注册工具集选出注入子集（保持原顺序） |
| `DefaultToolSelector` | `def select(tools) -> tools` | 默认全量注入 |

注入点：`ToolService.__init__(selector=...)`，`get_openai_tools()` 内部经选择器导出——**协议方法 `get_openai_tools()` 签名保持零参数**，`ToolGateway` 结构不变，Agent 层无感知。

## 预留路径

- 工具数 <50：默认全量注入，零配置
- 工具数 >50：实现 `ToolSelector` 协议（embedding 粗排 + 可选 Rerank 精排），构造期注入 `ToolService(selector=MySelector())`，Facade 与 Agent 零改动
- `get_openai_responses()` 不走选择器（保持全量转储，该 API 当前零生产者）

## 设计决策

- 小体量全量注入 + 只留接口不实现召回 → [ADR](../../../adr/integration/tools/2026-08-17-six-component-alignment.md)

## 测试

`tests/unit/test_tool_selector.py`（6 用例）：全量返回顺序 / 默认导出全部 / 自定义过滤只导出子集 / execute 不受影响 / 零注册空列表 / ToolGateway 协议满足。

## 相关文档

- [工具模块接口文档](tools.md)（ToolService.get_openai_tools 契约）
