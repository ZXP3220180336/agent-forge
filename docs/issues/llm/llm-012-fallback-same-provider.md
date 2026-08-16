# LLM-012 fallback 降级只支持同 provider，与文档「备用服务商」表述不符

> **状态**：✅ 已修复（2026-08-16）
> **优先级**：P1（近期，文档-实现偏差）
> **来源**：2026-08-16 Integration 层 LLM 模块工业级审核（重要项 11）
> **涉及模块**：`app/integration/llm/llm_service.py`（`_build_fallback_fn`）· 文档（llm.md / retry.md / config）
> **关联文档**：[llm.md](../../integration_doc/llm_doc/llm.md) · [retry.md](../../integration_doc/llm_doc/retry.md)

---

## 问题描述

### 现象

`_build_fallback_fn`（llm_service.py:64-84）用主模型 client（`ClientManager.get_client(model_key)`）发 fallback 请求，**仅替换 `model` 参数**。配置仅 `llm_fallback_model_id` 一个字段，无独立 `base_url`/`api_key`。而 [llm.md](../../integration_doc/llm_doc/llm.md) 宣称「降级到便宜模型或**备用服务商**」——按此配置跨服务商 fallback（如主 OpenAI、备 DeepSeek），fallback 请求会打到主 provider 端点带备用模型名 → 400/404，**fallback 永远失败**，主模型故障时系统反而不可用。

### 影响

- 跨 provider fallback 配置下，降级路径静默失效（主模型故障 → fallback 也失败 → 系统不可用）；
- 文档宣称能力与实现不符，排障时误以为 fallback 生效。

### 根因

实现只支持「同 provider 降级」（fallback 复用主模型 client 的 base_url/密钥），文档却宣称「备用服务商」——文档与实现偏差。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| LiteLLM Router | 多 provider 路由，每个模型独立 `base_url`/`api_key` 配置，跨服务商 failover 是显式能力 |
| 本项目简化设计 | 仅 `llm_fallback_model_id`（无独立 endpoint/key）→ 隐含「同 provider」约束，需文档化 |

**核心**：fallback 复用主 client 端点 = 同 provider 降级；跨 provider 需要独立配置（LiteLLM 式），当前不提供——约束必须文档化，避免宣称不符。

---

## 修复方案（含决策取舍）

**决策**：**文档化「fallback 须与主模型同 provider（同 base_url）」约束**，代码注释明确；修正 llm.md「备用服务商」表述为「同服务商便宜模型」。

**取舍理由**：

1. 当前实现是「同 provider 降级」的合理简化（降级到同服务商便宜模型，如 deepseek-chat → deepseek-reasoner）；
2. 支持跨 provider 需新增 `llm_fallback_base_url` / `llm_fallback_api_key` 配置 + 独立 client——功能增强，超出本问题范围（记录为后续可选）；
3. 最小正确修复：约束文档化，实现与文档一致。

**语义边界**：

- 同 provider fallback（降级便宜模型）→ 支持（现状）；
- 跨 provider fallback → **不支持**，文档明确说明（避免配置后静默失效）；
- 未来若需跨 provider → 新增独立 fallback client 配置（ADR 记录）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/llm_service.py` | `_build_fallback_fn` 加「同 provider 约束」注释（复用主 client 端点/密钥） | 无（现有 `test_generate_passes_fallback_fn` 已覆盖 fallback 用主 client） |
| `docs/integration_doc/llm_doc/llm.md` | 修正「备用服务商」→「同服务商便宜模型」+ 设计说明/配置表补约束 | — |
| `docs/integration_doc/llm_doc/retry.md` | `LLM_FALLBACK_MODEL_ID` 配置表补同 provider 约束 | — |
| `docs/integration_doc/llm_doc/llm.md`（已实现列表） | 加 LLM-012 条目 | — |

---

## 验证

- 全量测试 **364 passed**（无代码逻辑变化，回归确认；修复 llm-012 文档死链后 `test_verify_alignment` 通过）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **文档宣称的能力必须与实现一致**：fallback 复用主 client = 同 provider 降级；文档写「备用服务商」造成跨 provider 配置静默失效——约束需显式文档化。
- **最小修复 vs 功能增强**：文档化约束（低风险）与支持跨 provider（需新增配置面）分开——本问题先对齐文档，跨 provider 作为独立后续项。
