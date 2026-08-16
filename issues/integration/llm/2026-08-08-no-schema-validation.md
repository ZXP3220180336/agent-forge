# 解析后无 Schema 校验，模型返回「结构合法但值非法」数据直接进业务

> **状态**：✅ 已修复（2026-08-08）
> **优先级**：P0（严重，合并前必修）
> **来源**：2026-08-07 structured 模块审核（问题 1）· 2026-08-16 从 structure.md 提取归档
> **涉及模块**：`app/integration/llm/structured.py`（`_try_extract` / `_fallback_extract`）
> **关联文档**：[structure.md](../../../docs/integration_doc/llm_doc/structure.md)

---

## 问题描述

### 现象

`_try_extract` / `_fallback_extract` 在 `json.loads` 后不校验 Schema——模型返回 `{"confidence": "很高"}`（Schema 要求 `confidence` 0~1）也会当成功返回，上层信以为真，**系统不知道自己不稳定**。

### 影响

「结构合法但值非法」的坏数据直接进业务；Schema 上 `minimum`/`maximum`/`pattern` 等值约束不被 strict 模式保证（strict 只锁结构不锁值）。

### 根因

解析后无 Schema 校验环节——模型返回之后直接进业务逻辑，缺工业级生产链路的「程序校验」一环。

---

## 工业级参照

| 结论 | 做法 |
| --- | --- |
| **双保险是业界共识** | 即使服务端 strict mode，客户端也必须再校验一次——strict 只锁结构（字段存在/类型/enum/无多余字段），不锁值（`minimum/maximum/pattern/format`）；refusal、截断绕过 schema |
| **校验失败主路径** | 「带错误回喂重试（1~3 次）→ 失败后再降级」，而非直接降级；必须把 ValidationError 转人话 + 模型上次输出一起回喂 |
| **校验库选型** | schema 是 JSON Schema dict → 直接用 `jsonschema` 库（Pydantic 作者官方表态「JSON Schema 实例校验归 jsonschema」） |

> 调研对象：OpenAI/Anthropic Strict Outputs、Instructor、LangChain、Guardrails AI、Outlines/JSONFormer、jsonschema vs Pydantic 选型（2026-08-08）。

---

## 修复方案（含决策取舍）

**决策**：新增 `_validate_schema`（jsonschema `validate()`），`_try_extract` / `_fallback_extract` 解析出 dict 后按 schema 校验；校验失败记日志 → 返回 `None` → 触发降级。

**取舍理由**：

1. **jsonschema 选型**：schema 是 JSON Schema dict（非 Pydantic model），`jsonschema` 零转换直接校验，与调研结论一致；
2. **strict 锁结构，本地校验锁值**：`minimum/maximum` 等值约束 strict 不保证，`_validate_schema` 兜底（双保险）；
3. **校验失败触发降级**：与三级降级链衔接——校验失败先回喂重试（问题 3），耗尽再降级；
4. **非法 schema 有防护**：`validate()` 抛非 `ValidationError`（schema 非法）也记日志返回 False，不静默穿透；
5. **失败日志脱敏（2026-08-16）**：`e.message` 嵌入完整实例值（敏感泄露面），改用结构化字段摘要 + `parsed` 截断 500 字符。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/structured.py` | 新增 `_validate_schema`（jsonschema）；`_try_extract`/`_fallback_extract` 解析后校验，失败返回 None 触发降级 | `test_generate_structured.py` 校验失败降级用例 |

---

## 验证

- 三级降级每一级带校验兜底，不再返回「结构合法但值非法」坏数据
- 全量测试通过（2026-08-08 修复时验证）

---

## 教训沉淀

- **程序校验的意义不是"让模型更稳定"，而是让系统知道模型什么时候不稳定**——模型输出当不可信输入，生产链路必须有 Schema 校验一环。
- **strict 只锁结构不锁值**：`minimum/maximum/format` 等值约束必须本地校验兜底（双保险）。
