# Agent 运行轨迹持久化（Trace Persistence）—— 横切能力

> 日期：2026-08-31 ｜ 层级：domain（端口）+ application + infrastructure（横跨三层，应用层编排）
> 状态：🔶 已决策，待实施（ADR 末尾附实施计划）

---

## Context

### 问题

Agent 循环中，模型每轮输出的**回答内容（content）/ 思考内容（reasoning_content）/ 工具调用（tool_calls）/ 工具结果（tool_result）**当前仅经 SSE 事件流实时流出，持久化缺失导致**完整推理链事后不可回溯**：

| 数据 | 实时（SSE 事件流） | 事后（持久化） |
| --- | --- | --- |
| 每轮 reasoning（thinking） | ✅ `type=reasoning` 逐 token 流出 | ❌ 会话历史只存**末轮** reasoning_content |
| 每轮 content | ✅ `type=message` 逐 token 流出 | ⚠️ 只存最终 assistant 消息 content |
| tool_calls / tool_result | ✅ `type=tool_call` / `tool_result` 流出 | ❌ 仅在 `AgentResult.tool_calls`（运行期内存，实例销毁即失） |
| usage / 时间戳 | ⚠️ 部分在 done 事件 | ❌ 不落库 |

**需求**：模型输出的回答 / 思考 / 工具调用 / 调用结果等信息需**可靠持久化存储**，保证信息可追溯（事后可完整回溯「每轮思考 → 工具调用 → 结果」）。

### 定位：追溯链是横切能力，非策略专属

**追溯链与错误处理分发（`ErrorHandlerRegistry`）同构——是横切能力**，所有 Agent 类型（`ReActAgent` / 规划中的 `PlannerAgent` / `ReflectionAgent`）都应接入，而非 ReAct 策略专属：

| 横切能力 | 共享契约 | 注入点（BaseAgent） | 各策略接入点 |
| --- | --- | --- | --- |
| 错误处理分发 | `ErrorHandlerRegistry`（shared） | `BaseAgent.__init__(error_handlers=...)`，run() 分发 | ReActStrategy 构造注入，循环内 `_dispatch` |
| **追溯链（本次）** | `TraceCollectorPort`（ports 层） | `BaseAgent.__init__(trace_collector=...)`，run() 生命周期 + 终结 | ReActStrategy 轮次边界 `on_turn_end` / Planner 阶段边界 |

**工业级印证**：LangSmith / Langfuse 的 tracer 是**框架层横切**（callback handler 挂任何 Agent/Chain/图），不是每个策略单独接；LangChain tracer 挂在 runnable/executor 层；Claude Agent SDK 的 span 包装在 Agent 层——横切接入是工业标准。

### 现状数据流（代码事实）

1. **`_tool_call_records`**（[react.py](../../../app/domain/reasoning/react.py) `ReActStrategy` 内部状态）：内存累积工具执行记录（tool/params/result/success/error/error_code/duration）——等价于 LangChain `intermediate_steps`，是**证据链的内存形态**，但只覆盖「工具执行叶子」，缺每轮 LLM 的 content/reasoning/usage/时间戳；
2. **`ReActOutcome`**：`content`（末轮）/ `reasoning`（末轮）/ `tool_calls`（完整证据链）/ usage——中间轮 reasoning/content 已丢；
3. **SSE 事件流**（[events.py](../../../app/shared/events.py)）：reasoning/message/tool_call/tool_result 结构化 JSON 完整流出，但 [chat.py](../../../app/api/routes/chat.py) 只透传不落库；
4. **持久化现状**：[MessageModel](../../../app/infrastructure/models/database/messages.py)（content + 末轮 reasoning_content + meta 未用）；[tool_log.py](../../../app/infrastructure/models/database/tool_log.py) 空文件（工具日志表未实现）。

### 工业界参照（2026-08-31 调研，5 项目 + OTel）

