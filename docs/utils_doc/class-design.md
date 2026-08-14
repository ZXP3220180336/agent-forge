# 类的类型体系与实例形态

> **文档定位**：项目代码中各种「类」的设计模式归类 + 单例/实例形态分析。
> **适用对象**：任何需要理解或新增类的开发者 —— 先判断「这属于哪一类」，再决定「是否实例化、怎么实例化」。
> **内容来源**：对 `app/services/llm/`、`app/app_state.py`、`app/dependencies.py` 等代码的模式提炼。

---

## 📋 目录

- [为什么这些类不需要实例化](#为什么这些类不需要实例化)
- [六种类类型](#六种类类型)
- [判断口诀](#判断口诀)
- [单例的三种形态](#单例的三种形态)
- [AppState 容器模式与依赖注入](#appstate-容器模式与依赖注入)
- [三层单例体系](#三层单例体系)
- [实例方法 / 类方法 / 静态方法](#实例方法--类方法--静态方法)
- [常见陷阱](#常见陷阱)
- [相关文档](#相关文档)

---

## 为什么这些类不需要实例化

项目里 `ClientManager`、`RateLimiterManager`、`ReservationLimiterManager`、`StructuredOutput`、`CostTracker`、`StreamParser` 这些类都可以**直接类调用、不实例化**，但它们的原因各不相同：

| 类 | 不实例化的真实原因 |
|---|---|
| `StreamParser` / `CostTracker` / `StructuredOutput` | **无状态**，实例无意义（纯函数） |
| `ClientManager` / `ReservationLimiterManager` | **必须全局唯一**，实例化会分裂共享缓存 |

> **一句话记忆**：工具类不实例化是因为「无所谓」，管理器不实例化是因为「必须唯一」。

---

## 六种类类型

### 第一类：无状态工具类（`@staticmethod`）

**代表**：`StreamParser`、`CostTracker`、`StructuredOutput`

**特征**：所有方法都是 `@staticmethod`，**没有 `self` 也没有 `cls`**，完全不持有任何状态。

```python
class StreamParser:
    @staticmethod
    def parse_chunk(chunk) -> ParsedChunk: ...
```

**为什么不需要实例化**：它是一组「纯函数」的命名空间 —— 输入输出转换，无副作用、无缓存。实例化多个 `StreamParser()` 没意义，因为实例之间无差异。

**使用场景**：**纯计算/转换逻辑**，如「解析 chunk」「算成本」「提结构化输出」。这类逻辑放类里是为了**语义分组**（`StreamParser` 表明「这些解析函数属于流式解析」）。

**关键点**：`@staticmethod` 方法内部调用也是静态的（如 `CostTracker._find_price(model)`），不能用 `self`。

---

### 第二类：全局管理器类（`@classmethod` + `ClassVar` 缓存）

**代表**：`ClientManager`、`RateLimiterManager`、`ReservationLimiterManager`

**特征**：所有方法 `@classmethod`，持有**类级缓存**（`ClassVar[dict] = {}`），通过**懒加载**复用对象。

```python
class ClientManager:
    _instances: ClassVar[dict[str, AsyncOpenAI]] = {}

    @classmethod
    def get_client(cls, key) -> AsyncOpenAI:
        if key not in cls._instances:   # 懒加载：没有才创建
            cls._instances[key] = AsyncOpenAI(...)
        return cls._instances[key]      # 有就直接返回缓存
```

**为什么不需要实例化**：它要**全局共享一份状态**（连接池/限流桶）。如果实例化多个 `ClientManager()`，每个实例有自己的 `_instances`，连接池就分裂了 —— 违背「全局共享」意图。

**使用场景**：**全局唯一的管理器**，管理跨请求复用的资源（连接池、限流桶）。是单例的一种实现（比 `__new__` 单例更简洁，天然支持多 key 缓存）。

**关键点**：`@classmethod` 有 `cls`，能读写类属性。`ClassVar` 是「类变量」的正确声明方式。

---

### 第三类：有状态组件类（实例化 + 持有可变状态）

**代表**：`TokenBucket`、`CircuitBreaker`、`Reservation`、`SessionManager`、`ToolService`、`LLMService`

**特征**：有 `__init__`，每个实例持有**自己的**状态。

```python
limiter = ReservationLimiter(rpm=60, tpm=100000)   # 必须实例化！
await limiter.reserve(estimated_tokens=100)
```

**为什么必须实例化**：状态是**每实例独立**的。两个 `TokenBucket` 各有各的 token，互不影响。

**使用场景**：**需要多个独立副本**或有**运行时状态**的对象。`SessionManager`（每个会话管理器一个）、`CircuitBreaker`（每个熔断器独立开合状态）。

---

### 第四类：纯数据容器（`@dataclass`）

**代表**：`AgentContext`、`AgentResult`、`ParsedChunk`、`ToolCallDelta`、`RetryConfig`、`ToolResult`

**特征**：`@dataclass` 装饰，只有字段声明，自动生成 `__init__` / `__repr__` / `__eq__`。

```python
@dataclass
class ParsedChunk:
    reasoning_token: str | None = None
    message_token: str | None = None
```

**为什么不需要实例化**：它是**数据结构**不是行为。需要时 `ParsedChunk()` 创建一个。

**使用场景**：**数据传递** —— 函数间传递的结构化数据。

**注意**：dataclass 字段默认值是**可变容器**（list/dict）时，必须用 `field(default_factory=...)`；默认值是 `None` / 标量 / 不可变容器（tuple/frozenset）时，直接赋值即可。

---

### 第五类：数据契约（`pydantic.BaseModel`）

**代表**：`SendMessageRequest`、`CreateSessionRequest`、`CreateSessionResponse`、`Settings`

**特征**：继承 `pydantic.BaseModel`，实例化时**强校验**字段，支持序列化 / JSON Schema / 文档化。

**使用场景**：**API 请求/响应体 + 配置** —— 需要校验 + 文档化的边界数据。

```python
class SendMessageRequest(BaseModel):
    session_id: str
    message: str
    max_iterations: int = 10
```

**与 dataclass 的区别**：dataclass 是「存数据的轻量容器」，`BaseModel` 是「要校验 + 要文档的数据边界」。项目里 `ParsedChunk` 用 dataclass、`SendMessageRequest` 用 BaseModel，都是正确选择。

---

### 第六类：抽象基类（`ABC`）+ 继承体系

**代表**：`BaseAgent`（`ABC`）、`BaseTool`（`ABC`）

**特征**：抽象基类定义接口，子类实现（`ReActAgent`、`SearchTool` 等）。

**使用场景**：**多态** —— 不同策略/工具实现统一接口。

---

## 判断口诀

> **「一张表管理同类资源」→ 全局管理器（ClientManager）；「一个复杂对象持有异质状态」→ 模块级单例实例（AppState）。**

| 问题 | 判断 |
|---|---|
| 无状态、纯计算/转换？ | → 无状态工具类（`@staticmethod`） |
| 管理多个 key 的同类资源？ | → 全局管理器类（`@classmethod` + `ClassVar`） |
| 需要独立副本或有运行时状态？ | → 有状态组件类（实例化） |
| 只是数据结构？ | → 纯数据容器（`@dataclass`） |
| 需要校验 + 文档化的数据边界？ | → 数据契约（`BaseModel`） |
| 定义接口供多态实现？ | → 抽象基类（`ABC`） |

---

## 单例的三种形态

### 形态一：模块级单例实例

```python
# app_state.py 底部
app_state = AppState()
```

**原理**：Python 模块天然是单例 —— import 一次只执行一次类定义 + 实例化，之后所有 `from app.app_state import app_state` 拿到的是同一对象。

**适用场景**：一个复杂对象，有具名状态 + 实例方法。

---

### 形态二：全局管理器类

```python
class ClientManager:
    _instances: ClassVar[dict[str, AsyncOpenAI]] = {}
    @classmethod
    def get_client(cls, key): ...
```

**适用场景**：管理多个 key 的同类资源（连接池、限流桶）。

---

### 形态三：容器管理单例

```python
# app_state.py initialize() 里
self.session_manager = SessionManager(...)   # 统一创建
self.tool_service = ToolService()
```

**特征**：服务实例不是在各模块 `xxx = SessionManager()` 创建，而是由 `AppState` **容器**统一创建、持有、分发。

**为什么比模块级单例好**：
1. **集中创建 + 依赖注入**：`AppState` 统一组装（`SessionManager(redis, db)`），理清依赖图
2. **延迟初始化 + 生命周期管理**：启动才创建，关闭时统一清理
3. **测试友好**：可以替换 `app_state.session_manager` 为 mock，不影响路由

---

## AppState 容器模式与依赖注入

### 为什么 AppState 不用全局管理器写法

**核心区别：AppState 是「一个复杂对象」的实例，不是「多个资源的缓存表」。**

| 维度 | 全局管理器（ClientManager） | AppState |
|---|---|---|
| 结构 | `dict[str, X]` 一张表，按 key 取 | 具名属性（`redis` / `engine` / `session_manager`） |
| 状态 | 同类资源 | 异质服务（不同类型，各自独立属性名） |
| 方法 | 无实例方法，全 `@classmethod` | 有实例方法（`initialize`/`shutdown`） |
| 单例实现 | `ClassVar` 缓存 + classmethod | 模块级 `app_state = AppState()` |

**关键**：`AppState` 持有的是**不同类型**的服务引用（redis/engine/session_manager/tool_service），每个有独立属性名和类型，无法用 `dict[str, X]` 表达。改成 `@classmethod` 后 `self.redis` 全变 `cls.redis`，语义混乱。

### 依赖注入链路

```
路由层（FastAPI）
    │  Depends(get_session_manager)
    ▼
app/dependencies.py 的 get_session_manager()
    │  app_state.session_manager
    ▼
AppState.initialize() 统一创建的服务实例
```

```python
# dependencies.py
async def get_session_manager() -> SessionManager:
    if app_state.session_manager is None:
        raise RuntimeError("SessionManager 尚未初始化...")
    return app_state.session_manager
```

---

## 三层单例体系

```
AppState 容器（模块级单例实例）
    │  initialize() 时创建
    ├── session_manager ──┐
    ├── tool_service      ├─→ 路由经 get_xxx() 依赖注入获取
    ├── llm_service       │      （从 app_state 取，非自己 new）
    └── task_service ─────┘

ClientManager / ReservationLimiterManager   ← 全局管理器类（classmethod，路由不直接碰）
```

- **第一层（容器）**：`app_state` —— 模块级单例实例
- **第二层（服务）**：`SessionManager` 等 —— 容器管理的应用级单例，经 DI 分发
- **第三层（资源管理器）**：`ClientManager` / `ReservationLimiterManager`（及 `RetryHandlerManager`） —— 全局管理器类

各司其职，是工业级应用的典型依赖注入骨架。

---

## 实例方法 / 类方法 / 静态方法

| 方法类型 | 装饰器 | 首个参数 | 能访问 | 使用场景 |
|---|---|---|---|---|
| 实例方法 | 无 | `self` | 实例状态 | 有状态组件（`TokenBucket.acquire`） |
| 类方法 | `@classmethod` | `cls` | 类属性 | 全局管理器（`ClientManager.get_client`） |
| 静态方法 | `@staticmethod` | 无 | 无 | 纯函数（`StreamParser.parse_chunk`） |

```python
class Example:
    @staticmethod
    def pure(x): return x * 2          # 无状态

    @classmethod
    def manager(cls): return cls._cache # 管类状态

    def instance(self): return self.x   # 管实例状态
```

---

## 常见陷阱

### 1. 类属性可变默认值 → `ClassVar` 声明

```python
class ClientManager:
    _instances: dict[str, X] = {}   # ⚠️ Mutable default value 警告
    _instances: ClassVar[dict[str, X]] = {}  # ✅ 正确
```

`ClassVar` 显式声明「这是类变量」，告诉 linter 这个可变默认值是**有意共享的类状态**，规避警告且语义正确。

### 2. `field(default_factory=...)` 是 dataclass 专属

`field()` 只在 `@dataclass` 类里有意义。用在普通 class 的类属性上时，效果和 `= {}` 等价，但语义含糊。正确做法是用 `ClassVar`。

### 3. dataclass 可变默认值

```python
@dataclass
class Task:
    tags: list = []   # ❌ 所有实例共享同一个 list
    tags: list = field(default_factory=list)  # ✅ 每实例独立
```

- 默认值是**可变容器**（list/dict/set）→ 必须 `field(default_factory=...)`
- 默认值是 **`None` / 标量 / 不可变容器**（tuple/frozenset）→ 直接赋值

### 4. 普通 class 的 `self.x = []` 不是坑

```python
def __init__(self):
    self.tool_calls = []   # ✅ 每实例新建，无共享
```

「可变默认值陷阱」只存在于**默认参数**形式（`def f(x=[])` 或 dataclass 字段），`__init__` 内 `self.x = []` 每次都新建，安全。

---

## 相关文档

- [架构设计](../architecture.md)（分层与模块状态）
- [服务层说明](../service_doc/service.md)（各服务实例归属）
- [LLM 层](../service_doc/llm_doc/llm.md)（ClientManager / 限流器 / StreamParser 详解）
- [数据模型](../model_doc/model.md)（BaseModel 契约层）
