# LLM-007 非法 schema 使 iter_errors 崩溃，与 `_validate_schema` 防护不一致

> **状态**：✅ 已修复（2026-08-16）
> **优先级**：P1（近期）
> **来源**：2026-08-16 Integration 层 LLM 模块工业级审核（重要项 6）
> **涉及模块**：`app/integration/llm/structured.py`（`_collect_schema_errors` / `_collect_schema_error_summaries`）
> **关联文档**：[structure.md](../../integration_doc/llm_doc/structure.md)

---

## 问题描述

### 现象

`_parse_and_validate` 调 `_collect_schema_errors`（`Draft7Validator(schema).iter_errors`）无异常防护；已实测非法 schema（`properties:5`、`type:123`、未知 `type`）会使 `iter_errors` 抛 `AttributeError`/`TypeError`/`UnknownType`，穿透到调用方。孪生函数 `_validate_schema`（structured.py:271-276）却有 `except Exception` 显式兜底——同一类风险两套路径防护不一致。

### 影响

调用方传入非法 schema 时，第一/二级（回喂路径）直接崩，第三级（正则路径 `_validate_schema`）却能优雅返回 False——崩溃形态取决于命中哪级降级，行为不可预期。

### 根因

`_collect_schema_errors` / `_collect_schema_error_summaries` 未防御 `jsonschema` 对非法 schema 抛出的 `SchemaError` / `UnknownType` / `TypeError`。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| jsonschema 官方 | 非法 schema 抛 `SchemaError` / `UnknownType`（`jsonschema.exceptions`）；防御用 `check_schema` 预检 + catch 运行时异常 |
| 本项目 `_validate_schema` | `except Exception` 兜底：非法 schema 记 `logger.error` + 返回 False（触发降级）——修复应对齐 |

**核心**：schema 是运行时输入，非法时应按「校验失败」处理（记日志 + 降级），而非崩溃。

---

## 修复方案（含决策取舍）

**决策**：`_collect_schema_errors` / `_collect_schema_error_summaries` 包 `try/except Exception`——非法 schema 记 `logger.error` + 返回错误信息列表（非空 → 触发降级），与 `_validate_schema` 兜底一致。

**取舍理由**：

1. 对齐 `_validate_schema` 的既有兜底（同一类风险统一防护）；
2. 非法 schema 按校验失败处理（返回错误 → 回喂/降级），不崩溃、行为可预期；
3. 保留错误信息（`e`）供日志诊断。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/structured.py` | `_collect_schema_errors` / `_collect_schema_error_summaries` 包 try/except，非法 schema 记 ERROR + 返回错误列表 | `test_generate_structured.py` 新增 `test_invalid_schema_returns_none_not_crash`（3 种非法 schema） |
| 文档 | [llm.md](../../integration_doc/llm_doc/llm.md)（已实现列表加 LLM-007 条目） | — |

---

## 验证

- `tests/unit/test_generate_structured.py` **47 passed**（含新增非法 schema 用例）
- 全量测试 **360 passed**（45.20s），无回归
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **schema 是运行时输入，非法时必须防御**：`Draft7Validator(schema)` 对非法 schema 抛 `SchemaError`/`UnknownType`——所有 schema 校验入口统一 `except Exception` 兜底（记日志 + 返回失败），避免同一类风险两套路径行为不一致。