| 参照 | 轨迹结构 | 捕获方式 | 存储 |
| --- | --- | --- | --- |
| **LangSmith** | trace → run（=OTel span），树状 `parent_run_id`/`dotted_order` | callback handler / `@traceable` / client wrapper，非侵入 | 独立云平台，批量 ingestion |
| **Langfuse** | trace → observation（GENERATION/SPAN/TOOL，`parent_observation_id` 嵌套） | OpenAI client wrapper / callback / OTel 摄取 | 独立平台（可自托管），ClickHouse + Postgres |
| **LangChain AgentExecutor** | `intermediate_steps: List[Tuple[AgentAction, str]]` 扁平数组 | 执行器循环内累积 | 无内置存储，靠上层持久化 |
| **OpenAI Agents SDK** | trace → span（task/agent/turn/generation/function）；`RunState` 可序列化快照 | Runner 内建 tracer + `RunState` | SDK 内置 span，接任意 exporter |
| **Claude Agent SDK** | query() → AGENT 根 span → CHAT_COMPLETION（每轮）→ TOOL 子 span | 生命周期 hook（Pre/PostToolUse）+ span 包装；原生 OTel | SDK 内置，session 持久化由调用方 |
| **OTel GenAI semconv** | span 三类：model / agent(`invoke_agent`) / tool，属性 `gen_ai.*` | OTel 标准化 | 任意 OTLP 后端 |

**工业级共识**：
1. **数据模型 = 树**（trace → turn → tool），比扁平数组强在表达「某轮 LLM → 其 N 个工具」父子关系 + 成本沿树聚合；`usage/cost` 只挂 LLM 节点防重复计数；
2. **捕获点 = callback / wrapper（非侵入，横切）**：外围包装层，不改主循环；必须是**数据最全的点**（同时握有 LLM 输出 + 工具结果 + usage）；
3. **存储 = 独立轨迹表**（`traces`/`observations`），与「对话历史 messages」语义正交（Langfuse 双库同理）；**批量写**（每次执行结束一次写整条 trace，非逐事件）；
4. **模块归属**：LangSmith/Langfuse 是独立系统（外部平台）——引入意味着轨迹数据出应用；自建则按「领域模型 + 端口 → 应用编排 → 基础设施存储」三层。

### 本项目约束

- `ReActStrategy` 明确「只依赖 ports + shared + 标准库」——轨迹捕获不能直接依赖 DB/应用层；**横切入口应上移到 `BaseAgent`（所有 Agent 统一接入），策略层只做数据最全点的捕获回调**；
- 「一个事实一个家」：`_tool_call_records` 是运行态累积器（domain 内部状态），持久化应经端口解耦，不让 reasoning 层直接依赖存储；
- 产品导向：证据链根因报告是产品主链路——中间轮工具失败原因（`error_code`）已在 `_tool_call_records`，只差持久化；
- 当前单用户本地场景：独立可观测性平台（LangSmith/Langfuse）是过度建设，按「不做或预留」降级。

---

## Decision

### 1. 归属（横切三层，BaseAgent 统一接入）

| 层 | 模块 | 职责 |
| --- | --- | --- |
| 领域层（端口层） | `app/domain/ports/trace.py`（新） | 数据模型 `AgentStep` / `AgentTrace`（纯 dataclass）+ `TraceCollectorPort`（`on_turn_end` / `complete`，默认 no-op 实现）——**横切契约**，被 BaseAgent / 所有策略依赖；`ports/__init__.py` 登记 |
| 领域层（编排） | `app/domain/agent/base.py` | **`BaseAgent.__init__` 加 `trace_collector: TraceCollectorPort \| None = None`**（横切入口，所有 Agent 统一）；`run()` 终结时调 `complete(input/output/status/total_usage)`；子类 `_strategy_cycle` 透传给策略 |
| 领域层（策略） | `app/domain/reasoning/react.py`（及未来 Planner/Reflection） | `execute()` 收 `trace_collector`（默认 None），**轮次/阶段边界构造 `AgentStep` → `on_turn_end`**（数据最全点） |
| 应用层 | `app/application/trace/`（新） | `TraceCollector`（实现 `TraceCollectorPort`：累积 steps → `complete` 组装 `AgentTrace` + 经 repository 落库） |
| 基础设施层 | `app/infrastructure/models/database/trace.py`（新）+ `app/infrastructure/trace_repository.py`（新） | `TraceModel` / `TraceStepModel`（SQLAlchemy）+ 批量写实现 `TraceRepositoryPort` |

