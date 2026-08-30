# REASON-003 取消信号语义错位 + 未接线：cancel_event 链路贯通 + /chat/stop 真实实现

> **状态**：✅ 已修复（2026-08-30）
> **优先级**：P1（用户停止功能实际不存在；取消信号被误判为 LLM 失败可能重试）
> **来源**：2026-08-30 代码审查发现（Agent 运行异常处理全面性评审）
> **涉及模块**：`app/domain/reasoning/react.py` · `app/domain/agent/executor.py` · `app/application/task/task_service.py` · `app/api/routes/chat.py`
> **关联文档**：[react.md](../../../docs/domain_doc/reasoning_doc/react.md) · [task.md](../../../docs/application_doc/task_doc/task.md) · [routes.md](../../../docs/api_doc/routes_doc/routes.md)

---

## 问题描述

### 现象

1. **语义错位**：LLMService 的 `cancel_event`（优雅取消）置位时，整流层置 `result.error = "用户取消"` → ReAct 主循环走 `LLM_FAILED` 分发。若用户注册 `LLM_FAILED→CONTINUE`，**取消后会被重试**（取消是用户意图，重试无意义）。`CANCELLED` kind 只被 BaseAgent.run 的 `asyncio.CancelledError`（任务硬取消）触发，优雅取消信号不归它。
2. **未接线**：`cancel_event` 无任何调用方传入（`ReActStrategy.execute` 不收、`/chat/stop` 是 stub）——「用户停止」功能实际不存在。

### 影响

- 用户无法停止运行中的 Agent（长任务只能等它跑完或超时）——产品上良率排查中误触发长任务无法干预；
- 一旦 cancel_event 被未来接线，取消会被当作 LLM 失败处理（handler 可 CONTINUE 重试）——语义错误。

### 根因

- ReAct 策略层不识别「取消」信号：LLM 层返回的 error="用户取消" 与 LLM 失败混同，无独立语义分支；
- 链路断裂：cancel_event 从 HTTP 层到策略层无通路（ReAct 不收、Agent 不收、/chat/stop 不产生）。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| OpenAI Agent SDK `cancel(mode="after_turn")` | 优雅取消：在轮次边界停止（内部 asyncio 事件驱动），非硬中断；取消后完整消费事件流（清理收尾） |
| FastAPI SSE | 客户端断开 → 生成器 aclose；服务端主动停止需事件/信号机制 |

**核心**：优雅取消（after_turn / cancel_event）是标准模式——轮次边界停止、不硬中断、资源干净回收。本项目 `cancel_event`（asyncio.Event）正是同模式（整流层已在 chunk 边界检查、工具执行中不硬中断），**复用即可**，缺的是「ReAct 识别取消 → CANCELLED」和「HTTP 停止接线」。

---

## 修复方案（含决策取舍）

**决策**：

1. **ReAct 识别取消**：`ReActStrategy.execute` 加 `cancel_event: asyncio.Event | None = None`，传给 `self._llm.async_generate(cancel_event=...)`。主循环两处识别取消 → 新 `_finalize_cancelled`（复用 `_finalize_terminal(CANCELLED, ...)`，保留部分进度）：
   - **循环顶部**（每轮 LLM 调用前）：`cancel_event.is_set()` → 取消（快速响应，即使不在 LLM 调用中，如工具执行后）
   - **error 分支**（`stream_result.error` 非空时）：`cancel_event.is_set()` → CANCELLED（**不重试**），否则 LLM_FAILED
2. **透传链路**：`ReActAgent.__init__` 加 `cancel_event`（对齐 cost_limiter 构造注入）→ `_strategy_cycle` 传 `execute(cancel_event=...)`。每次请求新建 agent 实例，cancel_event 随请求独立。
3. **/chat/stop 接线**：`TaskService` 扩展会话级取消事件注册表（单例共享）：`create_cancel_event` / `get_cancel_event` / `clear_cancel_event` / `cancel_session`。`app/api/routes/chat.py` send 创建 + `ReActAgent(cancel_event=...)` + `finally` 清理；stop_chat 调 `cancel_session` 真实置位。
4. **不引入 StreamResult 字段**：ReAct 持有 cancel_event，直接 `is_set()` 判别，无需改 LLM 层。
5. **优雅取消语义**：对齐 after_turn——LLM 调用中整流层 chunk 边界中断（已有）；工具执行中等返回后主循环顶部检查取消；取消后 generator 正常走完（yield 取消 info + done，finally 清理注册表）。

**取舍理由**：

1. **语义分离**：取消（CANCELLED，不重试）与 LLM 失败（LLM_FAILED，可重试）是不同语义，靠 cancel_event.is_set() 判别不依赖 error 文本（避免字符串耦合）；
2. **硬/软取消并存**：`asyncio.CancelledError`（任务硬取消 → BaseAgent.run CANCELLED）与 `cancel_event`（优雅取消 → ReAct 层 CANCELLED）是独立路径，互不干扰；
3. **会话级注册表**：一个会话一个运行任务（chat 单 SSE 流），session_id 为 key 足够；多任务并发由 TaskService 信号量限制。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/domain/reasoning/react.py` | `execute()` 加 `cancel_event` 参数（传给 LLM 层）；主循环顶部 + error 分支取消识别 → `_finalize_cancelled`；`_dispatch` docstring 11→12 处 | `tests/unit/test_react_strategy.py` 新增 3 例：置位取消 / LLM error+置位 → CANCELLED（非 LLM_FAILED）/ 未置位正常 |
| `app/domain/agent/executor.py` | `ReActAgent.__init__` 加 `cancel_event` → `_strategy_cycle` 传 `execute(cancel_event=...)` | 经 chat_flow 间接覆盖 |
| `app/application/task/task_service.py` | 会话级取消事件注册表（create/get/clear/cancel_session） | `tests/unit/test_task_service.py` 新增注册表生命周期测试 |
| `app/api/routes/chat.py` | send 创建 cancel_event + `ReActAgent(cancel_event=...)` + `finally` 清理；stop_chat 调 `cancel_session` 真实置位（返回 `cancelled` 布尔） | `tests/integration/test_chat_flow.py` 新增 stop 取消（置位 → 流带取消事件结束，LLM 未调用，注册表清理） |

---

## 验证

- **测试驱动**：`test_chat_stop_cancels_running_agent` 完整链路（send 注册 → 置位 → Agent 优雅取消 → 流正常结束 → 注册表清理）；
- `tests/unit/test_react_strategy.py` 60 passed；`uv run pytest` 全量 681 passed；`verify_alignment` 通过；
- **教训**：断言子串时注意「已被取消」vs「已取消」——中间隔字不是子串，测试一度误断言失败（非代码缺陷）。

---

## 教训沉淀

- **取消是独立语义，不混入失败分类**：用户取消（CANCELLED）与 LLM 失败（LLM_FAILED）必须分离——否则 handler 的 CONTINUE（重试）会作用于取消，产生「用户点停止却在重试」的荒谬行为；
- **产品能力 vs 异常处理的边界**：取消信号「语义正确」与「实际接线」是两层——即使没有真实调用方，语义也应正确（为接线打基础）；接线后做端到端验证（HTTP → 事件 → Agent → 清理）；
- **优雅取消优于硬中断**：asyncio.CancelledError 硬中断可能留下未清理资源（reservation / 工具副作用）；cancel_event 在安全点（chunk 边界 / 轮次边界）响应，资源干净回收。
