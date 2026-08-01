# Agent 模块说明文档

## 📋 目录

- [模块概述](#模块概述)
- [架构设计](#架构设计)
- [快速开始](#快速开始)
- [核心组件详解](#核心组件详解)
- [ReAct 策略详解](#react-策略详解)
- [SSE 事件流](#sse-事件流)
- [预留策略](#预留策略)
- [与 LLM 层的边界](#与-llm-层的边界)
- [最佳实践](#最佳实践)
- [常见问题](#常见问题)

---

## 模块概述

### 核心功能

Agent 模块是系统的**决策与行动核心**，负责编排 LLM 推理和工具调用的循环流程：

- **策略模式**：`BaseAgent` 定义统一入口，子类实现具体推理策略（ReAct / Plan-then-Execute / Reflection）
- **ReAct 循环**：推理 → 行动 → 观察，循环直到任务完成或达到上限
- **流式事件输出**：通过 SSE 事件流实时推送推理过程、工具调用和结果
- **Token 用量追踪**：跨轮累计 Token 消耗，通过 `AgentResult.usage` 对外暴露
- **可扩展钩子**：`on_thought` / `on_tool_call` / `on_tool_result` / `on_complete` 供子类覆盖

### 模块结构

```
app/core/agent/
├── __init__.py          # 模块导出
├── base.py              # 基类定义（AgentState, AgentContext, AgentResult, BaseAgent）
├── executor.py          # ReAct 策略实现（ReActAgent）
├── planner.py           # Plan-then-Execute 策略（预留）
└── reasoning.py         # Reflection 策略（预留）
```

### 依赖关系

```
Agent 模块
    │
    ├── app.services.LLMService    ← LLM 通信（单轮推理）
    ├── app.tools.ToolRegistry     ← 工具注册中心（执行工具）
    ├── app.core.events            ← SSE 事件构建（共享 LLM 层）
    └── app.config.settings        ← 配置中心（默认值）
```

---

## 架构设计

### 设计理念

1. **策略模式**

   ```
   BaseAgent.run()        ← 统一入口，管理异常/状态/事件
       │
       └─ _strategy_cycle()  ← 子类实现，定义具体策略逻辑
              │
              ├─ ReActAgent          ← 推理→工具→推理（当前实现）
              ├─ PlannerAgent        ← 先规划再执行（预留）
              └─ ReflectionAgent     ← 生成→反思→修正（预留）
   ```

2. **无状态设计**

   - 每次 `run()` 新建 Agent 实例
   - 上下文通过 `AgentContext` 传入
   - Agent 实例本身不持有会话状态

3. **LLM / Agent 分层清晰**

| 层     | 职责                           | 不涉及               |
| ------ | ------------------------------ | -------------------- |
| LLM 层 | 单轮推理、Token 提取、连接重试 | 消息管理、循环控制   |
| Agent  | 循环编排、工具调用、结果判定   | Token 解析、API 重试 |

4. **事件流驱动**

   ```
   LLM 层产出:  reasoning / message / error
   Agent 层产出: tool_call / tool_result / done / agent_info
   ```

### 数据流

```
用户输入
    │
    ▼
BaseAgent.run(user_input, messages, context)
    │
    ├─ build_info_event("Agent 开始处理")
    │
    ├─ _strategy_cycle()
    │   │
    │   ├─ LLM 推理 ──→ yield reasoning / message 事件
    │   │
    │   ├─ finish_reason == "tool_calls"
    │   │   │
    │   │   └─ _execute_tool_calls()
    │   │       ├─ yield tool_call 事件
    │   │       ├─ tool_registry.execute()
    │   │       └─ yield tool_result 事件
    │   │
    │   ├─ finish_reason == "stop" / "length"
    │   │   └─ yield done 事件 → 结束
    │   │
    │   └─ 达到 max_iterations
    │       └─ yield done 事件 → 强制结束
    │
    └─ AgentResult (run() 完成后通过 agent.result 获取)
```

---

## 快速开始

### 基本使用

```python
import asyncio
from app.config import settings
from app.core.agent import AgentContext, ReActAgent
from app.services import LLMService
from app.tools import ToolRegistry

async def main():
    # 1. 准备依赖
    llm = LLMService(
        api_key=settings.llm_api_key,
        model=settings.llm_model_id,
        base_url=settings.llm_base_url,
    )
    tools = ToolRegistry()
    # tools.register(...)  — 注册所需的工具

    # 2. 创建 Agent 上下文
    ctx = AgentContext(
        session_id="sess_001",
        user_id="user_001",
        # 不传则使用 config 中心默认值
        # max_iterations=settings.agent_max_iterations  (= 10)
        # temperature=settings.llm_temperature  (= 0.2)
        # max_tokens=settings.llm_max_tokens  (= 4096)
    )

    # 3. 构建消息列表
    messages = [
        {"role": "system", "content": "你是一个智能助手"},
        {"role": "user", "content": "查询今天的天气"},
    ]

    # 4. 运行 Agent
    agent = ReActAgent(llm=llm, tools=tools)
    async for event in agent.run("查询今天的天气", messages, ctx):
        print(event, end="")  # SSE 事件

    # 5. 获取结果
    result = agent.result
    print(f"成功: {result.success}")
    print(f"回答: {result.content[:100]}")
    print(f"Token 用量: {result.usage}")
    print(f"迭代轮数: {result.iterations}")
    print(f"工具调用: {len(result.tool_calls)} 次")

asyncio.run(main())
```

### 在 FastAPI 路由中使用

```python
from fastapi.responses import StreamingResponse
from app.core.agent import AgentContext, ReActAgent

@router.post("/agent/chat")
async def agent_chat(request: Request):
    ctx = AgentContext(
        session_id=request.session_id,
        user_id=request.user_id,
    )
    messages = build_messages(request.message)

    agent = ReActAgent(llm=llm_service, tools=tool_registry)

    async def generate():
        async for event in agent.run(request.message, messages, ctx):
            yield event

    return StreamingResponse(generate(), media_type="text/event-stream")
```

---

## 核心组件详解

### `AgentState` — 状态枚举

```python
class AgentState(Enum):
    IDLE = "idle"         # 空闲，等待输入
    THINKING = "thinking"  # LLM 推理中
    WAITING = "waiting"    # 等待工具执行结果
    COMPLETED = "completed" # 任务完成
    FAILED = "failed"      # 任务失败
    CANCELLED = "cancelled" # 被取消
```

状态转换流程：

```
IDLE → THINKING → (工具调用) → WAITING → THINKING → ... → COMPLETED / FAILED
                                                           → CANCELLED（取消）
```

通过 `agent.state` 随时获取当前状态。

---

### `AgentContext` — 上下文信息

```python
@dataclass
class AgentContext:
    session_id: str                    # 会话标识（必填）
    user_id: str                       # 用户标识（必填）

    # 参数控制（默认值从配置中心读取）
    max_iterations: int = settings.agent_max_iterations   # 默认 10
    temperature: float = settings.llm_temperature          # 默认 0.2
    max_tokens: int = settings.llm_max_tokens              # 默认 4096

    # 扩展字段
    metadata: dict[str, Any] = field(default_factory=dict)
```

**传递原则：** `AgentContext` 是值对象，每次 `run()` 传入，运行期间不变。

**典型场景：**

| 场景                      | max_iterations | temperature | 说明                    |
| ------------------------- | -------------- | ----------- | ----------------------- |
| 简单问答                  | 1-3            | 0.2         | 不需要工具，一轮即可    |
| 多步骤推理                | 5-10           | 0.3-0.5     | 需要多次调用工具        |
| 创意写作                  | 1-3            | 0.8-1.0     | 不需要工具，但需要创意  |
| 深度研究（需 Reflection） | 10-20          | 0.2-0.4     | 多轮推理+反思，即将支持 |

---

### `AgentResult` — 执行结果

```python
@dataclass
class AgentResult:
    success: bool                          # 是否成功
    content: str                           # 最终回答内容
    reasoning: str = ""                    # 完整推理过程（累计）
    tool_calls: list[dict] = field(...)    # 工具调用记录
    iterations: int = 0                    # 实际执行轮数
    total_tokens: int = 0                  # Token 总数（累计）
    usage: dict | None = None              # Token 明细（含 prompt/completion/total）
    error: str | None = None               # 错误信息
    metadata: dict[str, Any] = field(...)  # 扩展字段
```

**通过 `agent.result` 在 `run()` 完成后获取。**

`usage` 字段格式：

```python
{
    "prompt_tokens": 150,       # 输入 Token 总数（累计）
    "completion_tokens": 300,   # 输出 Token 总数（累计）
    "total_tokens": 450,        # 总 Token 数
}
```

`tool_calls` 字段格式：

```python
[
    {
        "tool": "search",           # 工具名称
        "params": {"query": "..."}, # 调用参数
        "result": "搜索结果...",    # 执行结果内容
        "success": True,            # 执行是否成功
        "duration": 1.234,          # 执行耗时（秒）
    },
    ...
]
```

---

### `BaseAgent` — 抽象基类

```python
class BaseAgent(ABC):
    def __init__(self, llm, tools):
        # 注入 LLMService 和 ToolRegistry

    async def run(self, user_input, messages, context) -> AsyncGenerator[str]:
        """统一入口，流式产出 SSE 事件"""

    @property
    def result(self) -> AgentResult | None:
        """获取最终结果（run() 完成后调用）"""

    @property
    def state(self) -> AgentState:
        """获取当前状态"""

    # ==== 子类必须实现 ====

    @abstractmethod
    def _strategy_cycle(self, user_input, messages) -> AsyncGenerator[str]:
        """策略循环"""

    # ==== 可选覆盖的钩子方法 ====

    async def on_thought(self, content: str): ...
    async def on_tool_call(self, name: str, params: dict): ...
    async def on_tool_result(self, name: str, result): ...
    async def on_complete(self, result: AgentResult): ...
```

**`BaseAgent.run()` 的职责：**

| 职责       | 说明                                                                  |
| ---------- | --------------------------------------------------------------------- |
| 上下文管理 | 保存 context，重置状态                                                |
| 异常处理   | 捕获 CancelledError / Exception，转为对应状态和事件                   |
| 状态管理   | 根据 `_result.success` 自动设置 COMPLETED / FAILED                    |
| 事件路由   | 调用 `_strategy_cycle()`，逐事件向上 yield                            |
| 结果存储   | 子类在策略循环中设置 `self._result`，`run()` 完成后通过 property 读取 |

**使用钩子方法：**

```python
class LoggingReActAgent(ReActAgent):
    async def on_tool_call(self, name: str, params: dict):
        logger.info(f"工具调用: {name}, 参数: {params}")

    async def on_tool_result(self, name: str, result):
        logger.info(f"工具结果: {name}, 耗时: {result.execution_time}s")

    async def on_complete(self, result: AgentResult):
        logger.info(f"Agent 完成, 迭代: {result.iterations}, Token: {result.total_tokens}")
```

---

## ReAct 策略详解

### 流程图

```
第 N 轮推理开始
    │
    ├─ 1. LLM 推理（流式输出 reasoning / message token）
    │
    ├─ 2. 复制 LLM 回复到 messages（assistant 角色）
    │
    ├─ 3. 检查 finish_reason
    │
    ├─ "tool_calls"
    │   │
    │   ├─ 4. _execute_tool_calls()
    │   │   ├─ yield tool_call 事件
    │   │   ├─ tool_registry.execute(name, args)
    │   │   ├─ yield tool_result 事件
    │   │   └─ 追加 tool 角色到 messages
    │   │
    │   └─ continue → 第 N+1 轮推理
    │
    ├─ "stop" / "length" / 有内容
    │   │
    │   └─ _build_result() → yield done → 结束
    │
    └─ 空输出
        │
        └─ yield info("未生成有效输出") → 重试
    │
    ├─ 达到 max_iterations
    │
    └─ 强制结束（用 last_result 兜底）
```

### `_strategy_cycle()` 核心代码结构

```python
async def _strategy_cycle(self, user_input, messages) -> AsyncGenerator[str]:
    last_result = None
    total_usage = {}

    for iteration in range(1, ctx.max_iterations + 1):
        # 1. LLM 推理
        stream_result = StreamResult()
        async for event in self._llm.async_generate(
            messages=messages, tools=tool_defs,
            temperature=ctx.temperature, max_tokens=ctx.max_tokens,
            result=stream_result,
        ):
            yield event

        last_result = stream_result
        # 累计 Token 用量
        if stream_result.usage:
            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                total_usage[k] = total_usage.get(k, 0) + stream_result.usage.get(k, 0)

        # 2. 追加 assistant 消息
        messages.append({"role": "assistant", "content": stream_result.content, ...})

        # 3. 根据 finish_reason 分支
        if finish_reason == "tool_calls" and has_tools:
            async for event in self._execute_tool_calls(...):
                yield event
            continue

        if finish_reason in ("stop", "length") or full_content.strip():
            self._result = self._build_result(...)
            yield build_done_event(...)
            return

        # 空输出 → 重试
        ...
```

### `_execute_tool_calls()` 方法

负责执行单轮所有工具调用，是 ReActAgent 的核心工具编排单元：

```python
async def _execute_tool_calls(
    self,
    tool_calls: list[dict],
    messages: list[dict],
    iteration: int,
) -> AsyncGenerator[str]:
    """
    参数:
        tool_calls: LLM 返回的工具调用列表
        messages: 当前消息列表（会被追加 tool 角色结果）
        iteration: 当前循环轮数

    Yields:
        tool_call / tool_result SSE 事件
    """
```

**每个工具的流程：**

1. 解析工具名称和参数（含 `json.loads` 异常保护）
2. `yield build_tool_call_event(...)` — 通知前端
3. `tool_registry.execute(name, args)` — 执行工具（带超时重试）
4. 记录 `_tool_call_records` — 含耗时、成功状态
5. `yield build_tool_result_event(...)` — 通知前端
6. 追加 tool 角色消息到 `messages`

---

## SSE 事件流

Agent 模块全量产出的事件类型（与 LLM 层共用 `app.core.events`）：

| 事件类型      | 产出者       | 触发时机                   | 关键字段                               |
| ------------- | ------------ | -------------------------- | -------------------------------------- |
| `reasoning`   | LLM 层       | 模型输出思考 token         | `content`: 单 token                    |
| `message`     | LLM 层       | 模型输出回答 token         | `content`: 单 token                    |
| `error`       | LLM 层/Agent | LLM 调用失败/Agent 异常    | `content`: 错误描述                    |
| `tool_call`   | Agent        | LLM 决定调用工具           | `content`: 工具名, `params`, iteration |
| `tool_result` | Agent        | 工具执行完成               | `content`: 结果摘要, tool, duration    |
| `done`        | Agent        | Agent 结束（正常/强制）    | `iterations`, `total_tokens`           |
| `agent_info`  | Agent        | 状态信息（开始/重试/超限） | `content`: 描述                        |

### 事件流时序示例

```
Agent 开始处理           → type=agent_info
第 1 轮推理              → type=agent_info
思考 token...            → type=reasoning
回答 token...            → type=message
检测到 2 个工具调用      → type=agent_info
search                   → type=tool_call
search 执行结果          → type=tool_result
readFile                 → type=tool_call
readFile 执行结果        → type=tool_result
第 2 轮推理              → type=agent_info
思考 token...            → type=reasoning
回答 token...            → type=message
Agent 完成               → type=done  {iterations: 2, total_tokens: 550}
```

---

## 预留策略

### `planner.py` — Plan-then-Execute（预留）

预期工作流：

1. **规划阶段**：LLM 根据用户输入生成一份执行计划（步骤列表）
2. **执行阶段**：按计划逐步执行工具，每步结果反馈给下一步
3. **调整阶段**：如果某步失败，允许重新规划

适用场景：

- 复杂数据分析（多步依赖）
- 流程化任务
- 需要可解释的执行路径

### `reasoning.py` — Reflection（预留）

预期工作流：

1. **生成阶段**：LLM 首轮输出回答
2. **反思阶段**：LLM 评估自己的输出质量，指出问题
3. **修正阶段**：根据反思结果修正输出
4. **验证阶段**：再次评估，确认是否达到标准

适用场景：

- 代码生成（自动检查 bug）
- 数学推理（验算步骤）
- 长文写作（质量改进）

---

## 与 LLM 层的边界

### 职责划分

| 功能                 | 所属层   | 原因                                      |
| -------------------- | -------- | ----------------------------------------- |
| 单轮流式推理         | LLM 层   | 纯 API 通信，与策略无关                   |
| Token 级累积         | LLM 层   | 流式 chunk 解析，Agent 不关心底层协议     |
| API 重试/退避        | LLM 层   | 连接层容错，Agent 不应感知                |
| 消息历史管理         | Agent 层 | Agent 需要决定哪些消息传给 LLM            |
| 循环控制（是否结束） | Agent 层 | 策略相关：ReAct 继续，Reflection 反思     |
| 工具调用与编排       | Agent 层 | Agent 决定何时调、调什么、结果怎么用      |
| 高层事件构造         | Agent 层 | tool_call / tool_result / done 属于语义层 |

### 通信方式

```
LLM 层                      Agent 层
──────                      ────────
async_generate(messages,
    tools, ..., result)  ──→  yield reasoning/message
                              stream_result 写累加到传入的 result 对象
                          ←── 遍历完成，result 中读取 finish_reason / tool_calls / usage
```

**关键约定：**

- `async_generate()` 只 yield 字符串（SSE 事件），不 yield `StreamResult`
- `StreamResult` 通过传参引用传递，调用方自行读取
- Agent 层负责 `messages` 的组装和修改

---

## 最佳实践

### 1. 合理设置 max_iterations

```python
# 简单问答——不需要工具
ctx = AgentContext(..., max_iterations=1)

# 需要工具搜索——预留余量
ctx = AgentContext(..., max_iterations=5)

# 复杂任务——但不要无限
ctx = AgentContext(..., max_iterations=15)
```

`max_iterations` 过大会导致 Token 浪费且可能陷入死循环，建议根据任务类型动态设置。

### 2. 每次 run() 新建 Agent 实例

```python
# ✅ 正确：每次 run 新建实例
agent = ReActAgent(llm=llm, tools=tools)
async for event in agent.run(input1, messages1, ctx1):
    yield event

agent = ReActAgent(llm=llm, tools=tools)
async for event in agent.run(input2, messages2, ctx2):
    yield event

# ❌ 错误：复用实例执行多次 run
agent = ReActAgent(llm=llm, tools=tools)
await agent.run(...)
await agent.run(...)  # 状态混乱
```

### 3. 监控 Token 消耗

```python
agent = ReActAgent(llm=llm, tools=tools)
async for event in agent.run(...):
    yield event

result = agent.result
if result and result.usage:
    total = result.usage["total_tokens"]
    if total > 5000:
        logger.warning(f"Token 消耗较高: {total}")
    # 成本监控
    cost = (result.usage["prompt_tokens"] * 0.00015
          + result.usage["completion_tokens"] * 0.0006) / 1000
    logger.info(f"预估成本: ${cost:.4f}")
```

### 4. 获取完整推理过程

```python
result = agent.result
if result:
    print(f"推理过程:\n{result.reasoning}")
    print(f"最终回答:\n{result.content}")
```

### 5. 错误处理

```python
agent = ReActAgent(llm=llm, tools=tools)
try:
    async for event in agent.run(...):
        yield event
except asyncio.CancelledError:
    # 用户取消
    pass

result = agent.result
if result and not result.success:
    logger.error(f"Agent 执行失败: {result.error}")
    # 可能的原因：LLM 调用失败、max_iterations 耗尽、工具全部失败
```

---

## 常见问题

### Q1: Agent 一直在 tool_calls 循环中出不来？

**A:** 通常是 `max_iterations` 设置过大。检查：

```python
# LLM 可能重复调用同样的工具
# 解决：在 System Prompt 中限制工具的重复调用
```

也可以在 ReActAgent 子类中增加去重逻辑。

### Q2: `agent.result` 返回 None？

**A:** 在 `run()` 完成之前 `result` 属性是 `None`。必须在 `async for event in agent.run(...)` 循环结束后（或发生异常时）才能读取。

### Q3: 如何让 Agent 调用不支持流式的模型？

**A:** 在 `LLMService` 层面，非流式模型只需将 `stream=False` 并在 `_process_stream_response` 中处理非流式响应。Agent 层不感知差异。

### Q4: 如何自定义策略？

**A:** 继承 `BaseAgent`，实现 `_strategy_cycle()`：

```python
class MyCustomAgent(BaseAgent):
    async def _strategy_cycle(self, user_input, messages) -> AsyncGenerator[str]:
        # 实现自定义逻辑
        # 使用 self._llm.async_generate() 调用 LLM
        # 使用 self._tools.execute() 调用工具
        # 使用 self._build_result() 构建结果
```

### Q5: Agent 模块是否线程安全？

**A:** 否。Agent 实例是单线程异步设计，不在多个 Task 中共享同一个 Agent 实例。如果需要并发，为每个请求创建独立的 Agent 实例。

### Q6: `usage` 在什么情况下为 None？

```python
# 以下情况 usage 为 None：
# 1. LLM 调用失败（返回 error 事件）
# 2. LLM 流式响应未返回 usage chunk（某些 API 实现）
# 3. StreamResult 在异常路径中未被填充
```

建议始终做 `if result.usage:` 检查。

### Q7: 回传 tool 消息时报 400 `Messages with role 'tool' must be a response to a previous message with 'tool_calls'`？

**A:** OpenAI 兼容 API 的硬性规则：ReAct 循环把 tool 消息回传前，**前置的 assistant 消息必须带 `tool_calls` 字段**。修复前 executor 只回传 `content` + `reasoning_content`，DeepSeek 第二轮必报 400，Agent 空转重试到迭代上限。

```python
# 回传 assistant 消息时必须带上 tool_calls
assistant_msg["tool_calls"] = stream_result.tool_calls  # 见 executor.py 步骤 2
```

已加测试断言防回归。

---

## 配置关联

Agent 模块与 `settings.py` 中的以下配置项关联：

| 配置项                       | 默认值 | 影响范围                     |
| ---------------------------- | ------ | ---------------------------- |
| `AGENT_MAX_ITERATIONS`       | 10     | AgentContext 默认值          |
| `LLM_TEMPERATURE`            | 0.2    | AgentContext 默认值          |
| `LLM_MAX_TOKENS`             | 4096   | AgentContext 默认值          |
| `AGENT_TIMEOUT`              | 300    | 任务超时（预留）             |
| `AGENT_STREAMING`            | True   | 是否启用流式输出             |
| `AGENT_MAX_CONCURRENT_TOOLS` | 3      | 单任务最大并发工具数（预留） |

---

## 相关文档

- [架构设计文档](architecture.md)
- [配置管理模块说明](config.md)
- [工具模块说明](tools.md)
- [API 文档](api.md)
- [部署文档](deployment.md)