**归属原则**：**横切入口在 `BaseAgent`（对齐 `ErrorHandlerRegistry` 横切注入 BaseAgent）**——任何新 Agent 类型继承 BaseAgent 即自动接入追溯链，无需各策略自行接线；策略层只负责「数据最全点」的 step 捕获回调；写入归应用编排，存储归基础设施。

### 2. 数据模型（泛化 step，非 ReAct 专属）

```python
@dataclass
class AgentStep:                          # 一次可追溯单元（泛化：llm/tool/plan/reflect/sub_agent）
    type: str                             # llm / tool / plan / reflect / sub_agent
    name: str                             # 阶段/工具名（如 "turn_1" / "search" / "plan" / "collect"）
    content: str                          # 该步回答（中间步可空）
    reasoning: str                        # 该步思考全文（非截断）
    finish_reason: str                    # stop / tool_calls / length / ""
    tool_calls: list[dict]                # 该步工具执行记录（字段对齐 _tool_call_records 现状：
                                          #   tool/params/result/success/error/error_code/duration）
    usage: dict                           # prompt_tokens / completion_tokens / total_tokens
    start_time: float                     # 开始时间
    end_time: float                       # 结束时间（含工具执行）
    status: str                           # success / error / cancelled

@dataclass
class AgentTrace:                         # 一次 Agent 运行（单用户请求，横切——任何 Agent 类型）
    trace_id: str                         # 运行唯一标识
    agent_type: str                       # react / planner / reflection（区分 Agent 类型）
    session_id: SessionId
    user_id: UserId
    input: str                            # 用户原始输入
    output: str                           # 最终回答
    steps: list[AgentStep]                # 每步轨迹（含中间步）
    total_usage: dict                     # 累计 token
    status: str                           # success / failed / cancelled
    created_at: float
```

- `AgentStep.type` 泛化——ReAct 每轮 = `type="llm"`（工具并入 `tool_calls` 字段）；Planner 规划阶段 = `type="plan"`；Reflection 收集阶段 = `type="reflect"`——**任一策略用同一 step 模型产出轨迹**；
- `AgentTrace.agent_type` 区分 Agent 类型——多 Agent 编排（主 Agent 拆分 → 子 Agent 并行）时，每个子 Agent 一条 trace，`agent_type` + `trace_id` 可关联；
- `usage` 挂在每步（LLM 节点），对齐「usage 只挂 LLM 节点」工业级约定；
- `_tool_call_records` 现有字段（tool/params/result/success/error/error_code/duration）**保持**——已是工业级形态，直接并入每步 `tool_calls`；
- 数据模型放端口层（`domain/ports/trace.py`），`BaseAgent` / 策略 import 契约即可。

### 3. 捕获点（两层：BaseAgent 生命周期 + 策略层数据最全点）

**第一层（BaseAgent，横切生命周期）**：
- `BaseAgent.__init__(trace_collector=...)`——统一入口，所有 Agent 接入；
- `run()` 终结时（`_result` 就绪）：组装 `AgentTrace` 的 `input`（user_input）/ `output`（result.content）/ `status`（state）/ `total_usage` → `await trace_collector.complete(...)`；
- 各子类 `_strategy_cycle` 把 `trace_collector` 透传给策略（ReActAgent → ReActStrategy.execute；PlannerAgent → 各阶段）。

**第二层（策略层，数据最全点）**：
- `ReActStrategy.execute()` 收 `trace_collector`（默认 None=零开销），**每轮边界**（LLM 调用后 + `_handle_tool_calls` 后）构造 `AgentStep` → `await trace_collector.on_turn_end(step)`；
- `stream_result` 是**数据最全的点**（完整 content/reasoning/usage/tool_calls，非 SSE 截断后）——这是选定此捕获点的根本原因；
- 未来 Planner / Reflection 在各自阶段边界构造 `AgentStep` → `on_turn_end`（同一端口，无新接线）。

