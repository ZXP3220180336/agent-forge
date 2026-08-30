# 错误处理 handler 自身异常无防御：dispatch 逃逸破坏主循环

> **状态**：✅ 已修复（2026-08-30）
> **优先级**：P1（扩展点缺陷可破坏核心循环：UNKNOWN handler 异常会让主循环兜底失效，异常逃逸出 execute()；其余 handler 异常掩盖被分发的原始错误）
> **来源**：2026-08-30 代码审查发现（Agent 运行异常处理全面性评审，问题 4）
> **涉及模块**：`app/shared/error_handling.py`（`ErrorHandlerRegistry.dispatch`）· 经 `app/domain/reasoning/react.py` 触达
> **关联文档**：[react.md](../../../docs/domain_doc/reasoning_doc/react.md) · [error_handling.md](../../../docs/shared_doc/error_handling.md)

---

## 问题描述

### 现象

`_dispatch` → `ErrorHandlerRegistry.dispatch` 调用的用户注册 handler 自身抛出异常时无防御：

1. **普通 kind 的 handler 异常**（如 `LLM_FAILED` handler 抛 `RuntimeError`）→ 异常沿 `_finalize_llm_failed` → 主循环 `except Exception` → UNKNOWN 分发。**LLM 失败的原始原因被 handler 的 bug 掩盖**，最终报「Agent 运行异常: <handler 异常文本>」——错误信息失真。
2. **UNKNOWN handler 自身异常** → `_finalize_unknown` 在主循环 `except Exception` 块内再抛 → 异常从 except 块逃逸出 `execute()` 生成器 → UNKNOWN 兜底失效，调用方（`BaseAgent.run`）收到非预期异常。
3. 可恢复 kind（如 `TOOL_FAILED`）handler 异常 → 主循环把 handler 异常当未捕获异常走 UNKNOWN，**工具失败本应回喂继续，却整轮失败**——可恢复语义被破坏。

### 影响

- handler 是调用方扩展点（按 kind 覆盖策略），其实现缺陷不应破坏核心 ReAct 循环；
- 原始错误（LLM 失败 / 工具失败原因）被 handler 异常掩盖，根因报告失真；
- UNKNOWN 兜底（最后防线）可被 handler 异常穿透。

### 根因

`ErrorHandlerRegistry.dispatch` 对用户 handler 的调用无防御——handler 违反「返回 `AgentErrorAction`」契约时，异常直接传播，没有降级为默认行为。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| OpenAI Agent SDK / LangChain 回调 | 回调（callback/handler）异常默认记录并继续主流程（不因扩展点失败中断 Agent 运行） |
| 插件/扩展点防御性原则 | 核心系统对扩展点异常隔离：记录 + 降级默认，扩展点缺陷可观测且不破坏核心链路 |

**核心**：handler 是扩展点（可恢复/终结策略覆盖），其异常应按「默认 action 降级」处理——与未注册 handler 行为一致，同时日志记录缺陷（可观测），不掩盖被分发的原始错误。

---

## 修复方案（含决策取舍）

**决策**：

1. **防御位置**：`ErrorHandlerRegistry.dispatch`（共享内核横切能力）——错误分发是共享内核，「handler 异常防御」随之归一处，所有调用方（ReAct / Planner 等）受益（「一个事实一个家」）。
2. **降级语义**：`except Exception` 捕获 handler 异常 → `logger.warning`（含 traceback）→ 返回 `_DEFAULT_ACTIONS[kind]`（= 未注册 handler 的现有行为，`CONTINUE`/`STOP` 各按 kind 语义走）。
3. **不捕获 `BaseException`**：`asyncio.CancelledError`（Python 3.8+ 是 `BaseException`）/ `KeyboardInterrupt` / `SystemExit` 不被吞——保持取消语义（对齐 `asyncio.CancelledError` 永不吞约定）。
4. **logger 依赖**：shared 层「无依赖」约束（被所有层引用、自身不依赖 platform）——不用 `get_logger` 包装，直接用标准库 `logging.getLogger("app.shared.error_handling")`（命名空间对齐 `app.*`，可被 `setup_logging` 的 handler 捕获）。
5. **记录级别**：`warning` + `exc_info=True`（对齐项目「钩子执行失败」「工具加载失败」的 warning 惯例；完整 traceback 便于定位 handler 缺陷）。

**取舍理由**：

1. `AgentRunError` 是 `Exception` 子类，若 handler 主动 raise 视为违反契约，统一降级（handler 应 `return AgentErrorAction.RAISE` 而非自行 raise）；`_dispatch` 内 RAISE 决策构造的 `AgentRunError` 在 `registry.dispatch` 返回之后，不受影响；
2. handler 异常不转 SSE 事件流：registry 是纯领域内核无事件概念，且 handler 异常属开发期缺陷（日志定位即可），产品主链路不展示内部扩展点错误。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/shared/error_handling.py` | `dispatch` 对 handler 调用包 `except Exception`（不捕获 `BaseException`）→ 记录 warning + 降级 `_DEFAULT_ACTIONS[kind]`；模块级 `_logger`（标准库 logging，命名空间 `app.shared.error_handling`） | `tests/unit/test_error_handling.py` 新增 2 例：handler 异常降级默认 action（终结性 STOP / 可恢复 CONTINUE）/ `CancelledError` 不被吞 |
| `tests/unit/test_react_strategy.py` | ReAct 视角回归：LLM_FAILED handler 异常默认 STOP / TOOL_FAILED handler 异常默认 CONTINUE / UNKNOWN handler 异常兜底不崩 | 新增 3 例 |

---

## 验证

- **测试驱动**（5 例）：
  - registry 层：handler 抛异常 → 返回默认 action（`LLM_FAILED`→STOP / `TOOL_FAILED`→CONTINUE），异常不传播；handler 抛 `asyncio.CancelledError` → 向上传播（不被吞）；
  - ReAct 层：LLM_FAILED handler 异常 → 默认 STOP 短路，error 为 LLM 失败原因（**非** handler 异常）；TOOL_FAILED handler 异常 → 默认 CONTINUE 回喂，下一轮正常结束；UNKNOWN handler 异常 → 主循环兜底不崩（默认 STOP 保留部分进度）。
- `tests/unit/test_error_handling.py` 5 passed；`tests/unit/test_react_strategy.py` 68 passed；`uv run pytest` 全量 691 passed；`verify_alignment` 通过。

---

## 教训沉淀

- **扩展点异常必须隔离**：用户 handler / 回调 / 插件是外部扩展点，其缺陷不应破坏核心链路——捕获 + 记录 + 降级默认，是扩展点防御的通用模式；
- **兜底本身不能被击穿**：UNKNOWN 兜底在主循环 `except Exception` 内执行，若兜底路径（`_dispatch`）也依赖可抛异常的扩展点，兜底会失效——兜底路径必须对扩展点防御；
- **`except Exception` 与 `BaseException` 的边界**：防御性捕获用 `except Exception` 而非 `except BaseException`——`asyncio.CancelledError` 等取消/退出信号必须穿透（对齐「CancelledError 永不吞」）。
