> 历史设计记录，不是当前执行计划；有效待办统一见 [docs/todo.md](../todo.md)。后续已完成与替代项按正式模块、ADR/Issue核对。

# 领域层推理决策架构完整建设（Phase C 前置）

> **文档性质**：⚠️ **临时计划文件**（源自 2026-08-27 会话记录，用户要求保留为独立文件，是否删除由用户决定）。
> **创建日期**：2026-08-27（领域层建设起始，Slice 0 当日完成）
> **当前进度**（2026-09-05 视图）：Slice 0 ✅ · Slice 4 ✅（两者均超计划：stream_mode 双通道 / cancel·超时护栏 / cost 全阶段 / 收尾契约化）· Slice 3 ✅（2026-09-05 落地为两层结构：`reasoning/planner.py`（PlannerStrategy）+ `agent/planner.py`（PlannerAgent 桥接），每步复用 ReActStrategy.execute；见 [planner.md](../domain_doc/reasoning_doc/planner.md) / [ADR](../../adr/domain/reasoning/2026-09-05-planner-strategy.md)）· Slice 1 🔶（reflection 模板 + planning 三模板 + manager builder + test_prompts ✅；run() system 注入仍未做）· Slice 6 🔶（随已实现模块覆盖）· Slice 2/5 ⬜ 待做
> **一致性说明**：本文为计划原文，部分描述与当前代码有出入时**以代码为准**（如 Slice 4 实际落为 `agent/reflection.py`，原计划写 `agent/reasoning.py`）。简略版进度见 [docs/todo.md](../todo.md)。

---

## Context（为什么做）

- Phase C（应用与编排层）前，先把领域层四模块建设完整、跑通单 Agent 任务，再上应用层多任务编排（Orchestrator 依赖 PlannerAgent 拆分）。
- 用户目标：工业级推理决策基座 = 策略实现（reasoning/）+ prompt 管理（prompts/）+ 记忆管理（memory/）+ agent 编排（agent/）。
- 现状：agent/base.py + executor.py（ReActAgent）✅；planner.py / agent/reasoning.py / reasoning/ 三文件 / memory/ 五文件全部 0 字节空壳；PromptManager + SYSTEM_PROMPT/PLANNING_PROMPT 零引用（C9 死代码）。
- 产品锚点：PlannerAgent.plan() = 主 Agent 拆分原语（Phase C 主链路第一步）；ReActStrategy = 子 Agent 排查引擎复用；Reflection = 证据链语义自查（防伪证据）；Memory = 历史排查经验沉淀的领域前置。

## 已确认决策

- 长期记忆：端口 + 骨架，向量检索实现留 Phase D（不 mock 向量库，VectorStorePort 依赖倒置）。
- 策略库：ReAct 抽离 + Reflection 实现；CoT 预留（ADR 记录升级路径，不写实现）。
- 边界定案（工业实证）：PlannerAgent 整体归 agent/（planning 是编排职责，非原子算法）。

## 四模块设计

### 1. 策略库 reasoning/（ReAct 抽离 + Reflection）

**reasoning/react.py** — ReActStrategy 纯算法类，构造注入 llm: LLMGateway + tools: ToolGateway，不 import agent/ 任何模块（依赖方向：reasoning→ports+shared，被 agent 编排调用）。参数收标量（max_iterations/temperature/max_tokens）规避 AgentContext 依赖。

- `@dataclass ReActOutcome`：content / reasoning / tool_calls / iterations / total_tokens / usage / error / success
- `async def execute(...)`：ReAct 主循环（finish_reason 分支 + error 短路 + 空输出重试 + 迭代兜底），yield SSE 事件，结果写 self.outcome
- `async def execute_tool_calls(...)`：工具并行执行原语（gather 并行保序），供 PlannerAgent 执行阶段 / ReflectionAgent 收集阶段复用

**agent/executor.py** — ReActAgent 变薄桥接：构造建 ReActStrategy；_strategy_cycle 委托 execute + _map_outcome；保留 _execute_tool_calls 转发方法（test_agent.py 三处直接调用，零改动）。对外 API 不变，chat.py:82 / task_service / agent/init 均不动。

**reasoning/reflection.py** — ReflectionStrategy（内部持 ReActStrategy）+ schema：