**否决项及理由**：
- ❌ **api 层消费 SSE 重建轨迹**：SSE 事件截断（`tool_result` 200 字符 / reasoning 逐 token），拿不到完整 reasoning 全文、完整 tool result、usage 明细——丢数据；
- ❌ **结果载体补全（ReActOutcome 加完整 turn 数组）**：中间轮数据在策略内需另存，且改策略产出契约（`ReActOutcome` 是桥接契约，扩大它 = 扩大所有消费方契约）；且 ReActOutcome 是 ReAct 专属——不满足横切（Planner/Reflection 无此载体）；
- ❌ **仅策略层注入（不经过 BaseAgent）**：每个新 Agent 需自行接线，非横切——违背「与错误处理分发对齐」的定位；
- ✅ **选 BaseAgent 横切 + 策略层捕获**：与 LangSmith callback handler / Claude SDK hook / ErrorHandlerRegistry 同构——横切入口统一，数据最全点保留。

### 4. 存储（独立表 + 批量写，不扩消息表）

```sql
-- traces：一次 Agent 运行（任何 agent_type）
CREATE TABLE traces (
    id            BIGINT PRIMARY KEY AUTO_INCREMENT,
    trace_id      VARCHAR(36) UNIQUE NOT NULL,        -- 运行唯一标识
    agent_type    VARCHAR(20) NOT NULL,               -- react / planner / reflection
    session_id    VARCHAR(36) NOT NULL,
    user_id       VARCHAR(36) NOT NULL,
    input         TEXT NOT NULL,                      -- 用户输入
    output        TEXT,                               -- 最终回答
    status        VARCHAR(20) NOT NULL,               -- success/failed/cancelled
    total_usage   JSON,
    created_at    DATETIME,
    INDEX (session_id, created_at),
    INDEX (agent_type)
);

-- trace_steps：每步轨迹（泛化 type，含中间步）
CREATE TABLE trace_steps (
    id            BIGINT PRIMARY KEY AUTO_INCREMENT,
    trace_id      VARCHAR(36) NOT NULL,
    type          VARCHAR(20) NOT NULL,               -- llm / tool / plan / reflect / sub_agent
    name          VARCHAR(64),                        -- 阶段/工具名（如 turn_1 / search / plan）
    content       TEXT,                               -- 该步回答（中间步可空）
    reasoning     TEXT,                               -- 该步思考全文
    finish_reason VARCHAR(20),
    tool_calls    JSON,                               -- 该步工具执行记录（含 error/error_code）
    usage         JSON,
    status        VARCHAR(20),
    start_time    FLOAT,
    end_time      FLOAT,
    INDEX (trace_id, type)
);
```

- **独立表而非扩展消息表**：`messages` 存「回显给用户的历史」（content + 末轮 reasoning），`traces` 存「事后追溯的证据链」（每步完整）——语义正交，扩展 `MessageModel.meta` 只能塞扁平结构，无法表达 step 序列 / 按时间聚合（Langfuse 双库同理）；
- **批量写**：每次 Agent 执行结束（`run()` 终结，天然批量点）**一次性写 trace + 全部 steps**——中间步骤内存攒好，落库只发生在终结；逐事件写仅「长任务崩溃恢复」才需要，当前单次执行规模（每轮几 KB）不需要；
- `tool_log.py` / `task.py` 空文件不动（traces 独立表，不混入工具日志）。

### 5. 接口抽象（可降级路径）

- `TraceCollectorPort`（领域端口）：
  - `async on_turn_end(step: AgentStep) -> None`——策略层每轮/阶段调用；
  - `async complete(input: str, output: str, status: str, total_usage: dict, agent_type: str) -> None`——BaseAgent 终结调用；
- 应用层 `TraceCollector(trace_repository)` 实现端口：`on_turn_end` 累积 steps，`complete` 组装 `AgentTrace`（trace_id 生成 + agent_type + 上述字段）+ 经 `TraceRepositoryPort` 落库；
- `TraceRepositoryPort.save_trace(trace: AgentTrace) -> None`——基础设施实现（SQLAlchemy / 未来 Langfuse 换实现），领域层/策略层不动。

### 6. OTel GenAI semconv 对齐字段命名，但不引入 SDK

