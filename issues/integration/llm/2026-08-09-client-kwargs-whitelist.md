# `**extra` 参数被静默吞没：get_client 只取 api_key/base_url

> **状态**：✅ 已修复（2026-08-09）
> **优先级**：P1（中）
> **来源**：2026-08-09 审核修复（client.md Q3）· 2026-08-16 从 client.md 提取归档
> **涉及模块**：`app/integration/llm/client.py`（`ClientManager.get_client`）
> **关联文档**：[client.md](../../../docs/integration_doc/llm_doc/client.md)

---

## 问题描述

### 现象

旧实现 `get_client` 只取 `api_key` 和 `base_url`，调用方传入的 `organization`、`timeout`、`max_retries` 等参数虽存入 `_configs`，但**永远不会传给 `AsyncOpenAI`**——被静默吞没。

### 影响

配置的 timeout/max_retries/organization 等参数不生效（静默），行为与配置意图不符。

### 根因

无参数白名单——`get_client` 只显式取少数字段，其余 `**extra` 丢在 `_configs` 不透传。

---

## 工业级参照

| 结论 | 做法 |
| --- | --- |
| 参数白名单 | `AsyncOpenAI` 构造函数只认固定参数集——定义白名单 `_OPENAI_CLIENT_KWARGS` 筛选交集字段透传，隔离业务语义字段（model/proxy_url）与 SDK 参数 |

---

## 修复方案（含决策取舍）

**决策**：定义 `_OPENAI_CLIENT_KWARGS` 白名单，`get_client` 从配置中筛选交集字段传给 `AsyncOpenAI`。

**修复要点**：

1. `_OPENAI_CLIENT_KWARGS = {api_key, organization, base_url, timeout, max_retries, default_headers, default_query, http_client, websocket_client}`；
2. 白名单外字段（`model`、`proxy_url`）存 `_configs` 供其他方法用，不透传——避免非法参数透传给 SDK 抛 TypeError。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/client.py` | `_OPENAI_CLIENT_KWARGS` 白名单 + `get_client` 筛选透传 | `test_client_manager.py` 参数透传用例 |

---

## 验证

- organization/timeout/max_retries 等参数正确透传给 AsyncOpenAI；model/proxy_url 不透传
- 全量测试通过（2026-08-09 修复时验证）

---

## 教训沉淀

- **SDK 参数透传用白名单**：`AsyncOpenAI` 只认固定参数集——白名单筛选交集字段，避免业务字段（model/proxy_url）非法透传 + `**extra` 被静默吞没。
