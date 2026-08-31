# REASON-008 空输出重试轮向历史追加空 assistant 消息：累积污染上下文

> **状态**：✅ 已修复（2026-08-31）
> **优先级**：P3（低影响——空消息 token 极少，上下文预算可裁剪；但空输出重试累积空消息污染上下文 / 干扰模型）
> **来源**：2026-08-31 React 策略完成度评审发现（边缘情况）
> **涉及模块**：`app/domain/reasoning/react.py`
> **关联文档**：[react.md](../../../docs/domain_doc/reasoning_doc/react.md)

---

## 问题描述

### 现象

主循环第 7 步无条件把本轮 assistant 消息追加进 `messages`（含 `content=""` 的空消息）。空输出重试轮（`finish_reason` 空 + content 空 + 无 tool_calls）同样追加 `{"role": "assistant", "content": ""}`——**连续空输出重试会累积多条空 assistant 消息**。

### 影响

- 空 assistant 消息无信息量，累积占用上下文（token 极少，但占历史槽位）；
- 模型在历史中看到多条「什么都没说」的 assistant 消息，可能干扰后续输出语义。

### 根因

追加 assistant 消息无「本轮是否有产出」的守卫——空输出轮没有产出却写入历史。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| 主流 Agent 框架 | 空输出轮（模型未生成有效内容）不写入对话历史——无信息量的消息进历史只会污染上下文 |

**核心**：只有「本轮有产出」（content / reasoning / tool_calls）才追加 assistant 消息。

---

## 修复方案（含决策取舍）

**决策**：

1. **纯空轮不追加**：第 7 步追加守卫 `full_content or full_reasoning or has_reasoning or tool_calls`——全空（无 content / 无 reasoning / 无 tool_calls / 无 has_reasoning 信号）跳过追加。
2. **保留 `has_reasoning` 在守卫内**：thinking 模型返回空 reasoning（`has_reasoning=True`）仍追加（`reasoning_content` 回喂字段防 400 的需要，见 reasoning_content 回喂节）——仅「完全无信号的纯空轮」跳过。

**取舍理由**：

1. 不改为「重试时移除」：追加前守卫更简单（避免后置清理逻辑），且语义清晰（无产出的轮不写历史）；
2. `has_reasoning=True` 的空 reasoning 轮不算纯空轮（有模型输出信号），追加保持防 400 语义不变。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/domain/reasoning/react.py` | 第 7 步追加守卫：纯空轮（无 content/reasoning/has_reasoning/tool_calls）不追加 assistant 消息 | `tests/unit/test_react_strategy.py` 新增 1 例 |

---

## 验证

- **测试驱动**：`test_react_empty_output_retry_no_blank_assistant`——连续 2 轮空输出重试 → 消息历史只含 user + 最终 assistant（`assistant_msgs` 长度 1，content="完成"），不含空 assistant 记录（修复前长度 3）。
- `tests/unit/test_react_strategy.py` 76 passed；`uv run pytest` 全量通过；`verify_alignment` 通过。

---

## 教训沉淀

- **写入历史要有「产出守卫」**：任何「追加进消息历史」的操作应校验本轮是否有可回馈的信息——空输出轮（模型什么都没说）写历史既无益又污染；
- **保留信号的边界**：thinking 模型返回空 reasoning（`has_reasoning=True`）是有信号的轮（防 400 需要回喂字段）——守卫用「信号存在」而非「内容非空」，避免误伤 thinking 场景。