- **字段语义对齐 semconv**（`gen_ai.usage.input_tokens` / `gen_ai.tool.name` 等命名已是事实标准）：step 字段按此命名，未来接 Langfuse/LangSmith 映射零成本；semconv 明确 content 捕获默认 opt-in（`gen_ai.input.messages` 需显式开启）——契合本项目已有的脱敏/截断实践；
- **不引入 OTel SDK**：semconv 属性名大多 experimental（需 `OTEL_SEMCONV_STABILITY_OPT_IN`）；对单表自建持久化，OTel 的 processor/exporter/上下文传播是重量依赖，纯增复杂度。出现接入 Langfuse/多后端或跨进程 trace 上下文传播的真实需求时再上 OTel。

### 7. 产品导向确认

该能力**直接服务主链路**：`_tool_call_records` 已是证据链的内存形态，只差持久化；且作为**横切能力**，ReAct / Planner / Reflection 各 Agent 统一接入——多 Agent 编排（主拆分 → 子并行）时每个子 Agent 的轨迹可独立追溯、按 `trace_id`/`agent_type` 关联，支撑「带证据链的根因报告」。落地后良率工程师可完整回溯「每轮思考 → 工具调用 → 结果 → 失败原因」——**非纯技术建设**（对照第一硬性要求）。

---

## Consequences

- ✅ **完整推理链可回溯（所有 Agent）**：每步 content / reasoning / tool_calls / tool_result / usage / 时间戳落库，ReAct / Planner / Reflection 统一接入——事后可重建完整路径，服务证据链根因报告与审计；
- ✅ **横切（对齐错误处理分发）**：`BaseAgent` 统一注入 `trace_collector`——任何新 Agent 类型继承 BaseAgent 即自动接入，无需各策略自行接线；与 `ErrorHandlerRegistry` 的「shared 契约 + BaseAgent 注入 + 各策略接入」同构；
- ✅ **非侵入**：捕获经 `TraceCollectorPort` 回调（默认 no-op），`ReActStrategy` 保持「只依赖 ports」纯算法，主循环结构不变；
- ✅ **策略解耦**：step 模型泛化（`type` 字段），策略层只产出 `AgentStep`（数据最全点），不感知存储/组装——Planner / Reflection 复用同一端口；
- ✅ **消息表语义纯净**：`messages`（回显历史）与 `traces`（证据链）分离，各司其职；
- ✅ **可降级/可迁移**：`TraceRepositoryPort` 抽象——未来接 Langfuse 只换实现；
- ⚠️ **存储增长**：`trace_steps` 逐轮存全文 reasoning（DeepSeek thinking 文本量大），随会话累积增长——需清理/归档策略（升级路径 ①）；
- ⚠️ **数据冗余**：SSE 事件流（实时展示）+ traces 表（事后回溯）双份数据——但用途正交（实时展示 vs 持久追溯），非重复建设；
- 📌 升级路径 ①：**轨迹清理/归档**——按会话保留窗口（如 N 天）或大小上限清理 `traces`/`trace_steps`，出现存储压力再落地；
- 📌 升级路径 ②：**长任务崩溃恢复流式写**——当前批量写（执行结束一次）；若出现分钟级长任务崩溃需恢复中间状态，再改逐事件/分段写；
- 📌 升级路径 ③：**接入 Langfuse/LangSmith**——`TraceRepositoryPort` 换外部实现 + OTel 导出，出现多环境/多后端观测需求时再上；
- 📌 升级路径 ④：**跨进程 trace 上下文传播**（OTel baggage/span context）——出现多 Agent 编排（主 Agent 拆子任务）跨进程追踪需求时再上（`trace_id`/`agent_type` 已为关联留位）。

---

## 实施计划（待批准后执行）

> 垂直切片：每切片 = 代码 + 测试 + 文档，逐步可验证。顺序从纯数据结构到装配。

### Slice 0 —— 领域契约（纯数据结构，零依赖）

- [ ] `app/domain/ports/trace.py`：`AgentStep` / `AgentTrace` dataclass + `TraceCollectorPort`（`on_turn_end` / `complete`，默认 no-op 实现）
- [ ] `app/domain/ports/__init__.py`：登记 trace 端口
- [ ] 测试 `tests/unit/test_trace_ports.py`：数据模型字段默认值 / 端口协议可注入（no-op collector 调用不抛错）
- [ ] 文档 `docs/domain_doc/ports_doc/ports.md`：登记端口 + 模型

### Slice 1 —— BaseAgent 横切接入（对齐错误处理分发）

