# TOOLS-006 executor 参数 JSON 解析结果未校验为 dict，非 dict 抛 TypeError 逃逸

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P1（正确性 / 契约）
> **来源**：2026-08-18 工具模块代码审核（编排核心层 · 重要项 1）
> **涉及模块**：`app/integration/tools/executor.py`（ToolExecutor.execute 参数解析）
> **关联文档**：[executor.md](../../../docs/integration_doc/tools_doc/executor.md)

---

## 问题描述

### 现象

`parameters` 为 str 时 `json.loads` 成功后直接 `tool.validation_issues(**parameters)`。若解析结果是 JSON 数组 / 标量 / null（如 LLM 返回 `"[1,2,3]"`），`**parameters` 抛 `TypeError`，不在任何 try 内——异常逃逸整个编排层，破坏 Agent 循环 / 路由 500，违反「executor 统一失败分类、不让异常抛出」契约。

### 影响

LLM 对参数生成输出偶发非对象 JSON（长输出 / 模型行为漂移）时，工具执行路径整体崩溃而非优雅失败；调用方直接传含非 str 键的 dict 同样触发。

### 根因

`json.loads` 的合法返回值类型多样（dict / list / str / int / float / bool / None），解析后未做「字符串键 dict」的契约校验即进入 `**` 解包。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| `json.loads` 返回值契约 | 合法返回类型含 list / 标量 / null——**解包前必须 `isinstance` 校验** |
| Marshmallow / attrs 反序列化 | 入参契约校验失败返回结构错误，而非运行时爆炸 |
| Python `**` 解包语义 | 非 str 键会抛 `TypeError: keywords must be strings`——键类型同样需校验 |

**核心**：反序列化契约校验（是对象 + 键为字符串）在解包前完成，失败归 `JSON_PARSE` 结构化返回。

---

## 修复方案（含决策取舍）

**决策**：str 分支 `json.loads` 后，统一增加「字符串键 dict」校验（覆盖全部入参形态）：

```python
# 3. 解析参数（str → dict），并统一校验为「字符串键的 dict」
if isinstance(parameters, str):
    try:
        parameters = json.loads(parameters)
    except json.JSONDecodeError as e:
        ... JSON_PARSE 返回 ...
if not isinstance(parameters, dict) or any(
    not isinstance(k, str) for k in parameters
):
    result = ToolResult(
        success=False, content="",
        error=... f"参数必须为 JSON 对象（字符串键），收到 {type(parameters).__name__}",
        error_code=ErrorCode.JSON_PARSE,
    )
    await self._audit(tool, parameters, result, started_at=started_at)
    return result
```

**取舍理由**：

1. **校验放在 str 分支之外**：不仅覆盖 `json.loads` 结果（LLM 输出数组/标量），也覆盖调用方直接传入非 dict / 非 str 键 dict——单一校验点统一拦；
2. **归 `JSON_PARSE`**：与解析失败同类（入参形态非法），复用错误码与审计路径，语义一致；
3. **`type(parameters).__name__` 归因**：错误信息告知实际类型（list / int / NoneType），便于 LLM 修正输出。

**语义边界**：

- 合法字符串键 dict 行为不变；
- 非 dict 审计时 `_audit` 走 `{"raw": str(parameters)[:500]}` 兜底序列化（安全）；非 str 键 dict 审计会触发 auditor 序列化失败但被「审计失败不阻断」兜底吞掉。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/executor.py` | 参数解析步骤后加「字符串键 dict」校验，非 dict / 非 str 键归 `ErrorCode.JSON_PARSE` | `tests/unit/test_tool_executor_components.py` 新增 3 用例：`test_execute_non_dict_json_rejected`（参数化 `[1,2,3]`/`null`/`42`/`"str"`/`true`）+ `test_execute_non_str_key_dict_rejected`（`{1:"a"}`）+ `test_execute_list_parameters_rejected`（直接传 list） |
| 文档 | [executor.md](../../../docs/integration_doc/tools_doc/executor.md) 执行流程第 3 步补「字符串键 dict」校验说明 | — |

---

## 验证

- 相关测试 **20 passed**（含 7 个新增非 dict 校验用例）
- 全量测试待提交前确认（增量改动：仅参数校验路径，无回归面）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **`json.loads` 合法返回值不止 dict**：解包（`**`）前必须校验「对象 + 字符串键」契约，否则非法形态直接运行时爆炸。
- **契约校验前置到编排层统一完成**：校验失败结构化返回（`JSON_PARSE` + 归因）优于异常逃逸——LLM 能读到错误并修正，而非整链路崩溃。
- **校验覆盖所有入参形态**：不仅是 JSON 字符串分支，直接传参（list / 非 str 键 dict）同样拦截——单一校验点，避免调用方绕过。
