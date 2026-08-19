# 结果处理器（ResultProcessor）说明文档

> **更新日期**：2026-08-17
> **模块**：`app/integration/tools/result_processor.py`
> **职责**：工具执行结果的统一 head+tail 截断 + 错误归一化
> **状态**：✅ 已实现
> **工业级对照**：head+tail 截断策略 + 截断标记（AgentScope / pydantic-ai-harness），避免各工具各自内联截断的分散实现

---

## 📋 目录

- [结果处理器（ResultProcessor）说明文档](#结果处理器resultprocessor说明文档)
  - [📋 目录](#-目录)
  - [定位与职责](#定位与职责)
  - [接口契约](#接口契约)
  - [行为边界](#行为边界)
  - [使用示例](#使用示例)
  - [设计决策](#设计决策)
  - [测试](#测试)
  - [相关文档](#相关文档)

---

## 定位与职责

工具层返回的原始结果（读大文件、命令 stdout、网页正文）可能远超 LLM 上下文预算。ResultProcessor 在 executor 成功分支统一做 head+tail 截断：**保留开头（上下文/指令）与结尾（code_exec 的 stderr/traceback 常在尾部），中间以标记替换**，让 LLM 感知被移除内容。

内置工具不再各自内联截断，截断收敛到本组件单点（避免双重截断）。

## 接口契约

| 方法 | 签名 | 说明 |
| --- | --- | --- |
| `truncate` | `(content: str, *, max_length=None, head_ratio=None) -> str` | head+tail 截断；`max_length` 缺省用 `default_max_length`（100_000），`head_ratio` 缺省 0.7 |
| `truncate_result` | `(result: ToolResult, *, max_length=None) -> None` | 就地截断 `result.content`；截断发生时 `metadata['truncated']=True` |
| `normalize_error` | `(error: str\|None) -> str` | 错误归一化：None→''、去空行与行尾空白、保留行首缩进（traceback 可读性）、超长截断（保留头部，`max_error_length` 缺省 2_000） |

**截断语义**：`head = int(max_length * head_ratio)`，`tail = max_length - head`，中间替换为 `marker`（默认 `\n...（内容已截断，共 {original_len} 字符，仅保留首尾）\n`）。

## 行为边界

| 场景 | 行为 |
| --- | --- |
| `len(content) <= max_length` | 原样返回，无标记 |
| 超长 | head + marker + tail（含 marker 故总长略超 max_length） |
| `content` 为空 | 不截断 |
| `normalize_error(None)` | 返回 `''` |
| 错误超长 | 保留头部 + `...（错误过长已截断）` |
| 截断触发 | `truncate_result` 置 `metadata['truncated']=True`；未触发不设该字段 |

## 使用示例

```python
processor = ResultProcessor()
result = ToolResult(success=True, content="A" * 1000)
processor.truncate_result(result, max_length=100)
# result.content → head(70) + marker + tail(30)，metadata["truncated"] is True
```

实际调用方：`executor._execute_with_retry` 成功分支，`max_length=tool.max_output_length`（工具自声明，缺省回退 default_max_length）。

## 设计决策

- head+tail 7:3 默认比 + 截断收敛单点 + 工具自声明 max_length → [ADR](../../../adr/integration/tools/2026-08-17-six-component-alignment.md)

## 测试

`tests/unit/test_result_processor.py`（11 用例）：head+tail 边界 / 自定义 head_ratio / 空内容 / truncate_result 就地标记与未触发 / normalize_error 边界（含 traceback 缩进保留）。

## 相关文档

- [工具模块接口文档](tools.md)（BaseTool.max_output_length 契约）
- [内置工具说明](builtin_doc/builtin.md)（各工具截断配置）
- [TOOLS-017 问题记录](../../../issues/integration/tools/2026-08-19-normalize-error-indent.md)（normalize_error 缩进保留）
