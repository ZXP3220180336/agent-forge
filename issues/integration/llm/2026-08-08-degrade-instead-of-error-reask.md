# 校验失败直接降级，缺「错误感知重试」主路径

> **状态**：✅ 已修复（2026-08-08）
> **优先级**：P1（中）
> **来源**：2026-08-07 structured 模块审核（问题 3）· 2026-08-16 从 structure.md 提取归档
> **涉及模块**：`app/integration/llm/structured.py`（`_try_extract` 回喂循环）
> **关联文档**：[structure.md](../../../docs/integration_doc/llm_doc/structure.md)

---

## 问题描述

### 现象

第一级 schema 失败 → 直接降级到 JSON mode（约束更弱）→ 失败 → 正则（约束最弱）。**三级都在「换约束」，没有一次「带错误重试」**。

### 影响

校验失败直接降级，缺「错误感知重试」主路径——对 cheap 模型最有效的纠错手段缺失；降级到更弱约束往往仍失败。

### 根因

校验失败只降级、不回喂错误——工业级主路径是「把具体校验错误回喂模型修正」，而非直接降级。

---

## 工业级参照

| 结论 | 做法 |
| --- | --- |
| **回喂重试是业界标准主路径** | Instructor `max_retries`、Guardrails `num_reasks`、LangChain RetryOutputParser 全部以此为标准模式 |
| **错误必须回喂 + 保留上次输出** | 上次失败 assistant 输出保留在历史里 + 末尾追加 user 消息（具体校验错误 + 修正指令）；错误格式化成**人话指令**，绝不原样拼 traceback |
| **重试上限 2~3 次** | 温度保持 0，校验失败重试无需退避（退避是 retry.py 职责） |
| **与降级链组合** | 先重试、后降级，每一级各自重试——降级到更弱约束救不了本级错误（如值约束违反，JSON mode/正则根本不查值约束）；最坏调用数 (1+2)+(1+2)+1=7 |

> 调研对象：Instructor reask、Guardrails AI REASK、LangChain RetryOutputParser 及通用 best practice（2026-08-08）。

---

## 修复方案（含决策取舍）

**决策**：`_try_extract` 的解析校验段外包回喂循环（同一 response_format，同约束下重试），校验失败把具体错误回喂模型修正。

**修复要点**：

- **`_collect_schema_errors`**：`Draft7Validator.iter_errors` 收集全部校验错误，格式化为「字段路径: message」人话（一次改完，非第一条）；
- **`_build_reask_messages`**：clone + 保留上次失败 assistant 输出 + 末尾追加 user 错误反馈——**不污染调用方 messages**；
- **回喂上限 `_REASK_MAX_RETRIES = 2`**（工业共识 2~3 次），温度保持 0，无需退避；
- **降级组合**：strict 级回喂 2 次（值约束只能这级救）、JSON mode 级回喂 2 次、正则级不加（最弱约束重试收益最低）；
- **回喂循环内保留三态检查**：截断一律短路（防 token 爆炸，对齐 Instructor PR #2232）、拒答短路；
- **返回契约 `dict | None` 不变**：回喂耗尽返回 None 触发降级。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/structured.py` | 新增 `_REASK_MAX_RETRIES` / `_collect_schema_errors` / `_build_reask_messages` / `_parse_and_validate`；`_try_extract` 外包回喂循环 | `test_generate_structured.py` 回喂重试/回喂耗尽降级/终态解析用例 |

---

## 验证

- strict/JSON mode 级回喂 2 次，正则级不加；校验失败先回喂再降级
- 回喂成功用例 + 回喂耗尽降级用例通过（2026-08-08 修复时验证）

---

## 教训沉淀

- **降级到更弱约束救不了本级的错误**：如值约束违反，JSON mode/正则根本不查值约束——错误回喂是唯一修正机会，必须每级各自重试。
- **错误回喂设计要点**：保留上次失败输出（self-correction 关键）、user 消息而非 system（一次性指令）、一次收集全部错误、格式化成指令而非 traceback。
- **回喂内截断必须短路**：截断的半截 JSON 反复回喂吃 token（Instructor 烧 150 万 token 教训）。
