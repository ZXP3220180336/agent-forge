# TOOLS-009 search 搜索结果与 answer 不携带来源 URL，证据不可回溯

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P1（产品主链路卖点——证据链）
> **来源**：2026-08-18 工具模块代码审核（builtin 通用工具组 · 重要项 6）
> **涉及模块**：`app/integration/tools/builtin/search.py`（SearchTool 结果格式化）
> **关联文档**：[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

---

## 问题描述

### 现象

- **answer 路径**：只回答案字符串，`metadata` 仅 `source: "tavily_answer"`，来源 URL 全丢弃；
- **搜索结果路径**：格式化 `- 标题: 内容`，无 URL。

### 影响

产品主链路卖点是「带证据链的根因报告」——search 结果缺来源 URL，Agent 引用的事实不可回溯，证据链断裂（与 RCA 工具证据链 metadata 设计不一致）。

### 根因

Tavily 结果含 `url` 字段但格式化时未携带；answer 路径未从 results 提取 URL 佐证。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| 搜索结果标准展示（Google / Bing / Tavily 文档） | 标题 + 摘要 + **来源链接**——链接是搜索结果的固有要素 |
| RAG 引用 / 可溯源原则 | 每个 claim 关联来源（证据链可回溯），LLM 输出可验证 |
| 数据缺失兜底 | `dict.get("url", "")`，字段形态变化不崩溃（`result['url']` 直取会 KeyError） |

**核心**：证据链 = 内容 + 来源；URL 是「可回溯」的最小载体，必须随内容一起给 Agent。

---

## 修复方案（含决策取舍）

**决策**：两条路径均携带来源 URL：

| 路径 | 改动 |
| --- | --- |
| answer | `metadata["urls"]` = 前 3 条 results 的 `url`（过滤空值） |
| 搜索结果 | 行尾追加 `（来源: {url}）`（无 url 时省略后缀） |
| 字段兜底 | `title` / `content` / `url` 均改 `result.get(...)`，缺字段不崩溃 |

**取舍理由**：

1. **answer 进 metadata 而非 content**：答案本身已完整，URL 作证据链元数据（审计 / 结构化消费可回溯），不污染 content；
2. **前 3 条**：证据佐证足够且不膨胀 metadata（全部 URL 冗余）；
3. **URL 行尾追加**：结果行内容完整时尾部是证据锚点，ResultProcessor head+tail 截断保留尾部 → URL 不被截丢。

**语义边界**：Tavily 结果缺 `url` 字段时静默省略后缀（不伪造来源）；无 results 时 `urls=[]`。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/search.py` | answer 路径 metadata 加 `urls`（前 3 条）；搜索结果行尾追加 `（来源: url）`；字段改 `.get` 兜底 | `test_tool_execution.py` 新增 3 用例：`test_search_answer_includes_source_urls`（mock Tavily，urls 进 metadata）+ `test_search_results_include_source_urls`（行尾 URL）+ `test_search_result_missing_url_tolerated`（缺 url 不崩） |
| 文档 | [builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md) search 实现要点更新（answer urls + 结果行 URL） | — |

---

## 验证

- 相关测试 **4 passed**（含 3 个新增 search 用例）
- 全量测试待提交前确认（增量改动：仅 search 格式化与 metadata，无回归面）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **证据链的最小载体是来源**：搜索结果 / 引用型工具必须把 URL 随内容给 Agent，否则主链路「带证据链报告」名不副实。
- **证据进 metadata 而非污染 content**：answer 场景 URL 作结构化元数据，content 保持答案纯净，审计 / 消费方可回溯。
- **外部数据形态用 `.get` 兜底**：第三方 API（Tavily）字段不可信，直取索引会让整次搜索失败——`.get` 保形态变化不崩。
