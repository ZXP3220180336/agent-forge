# REASON-007 LLM 失败重试无独立上限：handler CONTINUE 可无限重试烧钱

> **状态**：✅ 已修复（2026-08-31）
> **优先级**：P3（仅 handler 显式 CONTINUE 时触发；max_iterations 兜底 + 集成层熔断三重防线在，但护栏不对称）
> **来源**：2026-08-31 React 策略 LLM 失败处理检查
> **涉及模块**：`app/domain/reasoning/react.py` · `app/domain/agent/base.py` · `executor.py` · `app/config/settings.py` · `app/container.py` · `app/api/routes/chat.py`
> **关联文档**：[react.md](../../../docs/domain_doc/reasoning_doc/react.md) · [config.md](../../../docs/config_doc/config.md) · [executor.md](../../../docs/domain_doc/agent_doc/executor.md) · [agent.md](../../../docs/domain_doc/agent_doc/agent.md)

---

## 问题描述

### 现象

`LLM_FAILED→CONTINUE` 重试无独立上限：用户注册 CONTINUE handler 时，LLM 持续失败（key 失效 / 余额不足 / 服务端 5xx）则每轮重试到 `max_iterations` 兜底——**护栏不对称**：

- 空输出有 `max_empty_retries` 独立上限 + 硬终止；
- 循环停滞有 `max_same_action_turns` 独立上限 + 硬终止；
- **LLM 失败重试无独立上限**，仅依赖全局 `max_iterations` 兜底。

### 影响

- handler 配置失误（误注册 LLM_FAILED→CONTINUE）或 LLM 持续失败时，每轮重试都经集成层再重试一次——烧满 `max_iterations` 轮 LLM 调用（产品上长任务费用失控风险）；
- 与其他「重试型」护栏（空输出 / 停滞）不对称，语义不统一。

### 根因

`_finalize_llm_failed` 的 CONTINUE 分支无连续失败计数——默认行为（STOP 短路）不暴露问题，但 handler 扩展点可绕过护栏。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| OpenAI Agent SDK | 失败重试 / `max_turns` 均有明确上限，防止持续失败无限重试烧 token |
| SMOLagents | 限流错误重试有次数上限（60s×2^n，仅限流） |
| 本项目既有护栏 | 空输出 `max_empty_retries`（默认 2，第 3 次硬终止）、停滞 `max_same_action_turns`——独立计数 + 硬终止（handler CONTINUE 忽略） |

**核心**：重试型错误应具备独立连续失败上限（含 handler 决策的覆盖），而非仅靠全局轮数兜底。

---

## 修复方案（含决策取舍）

**决策**：

1. **独立重试上限**：`max_llm_fail_retries: int = 2`——LLM 失败最多重试 N 次，第 N+1 次仍失败则硬终止（0=首次失败即终止），**完全对齐空输出护栏模式**。
2. **计数语义**：`self._llm_fail_retries`（execute 每次独立）：失败轮 +1、成功轮（error 为 None）清零、取消不参与计数（取消在 +1 前 return）。
3. **硬终止**：`_finalize_llm_failed` 在 CONTINUE 分支前检查 `self._llm_fail_retries > max_llm_fail_retries` → 先 `_dispatch`（handler 可 RAISE）→ 组装 outcome（error「连续 LLM 调用失败（N 轮）」）——即使 handler 返回 CONTINUE 也终止（对齐 `_handle_empty_output` 硬终止结构）。
4. **完整配置链路**：`settings.agent_max_llm_fail_retries` → `container.agent_params` → `AgentContext.max_llm_fail_retries` → executor 透传 → `execute(max_llm_fail_retries=...)`（对齐 max_empty_retries）。

**取舍理由**：

1. 默认值 2 与空输出一致，语义统一（「最多重试 2 次，第 3 次终止」）；
2. 默认行为（STOP 短路）零变化——护栏只在 handler 显式 CONTINUE 时生效，向后兼容；
3. 成功轮清零：连续失败语义（中间恢复则重置），非累计总数——对齐空输出「有产出清零」。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/config/settings.py` | `agent_max_llm_fail_retries: int = 2` + validator（非负） | `tests/unit/test_container.py` agent_params 断言同步 |
| `app/domain/agent/base.py` | `AgentContext.max_llm_fail_retries: int = 2` | — |
| `app/container.py` | agent_params dict 加 `max_llm_fail_retries` | test_container 覆盖 |
| `app/api/routes/chat.py` | AgentContext 构造加 `max_llm_fail_retries` | chat_flow / e2e mock 补键 |
| `app/domain/agent/executor.py` | `_strategy_cycle` 透传 | `tests/unit/test_agent.py` 新增 1 例（透传） |
| `app/domain/reasoning/react.py` | `execute()` 加 `max_llm_fail_retries=2`；`_llm_fail_retries` 计数（失败 +1 / 成功清零）；`_finalize_llm_failed` 硬终止分支（对齐 `_handle_empty_output`） | `tests/unit/test_react_strategy.py` 新增 5 例 |

---

## 验证

- **测试驱动**（react 5 例 + agent 1 例）：
  - 持续失败 + CONTINUE → 第 3 次硬终止（error「连续 LLM 调用失败（3 轮）」）；
  - 失败→工具成功→再失败不累计硬终止（成功轮清零，`max_llm_fail_retries=1` 下两次 error 各计数 1）；
  - 达上限 handler RAISE → 抛 `AgentRunError(LLM_FAILED)`；
  - `max_llm_fail_retries=0` → 首次失败即终止；默认 STOP 行为零变化（首轮短路）。
  - agent 透传：AgentContext 设 0 → 首次失败终止（透传失效则默认 2 第 3 次才终止）。
- `tests/unit/test_react_strategy.py` 75 passed；`tests/unit/test_agent.py` 9 passed；`uv run pytest` 全量通过；`verify_alignment` 通过。

---

## 教训沉淀

- **护栏应对称**：同类「重试型」错误（空输出 / LLM 失败 / 停滞）应有同构的独立上限 + 硬终止，避免「一种有护栏、一种裸奔」的不对称——默认行为不暴露问题，扩展点（handler）可绕过；
- **扩展点护栏在扩展点侧生效**：LLM 失败默认 STOP 不暴露上限问题，但 handler 显式 CONTINUE 是开放决策——护栏必须在 handler 决策分支内生效（硬终止即使 CONTINUE 也忽略），而非只保护默认路径。