- [ ] `app/domain/agent/base.py`：`BaseAgent.__init__` 加 `trace_collector: TraceCollectorPort | None = None`；`run()` 终结时（`_result` 就绪）调 `complete(user_input, result.content, status, total_usage, agent_type)`——**所有 Agent 统一接入**（默认 None=零开销）
- [ ] `app/domain/agent/executor.py`：`ReActAgent.__init__` 加 `trace_collector` 透传；`_strategy_cycle` 传 `execute(trace_collector=self._trace_collector)`
- [ ] `app/domain/reasoning/react.py`：`execute()` 加 `trace_collector`（默认 None）；每轮边界（LLM 调用后 + `_handle_tool_calls` 后）构造 `AgentStep(type="llm", name=f"turn_{n}", ...)` → `await trace_collector.on_turn_end(step)`
- [ ] 测试 `tests/unit/test_agent.py` 新增：BaseAgent 注入 collector，run 后 `complete` 被调（含 input/output/status）；None 不调用
- [ ] 测试 `tests/unit/test_react_strategy.py` 新增：mock collector 验证每轮回调收到完整 step（含 reasoning 全文/usage/工具记录）
- [ ] 文档 `docs/domain_doc/agent_doc/agent.md` / `executor.md` / `react.md`：构造契约 / execute 签名补 `trace_collector`

### Slice 2 —— 应用层编排

- [ ] `app/application/trace/collector.py`：`TraceCollector`（实现 `TraceCollectorPort`：累积 `AgentStep` 列表 + `complete` 组装 `AgentTrace`：trace_id 生成 + agent_type + input/output/status/total_usage + 经 repository 落库）
- [ ] `app/application/trace/__init__.py`：导出
- [ ] 测试 `tests/unit/test_trace_collector.py`：step 累积 / trace 组装（trace_id/agent_type/input/output/status/total_usage 正确）
- [ ] 文档 `docs/application_doc/trace_doc/trace.md`（新建模块文档）

### Slice 3 —— 基础设施存储

- [ ] `app/infrastructure/models/database/trace.py`：`TraceModel` / `TraceStepModel`（SQLAlchemy，表结构见 Decision §4）
- [ ] `app/infrastructure/trace_repository.py`：SQLAlchemy 实现 `TraceRepositoryPort`（`save_trace` 批量写 trace + steps）
- [ ] 测试 `tests/unit/test_trace_repository.py`：fake session 验证 trace + steps 写入（含 type/name/agent_type）
- [ ] `app/infrastructure/models/database/__init__.py` 登记模型

### Slice 4 —— 装配接线

- [ ] `app/container.py`：装配 `trace_repository`（infra 实现）+ `trace_collector`（应用层，注入 repository）
- [ ] `app/api/routes/chat.py`：`ReActAgent(..., trace_collector=...)`（或经 deps 提供）
- [ ] `app/api/deps.py`：`get_trace_collector` 依赖函数
- [ ] 测试 `tests/integration/test_chat_flow.py` 新增：Agent 运行后轨迹落库（trace + steps 行数 / 内容 / agent_type）
- [ ] 文档 `docs/api_doc/routes_doc/routes.md` / `docs/application_doc/README.md` 同步

### Slice 5 —— 收尾验证

- [ ] `uv run pytest` 全量通过
- [ ] `uv run python -m scripts.verify_alignment` 通过（ALIGNMENT 登记新模块 ports/trace.py / trace.py / trace_repository.py / application/trace）
- [ ] 本 ADR 状态 ✅ 已实现（实施后更新）

---

## 相关文档

- [架构文档](../../../docs/architecture.md)（分层与端口）
- [端口契约](../../../docs/domain_doc/ports_doc/ports.md)（LLMGateway / ToolGateway / ContextBudgetPort / CostLimiterPort 先例）
- [Agent 模块对外接口](../../../docs/domain_doc/agent_doc/agent.md)（BaseAgent 生命周期 / 错误处理横切注入先例）
- [ReActStrategy 设计](../../../docs/domain_doc/reasoning_doc/react.md)（`_tool_call_records` / `_finalize_outcome`）
- [异常处理与传播约定](../../../docs/shared_doc/error_handling.md)（脱敏 / 截断实践；错误处理横切先例）
