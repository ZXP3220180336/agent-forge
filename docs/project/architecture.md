# 架构设计文档

> **更新日期**：2026-09-12
> **文档定位**：系统目标架构、依赖边界与候选演进路线。目标图不表示全部已实现，也不授权按图建设。模块机制见对应说明，模块实现状态统一在 [ALIGNMENT.md](../ALIGNMENT.md) 维护。
> **核验边界**：治理文档已接入完整仓库。本轮仅核验迁移结构、引用与文件映射，未验证全部运行契约；模块状态及验证范围见 [ALIGNMENT](../ALIGNMENT.md)。

---

## 📋 目录

- [架构设计文档](#架构设计文档)
  - [📋 目录](#-目录)
  - [系统总览](#系统总览)
    - [一句话架构](#一句话架构)
    - [现状、目标与演进](#现状目标与演进)
    - [两张总图](#两张总图)
  - [目标分层架构](#目标分层架构)
    - [分层总图](#分层总图)
    - [各层实现目标](#各层实现目标)
    - [目标核心链路](#目标核心链路)
  - [依赖方向原则](#依赖方向原则)
    - [单向依赖规则](#单向依赖规则)
    - [目标依赖关系图](#目标依赖关系图)
    - [依赖倒置：端口与适配器](#依赖倒置端口与适配器)
    - [零外部框架依赖层](#零外部框架依赖层)
    - [装配根](#装配根)
  - [现状耦合与差距](#现状耦合与差距)
    - [现状分层](#现状分层)
    - [耦合点状态清单](#耦合点状态清单)
  - [演进路径](#演进路径)
    - [Phase A 基础设施落地](#phase-a-基础设施落地)
    - [Phase B 解耦改造](#phase-b-解耦改造)
    - [Phase C 应用与编排层](#phase-c-应用与编排层)
    - [Phase D 可观测性与业务域](#phase-d-可观测性与业务域)
    - [阶段依赖与验收](#阶段依赖与验收)
  - [相关文档](#相关文档)

---

## 系统总览

### 一句话架构

FastAPI 异步受理用户目标 → 应用/编排层调度与拆分 → 领域层 Agent 内核推理（ReAct 循环）→ 集成层调用 LLM 网关与工具执行 → 基础设施层持久化与缓存 → 汇总输出带**证据链**的根因报告。产品方向为**多 Agent 任务执行引擎 + 半导体良率异常根因分析（Yield RCA）**。

### 现状、目标与演进

| 维度 | 目标与演进关注点 | 状态入口 |
| --- | --- | --- |
| 分层 | 清洁/六边形架构；领域端口与横切能力分工 | [模块登记](../ALIGNMENT.md) |
| 依赖方向 | 向内依赖、端口实现与装配根注入 | [模块登记](../ALIGNMENT.md)及对应模块契约 |
| 配置与装配 | 业务配置注入；统一服务创建和生命周期；核验日志 bootstrap 例外 | [模块登记](../ALIGNMENT.md) |
| 数据访问 | 按真实需求分离 Repository 与 Cache；验证无缓存时的持久化路径 | [模块登记](../ALIGNMENT.md)及会话文档 |
| 编排 | 从可演示受理闭环向主从排查扩展，再按负载需要引入队列与背压 | [产品闭环](product.md#里程碑与闭环) |
| 可观测 | 日志、追踪、指标、审计各有明确需求与独立实施状态 | [模块登记](../ALIGNMENT.md)及横切文档 |

### 两张总图

- **[分层架构图](#分层总图)**（ASCII，详细到模块）——回答「系统由哪些层、哪些模块组成」
- **[目标依赖关系图](#目标依赖关系图)**（text 文本图）——回答「各层各模块之间如何依赖」

---

## 目标分层架构

### 分层总图

```text
┌───────────────────────────────────────────────────────────────────────────────┐
│              AGENT-FORGE 目标架构（工业级 · 清洁 / 六边形）                       │
│          多 Agent 任务执行引擎 + 半导体良率根因分析（Yield RCA）                   │
└───────────────────────────────────────────────────────────────────────────────┘

┌───────────────────────────────────────────────────────────────────────────────┐
│ ① 接入层 Interface · app/api/                ⚠ 依赖方向：▼ 调用应用层用例        │
│  ┌─────────────────┬───────────────────┬───────────────────────────────────┐   │
│  │ routes 路由      │ middleware 中间件  │ deps.py 解析器                     │   │
│  │ chat · session  │ auth 鉴权 (JWT)   │ Depends → container（薄）          │   │
│  │ task · agent    │ rate_limit 限流   │ schemas/ Pydantic DTO             │   │
│  │ admin           │ error_handler     │ SSE 流适配 · WS                    │   │
│  │                 │ correlation 追踪   │                                   │   │
│  └─────────────────┴───────────────────┴───────────────────────────────────┘   │
└───────────────────────────────────────────────────────────────────────────────┘
                                  │  调用用例（Use Case）
                                  ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ ② 应用 / 编排层 Application · app/application/                                 │
│  ┌────────────────┬───────────────────┬─────────────────┬──────────────────┐   │
│  │ 用例服务        │ 任务调度枢纽        │ 多 Agent 编排     │ 业务用例          │   │
│  │ ChatService    │ TaskService       │ Orchestrator    │ RcaUseCase       │   │
│  │ SessionUseCase │ TaskQueue 优先级   │ 主拆→子并行→汇总  │ (Yield RCA)      │   │
│  │ TaskUseCase    │ WorkerPool 状态机  │ AgentFactory    │ 证据链编排        │   │
│  └────────────────┴───────────────────┴─────────────────┴──────────────────┘   │
└───────────────────────────────────────────────────────────────────────────────┘
                                  │  依赖领域模型 + 端口
                                  ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ ③ 领域层 Domain · app/domain/   ★ 零外部框架依赖 · 禁止 import services         │
│  ┌───────────────┬───────────────────┬──────────────────┬──────────────────┐   │
│  │ Agent 推理内核 │ 记忆策略           │ 提示词            │ 领域服务 / 模型    │   │
│  │ BaseAgent     │ working           │ PromptManager   │ YieldRcaService  │   │
│  │ ReActAgent    │ short_term        │ templates       │ EvidenceChain    │   │
│  │ PlannerAgent  │ long_term         │ (system/tools/  │ Task / SubTask / │   │
│  │ reasoning 策略 │ (纯策略接口)       │  planning)      │ TaskState 模型    │   │
│  │ CoT/Reflection│                   │                  │ AgentContext/    │   │
│  └───────────────┴───────────────────┴──────────────────┴──────────────────┘   │
└───────────────────────────────────────────────────────────────────────────────┘
                                  │  定义并依赖端口（依赖倒置）
                                  ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ ④ 端口层 Ports · app/domain/ports/   —— 领域拥有的抽象协议（typing.Protocol）    │
│  LLMGateway（含 token 计数）│ ToolGateway │ Session/Message/TaskRepository     │
│  CachePort │ VectorStorePort │ EmbeddingPort │ EventPublisher │ IdGenerator  │
└───────────────────────────────────────────────────────────────────────────────┘
                     ▲ 实现（Adapter，向端口注入）
      ┌──────────────┴────────────────────────────┐
      ▼                                            ▼
┌───────────────────────────────┐      ┌───────────────────────────────────────┐
│ ⑤ 能力 / 集成层 Capability     │      │ ⑥ 基础设施层 Infrastructure            │
│   app/integration/             │      │   app/infrastructure/（目标职责）       │
│  LLM 网关：LLMService +        │      │  db/engine.py + db/repos/              │
│   llm/ 内部组件（实现 LLMGateway）│     │   （SqlSession/Message/Task Repo）     │
│  工具：ToolService（拆分 Facade）│      │  redis/client + redis/cache.py        │
│   + builtin 10 工具（含 RCA 5）  │      │   （RedisCache + NullCache 真降级）    │
│  嵌入：EmbeddingService         │      │  mq/ 队列 · store/ 存储 · http/       │
│  向量：VectorStore Adapter      │      │  models/database/（ORM 归位）         │
└───────────────────────────────┘      └───────────────────────────────────────┘
                                  │
                                  ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ ⑦ 共享内核 Shared Kernel · app/shared/   —— 无依赖公共类型，被所有层引用         │
│  events.py：共享事件类型与 build_*_event；事件发布端口按真实需求独立归属       │
│  exceptions.py：异常体系 → 业务错误码     types.py：通用类型 / 标识             │
└───────────────────────────────────────────────────────────────────────────────┘

┌ 横切关注点（注入方式使用，禁止散落 import 单例）────────────────────────────────┐
│ 配置 app/config：业务配置由装配根注入；日志 bootstrap 边界独立核验              │
│ 可观测/安全 app/platform：日志 · Prometheus 指标 · OTel 追踪 · 审计 · JWT        │
└────────────────────────────────────────────────────────────────────────────────┘
┌ ⑧ 评测与评估层 Evaluation · app/eval/（离线 · 不参与运行时调用链） ────────────┐
│ 评测集（对话 / 工具调用 / Yield RCA 场景分组）· 评测执行器（mock LLM / 真实调用）│
│ 评估指标（任务成功率 / 工具准确率 / 延迟 / 成本）· 基准报告（benchmarks/ 随版本积累）│
└────────────────────────────────────────────────────────────────────────────────┘

装配根 Composition Root：app/container.py —— 读取业务配置，组装 ④⑤⑥ 并注入 ③
```

### 各层实现目标

> 全系统目标蓝图，按架构层组织。表中阶段是需求归类，不是执行顺序或完成标记；逐模块状态只查 [ALIGNMENT.md](../ALIGNMENT.md)，不存在的目标模块须通过需求与抽象 Gate 后才能创建。

#### 接入层

| 目标模块 | 职责 | 演进关注点 |
| --- | --- | --- |
| chat / session 路由 | 交互式聊天 SSE、会话 CRUD | — |
| task / agent / admin 路由 | 异步任务提交/查询/进度、Agent 管理 | Phase C |
| 中间件（auth / rate_limit / error_handler / correlation） | 鉴权、限流、错误码映射、关联 ID | Phase D |
| deps.py 薄解析 + schemas DTO | Depends → container；请求/响应模型 | — |

#### 应用层

| 目标模块 | 职责 | 演进关注点 |
| --- | --- | --- |
| 用例服务（Chat / Session / Task / Rca） | 路由内联编排上移为用例 | Phase C |
| TaskScheduler（队列 / 优先级 / 背压 / worker / 状态） | 任务调度枢纽 | Phase C |
| Orchestrator + AgentFactory | 多 Agent 主拆 → 子并行 → 汇总 | Phase C |
| 定时任务 | 周期任务编排 | Phase D |

#### 领域层

| 目标模块 | 职责 | 演进关注点 |
| --- | --- | --- |
| Agent 内核（BaseAgent / ReActAgent） | ReAct 循环推理、工具调用 | — |
| PlannerAgent + reasoning（CoT / Reflection） | 任务拆分、推理策略 | Phase C |
| 记忆策略（working / short_term / long_term） | 短期 / 长期记忆 | Phase C/D |
| PromptManager + planning 模板 | 提示词管理与接线 | Phase C |
| YieldRcaService + EvidenceChain | 良率 RCA 领域服务、证据链 | Phase D |
| Task / SubTask / TaskState 模型 | 任务领域模型与状态机 | Phase C |

#### 端口层

| 目标模块 | 职责 | 演进关注点 |
| --- | --- | --- |
| LLMGateway / ToolGateway | 领域依赖的 LLM / 工具抽象；token 计数是 LLMGateway 能力 | — |
| Session / Message / Task Repository | 持久化抽象 | Phase A |
| CachePort / VectorStorePort | 缓存 / 向量抽象 | Phase A/D |
| EmbeddingPort | 嵌入抽象 | — |
| EventPublisher / IdGenerator | 事件发布、ID 生成 | Phase B |

#### 集成层

| 目标模块 | 职责 | 演进关注点 |
| --- | --- | --- |
| LLMService + llm/ 内部组件 | LLM 网关（实现 LLMGateway） | 归位 integration |
| 可靠性链（重试 / 熔断 / 限流 / 整流 / 结构化降级） | 外部调用可靠性 | — |
| ToolService（拆分 Facade）+ builtin 10 工具 | 工具执行（实现 ToolGateway） | — |
| RCA 工具（良率 / 告警 / FDC / wafer / 历史检索） | 良率分析工具链 | — |
| EmbeddingService（实现 EmbeddingPort） | 文本向量化 | Phase D |
| VectorStore adapter（Milvus） | 向量库检索 | Phase D |

#### 基础设施层

| 目标模块 | 职责 | 演进关注点 |
| --- | --- | --- |
| db/engine.py + Repository 实现 | 连接池、SQLAlchemy Repository | Phase A |
| redis/client + RedisCache + NullCache | 缓存实现（含真降级） | Phase A |
| mq/（asyncio.Queue / Redis Streams） | 任务队列、事件总线实现 | Phase C |
| store / http | 对象存储、HTTP 客户端封装 | Phase D |
| ORM（models/database/） | Session / Message / Task 模型 | Phase A |

#### 共享内核

| 目标模块 | 职责 | 演进关注点 |
| --- | --- | --- |
| events.py | 共享事件类型与格式构造；发布端口按真实需求另定归属 | — |
| exceptions.py（异常体系 → 错误码） | 统一异常与错误码 | — |
| types.py | 通用类型 / 标识 | — |

#### 横切与装配根

| 目标模块 | 职责 | 演进关注点 |
| --- | --- | --- |
| 配置：settings + register_config 注入 | 业务配置由装配根注入；日志 bootstrap 边界独立核验 | — |
| 可观测：日志 / 指标 / 追踪 / 审计 | 可观测三件套 | Phase D |
| 安全：鉴权 JWT / 密钥管理 | 认证授权、密钥托管 | Phase D |
| 装配根：container.py | 业务配置装配 + 服务组装 + 生命周期 | — |

#### 评测与评估（离线质量保障，不参与运行时调用链）

| 目标模块 | 职责 | 演进关注点 |
| --- | --- | --- |
| 评测集（对话 / 工具调用 / Yield RCA 场景） | 评测用例与标注，覆盖正常 / 边界 / 失败路径 | Phase D |
| 评测执行器（mock LLM / 真实调用） | 跑评测、CI 回归触发、结果对比 | Phase D |
| 评估指标（任务成功率 / 工具准确率 / 延迟 / 成本） | 质量基线量化 | Phase D |
| 基准报告（benchmarks/） | 各版本评测结果与基线对比 | Phase D |

> 详见 [评测与评估](../eval_doc/evaluation.md)。

---

### 目标核心链路

#### 链路 1：交互式聊天（HTTP → SSE）

```text
POST /api/chat/send
  → deps.py 注入 ChatService
  → ChatService（用例编排）
      → SessionManager 校验会话 + 存 user 消息
      → ContextManager 经 LLMGateway 计数并按消息 ID 快照组装/截断
      → 创建本次 ReActAgent 与唯一 run 身份
      → TaskService 并发闸门 → BaseAgent.run()（ReAct 循环）
              → LLMGateway.async_generate（流式 + 重试/熔断/限流/整流）
              → ToolGateway.execute（工具级信号量）
  → 路由适配 SSE/断连 → ChatRun 清理并存 assistant 消息
```

#### 链路 2：多 Agent 主从并行（批量任务）

```text
POST /api/tasks/submit
  → TaskUseCase 入队（优先级 + 有界背压）
  → Worker 取任务 → TaskState: pending → running
  → Orchestrator 编排：
      主 Agent 拆分（planning prompt）→ SubTask[1..n]
      → 并行子 Agent（受并发信号量约束）→ 各自 ReAct 循环
  → 主 Agent 汇总 → TaskState: completed → 结果带证据链
```

#### 链路 3：Yield RCA 证据链（产品场景）

```text
RcaUseCase 受理良率异常报告
  → 主 Agent 拆分排查子任务（查批次良率 / 设备告警 / FDC 参数 / wafer map / 历史案例）
  → 子 Agent 并行执行（工具链逐层推理）
  → VectorStorePort 检索历史 RCA（RAG）+ EmbeddingPort
  → 汇总：每个结论附数据来源（证据链）+ 置信度分级 + 显式放弃
  → 人机协同：提议根因与下一步，工程师验证
```

---

## 依赖方向原则

### 单向依赖规则

```text
接入层 → 应用层 → 领域层  ←（依赖倒置） 集成层 / 基础设施层 实现领域端口
                                                    ↗
所有层 → 共享内核（无反向依赖）                    领域层 只依赖 端口层 + 共享内核
横切关注点：通过注入使用，禁止模块内 import 全局单例
```

1. **依赖收敛指向内侧**：`api → application → domain`；领域内部按职责依赖 ports、shared 和 prompts，reasoning 不反向依赖 agent 生命周期对象；`integration` / `infrastructure` 是端口实现方。
2. **禁止越层**：接入层不得触碰领域内部细节或直接 new Agent；领域层不得 import 服务层 / 基础设施 / 配置。
3. **配置经装配注入**：新增业务模块不直接读取 settings。日志 bootstrap 读取配置的既有边界需核验并登记；shared 可使用标准库 logging，不反向依赖 platform。不能把命名 logger 等同于业务全局单例。禁止硬编码密钥、端口和可配置运行参数，环境与命令约定见 [部署说明](deployment.md)。

### 目标依赖关系图

```text
目标角色（不表示已实现）：
① 接入层 app/api
  routes: chat/session/task/agent/admin
  middleware: auth/rate_limit/error/correlation
  deps.py → container
② 应用层 app/application
  用例: Chat/Session/Task/Rca
  TaskScheduler 队列+worker+状态
  Orchestrator 主拆→子并行→汇总
  AgentFactory
③ 领域层 app/domain ★零框架
  Agent 内核 Base/ReAct/Planner
  记忆策略 working/short/long
  PromptManager + templates
  YieldRcaService · Task/SubTask
④ 端口层 app/domain/ports
  LLMGateway
  ToolGateway
  Session/Message/Task Repository
  CachePort
  VectorStorePort
  EmbeddingPort
  EventPublisher
⑤ 集成层 app/integration
  LLMService + llm/内部组件
  ToolService + builtin
  EmbeddingService
  VectorStore
⑥ 基础设施层 app/infrastructure
  engine + Repository 实现
  RedisCache + NullCache
  MQ 队列
  ORM
⑦ 共享内核 app/shared
  events/exceptions/types
横切 config + platform
  settings 业务配置装配注入
  日志/指标/追踪/审计/安全
装配根 app/container.py
  container 唯一组装 + 生命周期

使用 / 装配关系：
routes: chat/session/task/agent/admin → 用例: Chat/Session/Task/Rca
routes: chat/session/task/agent/admin → TaskScheduler 队列+worker+状态
deps.py → container → container 唯一组装 + 生命周期
用例: Chat/Session/Task/Rca → Agent 内核 Base/ReAct/Planner
用例: Chat/Session/Task/Rca → YieldRcaService · Task/SubTask
用例: Chat/Session/Task/Rca → AgentFactory
TaskScheduler 队列+worker+状态 → Session/Message/Task Repository
Orchestrator 主拆→子并行→汇总 → TaskScheduler 队列+worker+状态
Orchestrator 主拆→子并行→汇总 → AgentFactory
AgentFactory → Agent 内核 Base/ReAct/Planner
Agent 内核 Base/ReAct/Planner → LLMGateway
Agent 内核 Base/ReAct/Planner → ToolGateway
Agent 内核 Base/ReAct/Planner → EventPublisher
Agent 内核 Base/ReAct/Planner → PromptManager + templates
YieldRcaService · Task/SubTask → VectorStorePort
YieldRcaService · Task/SubTask → EmbeddingPort
记忆策略 working/short/long → VectorStorePort
LLMService + llm/内部组件 —实现→ LLMGateway
ToolService + builtin —实现→ ToolGateway
EmbeddingService —实现→ EmbeddingPort
VectorStore —实现→ VectorStorePort
engine + Repository 实现 —实现→ Session/Message/Task Repository
RedisCache + NullCache —实现→ CachePort
MQ 队列 —实现→ EventPublisher
container 唯一组装 + 生命周期 → settings 业务配置装配注入
container 唯一组装 + 生命周期 → LLMService + llm/内部组件
container 唯一组装 + 生命周期 → ToolService + builtin
container 唯一组装 + 生命周期 → EmbeddingService
container 唯一组装 + 生命周期 → VectorStore
container 唯一组装 + 生命周期 → engine + Repository 实现
container 唯一组装 + 生命周期 → RedisCache + NullCache
container 唯一组装 + 生命周期 → MQ 队列
① 接入层 app/api → events/exceptions/types
② 应用层 app/application → events/exceptions/types
middleware: auth/rate_limit/error/correlation —注入观测→ 日志/指标/追踪/审计/安全
LLMService + llm/内部组件 —注入观测→ 日志/指标/追踪/审计/安全
ToolService + builtin —注入观测→ 日志/指标/追踪/审计/安全
```

> 箭头表示使用 / 装配依赖；实现和注入观测关系分别显式标注。共享内核被引用但不反向依赖业务层；装配根可引用具体实现用于注入。图中接口和模块是目标角色，是否存在以模块登记和源码核验为准。

---

### 依赖倒置：端口与适配器

领域层定义抽象协议（`typing.Protocol`），集成层 / 基础设施层实现之，装配根在启动时注入。以下为依赖关系示意，方法签名、同步/异步与流返回契约以正式端口说明为准，不作为可直接替换源码的接口定义：

```python
# app/domain/ports/llm_gateway.py —— 领域拥有的端口
from typing import Protocol

class LLMGateway(Protocol):
    async def async_generate(self, messages, tools=None, model_key="main", **kwargs): ...
    async def generate(self, messages, tools=None, model_key="fast", **kwargs): ...

# app/integration/llm/llm_service.py —— 集成层实现
class LLMService:  # implements LLMGateway
    async def async_generate(self, messages, tools=None, model_key="main", **kwargs): ...

# app/container.py —— 装配根注入
def build_agent(llm: LLMGateway, tools: ToolGateway) -> BaseAgent:
    return ReActAgent(llm=llm, tools=tools)   # BaseAgent 依赖抽象，不依赖具体类
```

参照范式：现有 `llm/` 子包的 `register_config()` 配置注入（零 settings 依赖）、`ContextManager` 的构造注入。

### 零外部框架依赖层

| 层 | 允许依赖 | 禁止依赖 |
| --- | --- | --- |
| 领域层 | 标准库、领域内职责模块、`domain/ports`、`shared`；纯校验依赖按已确认契约审查 | FastAPI / SQLAlchemy / OpenAI SDK / redis / tiktoken / pydantic-settings 等传输与基础设施耦合 |
| 端口层 | 标准库（`typing.Protocol`） | 一切外部框架 |
| 共享内核 | 标准库（可选 Pydantic dataclass） | 业务依赖 |

tiktoken 计数由集成层 `token_counter.py` 实现，并经 `LLMGateway.count_*` 提供给应用层；ORM / Redis 经 Repository / CachePort 在基础设施层实现。

### 装配根

`app/container.py` 承担服务装配边界：

- **业务配置装配入口**：业务模块经 `register_config()` / 构造注入获得配置，不直接 import settings；日志 bootstrap 等既有例外须独立核验，不能声称全仓仅一个读取点
- **唯一组装**：创建基础设施 → 注册配置 → 实例化适配器 → 组装用例 / Agent → 生命周期（initialize / shutdown）
- `main.py` 只做 lifespan 接线；`deps.py` 只做 `Depends` → container 的薄解析

---

## 现状耦合与差距

### 现状分层

当前模块状态、对应说明和测试只在 [ALIGNMENT.md](../ALIGNMENT.md) 登记。该登记目前来自项目文档，尚未回仓核验，不据此声称生产路径已验证。

### 耦合点状态清单

以下保留架构评估问题与候选处理方向，不另行维护“已解决/待做”标签；每次实施先检查真实调用方、当前代码和关联决策。

| 关注点 | 需要验证的边界 | 有需求时的处理方向 |
| --- | --- | --- |
| C1 资源创建职责 | DB/Redis 工厂和容器组装是否清晰 | 将具体连接创建放基础设施，容器只组装和管理生命周期；不重复创建已有 container |
| C2 会话数据访问 | SessionManager 业务、缓存、SQL 的变化原因与测试成本 | DB-F05a 已提取 SessionStorePort/PostgresSessionStore，保留应用缓存；不为蓝图新增 CachePort/NullCache |
| C3/C4 领域依赖 | 领域是否通过 LLMGateway/ToolGateway，而非具体服务或旧 core/services 路径 | 使用现有端口，消除新增反向耦合 |
| C5 配置 | 业务模块是否自读 settings；日志 bootstrap 是否是明确例外 | 保持构造/register_config 注入，记录有范围的例外 |
| C6 共享事件 | 共享格式、传输序列化和事件发布是否混同 | 按消费者和生命周期划分，不能因为目标图而预建事件总线 |
| C7 DI 与 Agent 创建 | 路由直接创建 Agent 的存量耦合是否影响当前需求 | 先验证现有注入链，再决定用例/Factory 是否有独立价值 |
| C8 工具职责 | Facade 与 Registry/Selector/Validator/Executor/ResultProcessor/Auditor 等既有组件是否职责清晰 | 复用现有组件；Stats/Hooks/Assembler 不误列为另一套替代六组件方案 |
| C9 未接线能力 | Prompt/Planner、Memory、向量与 Embedding 是否有真实消费链 | 区分已实现策略与尚未完成的跨 Agent 产品闭环，不依据旧“空文件”描述重新开发 |
| C10 构造注入 | 参数来源、资源拥有者、初始化与关闭链 | 作为持续审查边界，避免把旧范例机械推广为新抽象 |

部署还需核验缓存缺失、真实 DB 驱动、认证与 CORS 的组合行为。会话文档已声明 `_cache_*` 判空降级，不能再将 Redis 缺失必然 AttributeError 当作当前事实；验收见 [部署说明](deployment.md)。

## 演进路径

> Phase A-D 保留为能力分组与依赖讨论。它们不是新任务授权，也不是先建齐基础设施再交付产品的强制顺序。已有实现先查 ALIGNMENT；活动范围与验收写入当前计划。

### Phase A 基础设施落地

目标：在产品需要时改善资源创建与会话访问边界，验证真实降级能力。

- `infrastructure/database.py`：共享 runtime 已实现，Container 接入归 DB-F05b
- `domain/ports/session_store.py`：会话专用普通数据端口（DB-F05a 已实现）
- `infrastructure/session_store.py`：PostgreSQL 适配器，逐操作事务与有界清理（DB-F05a 已实现）
- 缓存边界：评估已有 `_cache_*` 降级是否充分；独立 CachePort/NullCache 仅在职责与消费需求成立时采用
- `container.py`：维护已有装配根和资源关闭责任
- `SessionManager`：已迁出 SQL/ORM，注入 SessionStorePort；保留现有 Redis 缓存策略，不增建 CachePort

验收目标：装配根与基础设施工厂分离；会话 Store 事务边界可验证；无 Redis 时可持久化，数据库已知不可用时缓存不得伪装成功。上文通用 Repository 图为历史蓝图，数据库部分以 [DB-ADR-001](../../adr/infrastructure/database/2026-09-22-shared-database-foundation.md) 和[会话 Store](../infrastructure_doc/database_doc/session_store.md) 为当前契约，不实施通用 Repository。
进度和验证状态仅查 [ALIGNMENT.md](../ALIGNMENT.md)。

### Phase B 解耦改造

目标：C3、C4、C5、C6、C7、C8。

- `shared/events.py`：events 迁入 + 拆分
- `domain/ports/llm_gateway.py` + `tool_gateway.py`（StreamResult 迁入；token 计数并入 LLMGateway）
- `app/domain/agent/` 构造签名依赖抽象（`BaseAgent(llm: LLMGateway, tools: ToolGateway)`）
- LLMService / ToolService 实现端口；配置注入推广（AgentContext / 内置工具 / TaskService / LLMService Facade）
- 评估 ToolService 的现有职责拆分与 `deps.py` 薄解析；EmbeddingService 是否需要新入口取决于实际消费者

验收目标：领域依赖稳定端口、业务配置注入、共享异常/类型有明确归属，具体实现不被调用方越过 Facade 依赖。各项是否达成以 ALIGNMENT 与实际回归证据为准。

### Phase C 应用与编排层

目标：C9 部分 + 产品闭环 1-3。

- `application/chat/chat_service.py`：聊天预检、上下文、运行身份、Agent 创建与成果提交已从路由上移
- `application/task/`：TaskQueue（优先级 + 背压）/ WorkerPool / TaskState / TaskScheduler（扩展原 TaskService）
- `application/orchestration/orchestrator.py`：主拆 → 子并行 → 汇总
- 复用并核验既有 PlannerAgent/PlannerStrategy 与 PromptManager，设计其与应用层多任务编排的衔接；不得将已登记实现当空文件重写
- 出现第二种真实且同构的 Agent 创建用例后再评估 `application/factories/agent_factory.py`；`api/routes/task.py` + `agent.py`（提交/查询/进度 SSE）仍待产品需求

验收目标：异步任务受理闭环（`POST /tasks` → worker → `GET /tasks/{id}` → SSE 进度）；多 Agent 主从并行编排。是否已实现见 ALIGNMENT。

### Phase D 可观测性与业务域

目标：C9 剩余 + Yield RCA 产品。

- `platform/observability/`：metrics（Prometheus）/ tracing（OTel）/ audit / 日志迁入
- 按需求扩展 auth（JWT）/ rate_limit / correlation；error_handler 已有文档登记，先核验其映射而非从空文件新建
- 按产品需要评估 `domain/yield_rca/` 与 `application/yield_rca/rca_use_case.py`；复用已有文档描述的 5 个模拟 RCA 工具
- embedding / vector_store / memory 接线（RAG：search_historical_rca）；StructuredOutput 供报告结构化
- MemoryService 实现（短期注入 ContextManager + 长期向量 RAG）
- `app/eval/`：评测集 + 评测执行器 + 评估指标 + 基准报告（Agent 质量基线，CI 回归）

验收目标：全链路可观测三件套；鉴权 / 限流 / 错误码真实；Yield RCA 带证据链根因报告闭环；评测集闭环回归；C9 清零。是否已实现见 ALIGNMENT。

### 阶段依赖与验收

能力依赖应按具体任务验证：端口与装配支撑解耦，明确状态和取消责任后再扩展并行编排。产品顺序遵循 [产品闭环](product.md#里程碑与闭环)：受理与查询可演示 → 主从排查 → 按负载引入队列/背压。Phase A-D 不构成整阶段串行前置。

每个切片验收包括既有行为回归、该切片关键路径与失败路径验证；只在 ALIGNMENT 更新实现与覆盖状态。纯架构目标调整不伪造代码完成状态。

---

## 相关文档

- [product](product.md)（产品方向）
- [服务层说明](../application_doc/README.md)（服务层模块 + 实现目标 — 对标工业级）
- [agent 模块](../domain_doc/agent_doc/agent.md)
- [api 层说明](../api_doc/README.md)
- [config 模块](../config_doc/config.md)
- [logging 模块](../platform_doc/observability/logging.md)（全局日志框架）
- [error_handling 模块](../shared_doc/error_handling.md)（异常处理与传播约定）
- [class-design 模块](../shared_doc/class-design.md)（类的类型体系与实例形态）
- [LLM 层](../integration_doc/llm_doc/llm.md)
- [task 模块](../application_doc/task_doc/task.md)
- [tool 模块](../integration_doc/tools_doc/tools.md)
- [部署](deployment.md)
- [评测与评估](../eval_doc/evaluation.md)（Agent 质量基线，待规划）
- [安全](../platform_doc/security.md)（威胁模型 / 数据 / 工具 / 密钥）
- [可观测性](../platform_doc/observability.md)（日志 / 追踪 / 指标 / 告警）
