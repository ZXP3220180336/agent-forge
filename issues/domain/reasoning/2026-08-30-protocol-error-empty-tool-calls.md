# REASON-004 finish_reason=tool_calls 信号与数据/工具可用性不一致：协议异常短路

> **状态**：✅ 已修复（2026-08-30）
> **优先级**：P2（协议信号不一致；模型持续返回时浪费轮次 / 空输出计数语义错误与护栏失效）
> **来源**：2026-08-30 代码审查发现（Agent 运行异常处理全面性评审，问题 3）+ 完成度评审（无工具场景）
> **涉及模块**：`app/domain/reasoning/react.py`
> **关联文档**：[react.md](../../../docs/domain_doc/reasoning_doc/react.md)

---

## 问题描述

### 现象

`finish_reason="tool_calls"` 而模型侧与系统侧不一致时，**两类触发**均被错误处理：

**① 信号但无数据**（`tool_calls` 为空列表，服务端异常 / 响应被截断）：

1. **注册了工具（`has_tools=True`）**：进入工具调用分支，yield「检测到 0 个工具调用」荒谬 info → `execute_tool_calls([])` → `asyncio.gather()` 空列表 → 无 tool 消息、无事件 → 静默 `continue`。**模型持续返回该信号则每轮浪费一次 LLM 调用直到 `max_iterations`**，且无任何有效信号透出。
2. **未注册工具（`has_tools=False`）**：落入空输出分支，**误计入空输出重试计数**。

**② 要调工具但系统无工具**（`tool_calls` 非空 + `has_tools=False`，服务端状态错乱 / 配置不一致）：

1. 协议异常分支只检查「`tool_calls` 空」、工具执行分支要求 `has_tools`——**都不命中** → 落入空输出分支重试（「模型要调工具」有明确意图，不是空输出，语义错误）；
2. 且非空 `tool_calls` 使空输出计数清零（`stream_result.tool_calls` 为真）→ **`max_empty_retries` 护栏失效**，连续多次仅靠 `max_iterations` 兜底。

### 影响

- 协议信号不一致时行为语义错误：不是「空输出」（有调用意图）、不是「工具失败」（无工具可执行），却被按其中一种处理；
- 空转执行路径浪费 LLM 轮次且无有效事件；无工具场景下重试护栏静默失效（比直接没有护栏更隐蔽）。

### 根因

主循环 finish_reason 分支未校验「tool_calls 信号」与「**数据（`tool_calls`）/ 能力（`has_tools`）**」的一致性——仅按 `finish_reason` 分流，未覆盖信号不一致的完整维度（只校验了数据侧，遗留了能力侧）。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| OpenAI 协议 | `finish_reason="tool_calls"` 的前提是请求注入了 tools 且 `message.tool_calls` 非空；任一不满足均为协议不一致，非合法状态 |
| Vercel AI SDK | `parseToolCallStreamPart` 解析失败/信号不一致 → 视为 tool-call 流异常处理（不静默空转） |
| 本项目既有 `PARSE_FAILED` | 语义「模型输出协议不合法」（工具参数 JSON 解析失败 → 默认 CONTINUE 回喂/重试）——tool_calls 空列表 / 无工具可调同属协议不合法 |

**核心**：协议信号与数据 / 工具可用性不一致是「模型输出协议异常」，应独立识别并走已有的解析异常分发（PARSE_FAILED），而非被空输出 / 工具执行路径吞掉。

---

## 修复方案（含决策取舍）

**决策**：

1. **协议异常识别覆盖两类不一致**：`finish_reason == "tool_calls" and (not stream_result.tool_calls or not has_tools)` → `_finalize_protocol_error`（结构对齐 `_finalize_llm_failed`：先 `_dispatch` 拿 action）：
   - 默认 CONTINUE → yield info（含「协议异常」）后重试下一轮；
   - STOP → `_finalize_outcome` 终止（error 记录协议异常）；
   - RAISE → `_dispatch` 抛 `AgentRunError(PARSE_FAILED)`。
2. **独立于空输出计数**：协议异常检查置于空输出连续计数**之前**并 `continue`——不计入 `_empty_retries`（语义不同，不共享计数），也不因 `tool_calls` 非真清零计数（空输出护栏只统计「模型什么都没说」）；不进停滞检测 / `execute_tool_calls`（无实际动作，避免空转）。
3. **复用 `PARSE_FAILED` kind**：不新增 kind——「模型输出协议不合法」语义已覆盖，扩大枚举面收益不匹配（产品导向）。

**取舍理由**：

1. 协议异常默认 CONTINUE 重试，护栏由 `max_iterations` 兜底（罕见场景，且 handler 可 STOP / RAISE 快速终止），不新增独立计数护栏；
2. 不入空输出计数：空输出护栏的 error「连续空输出（N 轮）」对协议异常场景错误信息不准确，且协议异常与空输出应各自独立评估；
3. `has_tools=False` 时模型不可能合法返回 `tool_calls`（请求未注入 tools），判别可靠不依赖字符串耦合。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/domain/reasoning/react.py` | 主循环空输出计数前新增协议异常检查 → `_finalize_protocol_error`；新方法（对齐 `_finalize_llm_failed`，默认 CONTINUE 重试 / STOP 终止 / RAISE 上抛）；`_dispatch` docstring 12→13 处；execute docstring 步骤 8 补协议异常分支 | `tests/unit/test_react_strategy.py` 新增 5 例 |
| `app/domain/reasoning/react.py` | 协议异常条件扩展 `or not has_tools`（覆盖「要调工具但系统无工具」）+ 注释 / execute docstring 步骤 8 同步 | `tests/unit/test_react_strategy.py` 新增 1 例（无工具 + 非空 tool_calls 短路） |

---

## 验证

- **测试驱动**（6 例）：
  - 默认 CONTINUE 重试 → 下一轮正常结束（LLM calls==2，不进空转，不 yield「检测到 0 个工具调用」）；
  - 连续 3 轮协议异常 → `max_iterations` 兜底（**非** EMPTY_OUTPUT 终止，验证不入空输出计数）；
  - PARSE_FAILED→STOP 终止 / →RAISE 抛 `AgentRunError(PARSE_FAILED)`；
  - 无工具 + 空 tool_calls 识别为协议异常（事件可区分，不落入空输出分支）；
  - **无工具 + 非空 tool_calls 短路**（事件含「协议异常」、不含「LLM 未生成有效输出」，无工具执行记录）。
- `tests/unit/test_react_strategy.py` 70 passed；`uv run pytest` 全量通过；`verify_alignment` 通过。

---

## 教训沉淀

- **协议一致性校验要看全维度**：`tool_calls` 信号不仅要与「数据非空」一致，还要与「工具可用性（`has_tools`）」一致——任何一侧缺失都是协议异常，不能只校验数据一侧；
- **异常分类以「语义」而非「表面信号」为准**：finish_reason 相同但数据为空 / 无工具可调 ≠ 空输出（意图不同），共享计数器会让错误信息失真、护栏误触发；
- **护栏失效的隐蔽性**：误入空输出分支时，非空 `tool_calls` 恰好清零重试计数——护栏「以为模型有产出」而失效，比直接没有护栏更隐蔽（行为看似正常直到 max_iterations）。