- REFLECTION_SCHEMA（报告初稿契约）+ CRITIQUE_SCHEMA（{ok: bool, issues: []}）
- ReflectionOutcome：final / draft / critique / degraded
- execute(...)：阶段1 收集（ReAct 跑地面真值）→ 阶段2 初稿（generate_structured）→ 阶段3 自查（ok=true 采用初稿，None 也采用初稿——自查失败不丢结果）→ 阶段4 修正（issues 回喂重写，None 用初稿）

**reasoning/chain_of_thought.py**：保持空壳，CoT 预留（ADR 记录）。

### 2. Prompt 管理完整化

- **prompts/templates/planning.py**：PLANNING_PROMPT 从 draft 完整化——规划职责（拆分自包含步骤 + 显式 depends_on）、执行约束（每步只做当前步骤）、re-plan 语义、汇总职责。含 {tool_descriptions} 占位。
- **prompts/templates/reflection.py**（新增）：REFLECTION_SYSTEM（报告起草，禁止编造）+ SELF_CHECK_PROMPT（{checklist} 占位，自查清单穷举维度）。
- **prompts/manager.py**：新增 build_planning_prompt / build_reflection_prompt / build_self_check_prompt 三个 staticmethod。
- **agent/base.py**：run() 顶部若无 system 角色消息，注入 PromptManager.build_system_prompt()（解决 SYSTEM_PROMPT 零引用）。

### 3. 记忆模块（端口 + 骨架）

- **ports/vector_store_port.py**（新增）：VectorStorePort(Protocol)（upsert / search / delete）+ VectorHit。登记 ports/**init**.py。
- **memory/base.py**：MemoryItem + MemoryBackend(ABC)（add/get/remove/clear）。
- **memory/working.py**：WorkingMemory（dict 承载 + snapshot）。
- **memory/short_term.py**：ShortTermMemory（有序 dict，max_size 满则逐出最旧 + list_recent）。
- **memory/long_term.py**：LongTermMemory，构造注入 vector_store: VectorStorePort | None + embedder: EmbeddingPort | None；任一端口 None → no-op（骨架，非 mock，同 SessionManager redis=None 降级范式）。
- **memory/memory_service.py**：MemoryService 聚合三层（working/short_term/long_term/enabled），enabled=False 全惰性。签名只用原始类型，不 import agent/。
- **agent/base.py**：BaseAgent.**init**(llm, tools, memory: MemoryService | None = None)（向后兼容）；run() 结束记录 _memory.record_task(...)，异常吞掉不阻断任务。

### 4. Agent 编排（PlannerAgent + ReflectionAgent）

**agent/planner.py** — PlannerAgent(BaseAgent)。模块常量：PLAN_STEP_SCHEMA / PLAN_SCHEMA（goal + steps[id/description/depends_on]）/ REPLAN_SCHEMA / RESULT_SCHEMA（summary/conclusions/confidence/next_steps），全 object 节点 additionalProperties:false，随模块发布（Phase C Orchestrator import）。

- `async def plan(user_input, messages, context) -> dict | None`：结构化拆分原语（无 SSE），build_planning_prompt() + PLAN_SCHEMA + generate_structured(model_key=ctx.metadata.get("model_key","fast"))；捕获 StructuredRefusalError/StructuredToolCallError → None。
- _strategy_cycle：规划（plan None → 降级 ReActStrategy 兜底）→ 执行（depends_on ⊆ completed 拓扑出队，每步复用 execute_tool_calls；工具失败 → replan 限 2 次，新步骤 next_id 重编号防依赖错位）→ 汇总（generate_structured(RESULT_SCHEMA)，None → 纯文本拼装）。

**agent/reasoning.py** — ReflectionAgent(BaseAgent) 桥接 ReflectionStrategy。

### 5. 装配接线

- **container.py**：**init** 加 memory_service = None；initialize() 组 WorkingMemory + ShortTermMemory + LongTermMemory(vector_store=None, embedder=embedding_service) + MemoryService(enabled=settings.memory_enabled)。
- **api/deps.py**：get_memory_service() provider（可返回 None，不 raise）。
- **api/routes/chat.py**：ReActAgent(llm, tools, memory=memory_service)（memory_enabled 默认 False → 惰性零行为变化）。
- **application/task/task_service.py**：agent: ReActAgent 类型标注 → BaseAgent。
- **domain/agent/**init**.py**：重导出 PlannerAgent / ReflectionAgent。

## 垂直切片文件改动清单

| Slice | 内容 | 关键文件 | 进度 |
| --- | --- | --- | --- |
| 0 | ReAct 抽离 | reasoning/react.py + test_react_strategy.py；executor.py、reasoning/init.py | ✅ 已完成（c6d8b9e；超计划：错误分发 13 kind + 护栏全 + stream_mode 双通道 + 收尾契约化） |
| 1 | Prompts 完整化 | planning.py、manager.py、agent/base.py；templates/reflection.py + test_prompts.py | 🔶 部分：templates/reflection.py ✅；planning.py 完整化 / build_planning_prompt / run() system 注入 / test_prompts ⬜ 待做 |
| 2 | Memory 基座 | ports/vector_store_port.py + memory/ 六文件；test_memory.py；ports/init.py | ⬜ 待做（vector_store_port 未建、memory/ 全空壳；骨架范围，向量库实现留 Phase D） |
| 3 | PlannerAgent | reasoning/planner.py + agent/planner.py + test_planner.py + test_planner_agent.py；reasoning/init.py、agent/init.py | ✅ 已完成（2026-09-05；两层结构：策略在 reasoning/planner.py、桥接在 agent/planner.py；每步复用 ReActStrategy.execute 而非裸 execute_tool_calls；超计划：replan 循环 + PLAN_FAILED 分发 + usage/done 口径 + cancel/成本护栏全阶段） |
| 4 | ReflectionAgent | reasoning/reflection.py + agent/reflection.py + test_reflection.py | ✅ 已完成（2026-08-31，落为 agent/reflection.py；超计划：P3 cancel/超时护栏 + P4 explicit_abstention + usage/cost 全阶段 + 收尾契约化） |
| 5 | 装配接线 | container.py、deps.py、chat.py、task_service.py、agent/base.py；test_container.py | ⬜ 待做（随 Slice 2/3；Reflection 产品接线按 ADR 决策留 RCA 产物链） |
| 6 | 文档 + ADR + 对齐 | ALIGNMENT / architecture / 各模块文档 / todo + 4 ADR；verify_alignment | 🔶 部分：React/Reflection 模块文档 + ADR 齐；Planner/Memory 相关文档与 ADR 待随实现 |

## 测试计划（全手写假对象 + monkeypatch，不用 AsyncMock）

- test_react_strategy.py：工具循环 stop / error 短路 / 空输出重试 / max_iterations 兜底 / execute_tool_calls 并行保序 + 并发 ✅
- test_prompts.py：planning/self_check builder 渲染；关键约束文本断言；默认 system 注入
- test_memory.py（填空文件）：Working / ShortTerm 窗口逐出 / LongTerm 委托 + None no-op / MemoryService 惰性
- test_planner_agent.py：_FakeStructuredLLM（async_generate + generate_structured script）。plan 成功 / 拒答→None / 依赖执行 / replan / 降级
- test_reflection.py：收集→初稿→自查→修正全路径 + 各降级
- test_container.py（改）：memory_service 惰性装配

## 文档同步 + ADR

- 文档：ALIGNMENT / architecture / agent.md / reasoning.md / memory.md / prompts.md / domain README / todo.md
- ADR（4 个，adr/domain/）：react-strategy-extraction ✅（2026-08-27 已建）/ structured-degradation-contract / memory-layered-contract / cot-upgrade-path

## 验证

```bash
uv run pytest tests/unit/test_react_strategy.py tests/unit/test_prompts.py tests/unit/test_memory.py tests/unit/test_planner_agent.py tests/unit/test_reflection.py
uv run pytest                                    # 全量回归
uv run python -m scripts.verify_alignment        # 文档对齐校验
```

## 关键设计决策摘要

1. ReActStrategy 收标量参数 → 遵守 reasoning→ports+shared 依赖方向，策略可独立测试。
2. test_agent.py 零改动依赖 ReActAgent 保留_execute_tool_calls 转发方法。
3. PlannerAgent 执行阶段复用 execute_tool_calls，不复用完整 ReAct 循环——执行被计划约束是 plan-then-execute 灵魂。
4. generate_structured 返回 None 降级：plan None→ReAct 兜底；summarize None→纯文本；critique None→采用初稿。
5. 记忆模块不 mock 向量库：LongTermMemory 依赖注入端口，None 时 no-op，Phase D 接 Milvus。
6. 记忆避免孤儿：BaseAgent 完成钩子 + chat.py 接线（memory_enabled 默认 False 惰性零行为变化）。
