# Prompts 模块对外接口文档

> **对应代码**：`app/domain/prompts/`
> **文档定位**：模块公开契约与内部组件导航
> **更新日期**：2026-09-13
> **状态与映射**：[ALIGNMENT](../../ALIGNMENT.md)

## 模块定位与边界

Prompts 是领域层的提示词组装模块。它把调用方提供的目标、工具目录、证据、稿件、
问题清单和步骤记录组装为 LLM 可消费的字符串。当前直接生产调用方是
`ReflectionStrategy` 与 `PlannerStrategy`；Agent 通过推理策略间接使用本模块。

本模块承担：

- 维护系统、工具、Reflection 和 Planner 的提示词模板；
- 通过 `PromptManager` 提供稳定的组装入口；
- 在调用方提供 token 预算与计数器时形成领域语义缩减视图；
- 通过 `PromptTemplate` 提供通用字符串模板包装。

本模块不调用 LLM、工具或存储，不管理会话消息，不产生 usage、取消事件或 deadline
终态，也不计算 provider 的最终 wire payload。语义字段取舍属于 Domain；最终请求窗口
准入属于 Integration。分层依据见[语义上下文预算 ADR](../../../adr/domain/reasoning/2026-08-28-context-budget.md)
和[请求上下文预算 ADR](../../../adr/integration/llm/2026-09-06-request-context-budget.md)。

```text
ReflectionStrategy / PlannerStrategy
        │ 业务对象 + 可选 token 预算
        ▼
PromptManager（公开 Facade）
        ├── _reflection_payload.py（Reflection 语义投影）
        ├── _planner_payload.py（Planner 语义投影）
        └── templates/*.py（固定指令模板）
        │ 完整 prompt 字符串
        ▼
调用策略本地复查 → LLMGateway → Integration 最终请求准入
```

## 公开导出

包级公开入口定义在 `app.domain.prompts.__all__`：

| 符号 | 类型 | 契约 |
| --- | --- | --- |
| `PromptManager` | 无状态 Facade | 提供六个同步静态 builder；返回完整 prompt 字符串 |
| `PromptTemplate` | 字符串模板值对象 | 保存模板原文并通过 Python `str.format` 完成命名变量插值 |

`templates/` 常量与 `_reflection_payload.py`、`_planner_payload.py` 均不属于包级公开 API。
下划线组件只允许 `PromptManager` 在包内协作，不授权其他层绕过 Facade 调用。

## `PromptManager` 接口

所有方法均同步执行、无 I/O，成功时返回 `str`。

| 方法 | 业务输入 | 可选预算参数 | 返回及调用语义 |
| --- | --- | --- | --- |
| `build_system_prompt(tool_descriptions: str = "")` | 已格式化工具目录；空串表示不附加工具说明 | 无 | 返回 `SYSTEM_PROMPT`；目录非空时追加 `TOOL_FORMAT_PROMPT` |
| `build_reflection_critique_prompt(evidence, draft, *, max_tokens=None, count_tokens=None)` | 工具证据记录、当前结构化稿件 | `max_tokens`、`count_tokens` | 返回 Reflection 自查 user prompt |
| `build_reflection_refine_prompt(evidence, draft, issues, *, max_tokens=None, count_tokens=None)` | 工具证据记录、当前稿件、审查问题 | 同上 | 返回 Reflection 修订 user prompt |
| `build_planning_prompt(user_input, tool_descriptions, *, max_tokens=None, count_tokens=None)` | 用户目标、规划器可见的工具目录 | 同上 | 返回初始规划 user prompt；规划器只做工具 introspection，不获得工具调用权 |
| `build_planning_replan_prompt(goal, tool_descriptions, executed, failed_step, error, *, max_tokens=None, count_tokens=None)` | 目标、工具目录、执行记录、当前失败步骤及原因 | 同上 | 返回重规划 user prompt |
| `build_planning_summarize_prompt(goal, executed, *, max_tokens=None, count_tokens=None)` | 目标和步骤执行记录 | 同上 | 返回证据链报告汇总 user prompt |

Reflection 的字段优先级与缩减机制见
[Reflection payload 组件](reflection_payload.md)；Planner 的步骤、依赖和证据规则见
[Planner payload 组件](planner_payload.md)。具体结构化输出 schema 属于对应 reasoning
策略的契约，不由本模块定义。

### 公共预算语义

| 条件 | 行为 |
| --- | --- |
| `max_tokens` 与 `count_tokens` 同时提供 | 先计算固定模板开销，再把剩余容量交给对应 payload 组件生成语义视图 |
| 只提供其中一个参数 | 不启用 token 预算路径，使用该组件的兼容序列化行为 |
| `max_tokens < 0` | 动态内容容量按零处理；必须保留的最小业务骨架仍会返回 |
| 完整内容可容纳 | 不主动省略业务内容 |
| 需要缩减 | 返回带 `<omitted ... reason="context_budget"/>` 的视图；原始输入不变 |
| 最小骨架仍超过预算 | 仍返回最小骨架；本模块不删除必须事实，也不抛上下文异常 |

`max_tokens` 是本次 prompt 的领域候选预算，不等于 provider 模型窗口。调用策略必须对
最终组装字符串再次计数；仍超限时应在 SDK 调用前形成 Guard。即使本层结果在预算内，
Integration 仍会把 schema、工具定义和输出预留计入最终请求准入。

### 错误与终止契约

| 触发条件 | 本模块行为 | 调用方责任 |
| --- | --- | --- |
| 输入符合约定 | 返回 prompt 字符串 | 在真实请求前执行本地 Guard 与最终请求准入 |
| 输入对象形状错误 | `AttributeError`、`TypeError` 等编程错误直接传播 | 提供对应策略契约内的 `dict` / `list` 数据，不把编程错误当 LLM 失败降级 |
| 输入含不可 JSON 序列化对象 | JSON 序列化异常直接传播 | 在进入 Facade 前保证业务载荷可序列化 |
| `count_tokens` 回调失败 | 回调异常直接传播 | 注入符合 `Callable[[str], int]` 且结果非负的稳定计数器 |
| `PromptTemplate.format` 缺少变量 | Python `str.format` 的 `KeyError` 直接传播 | 提供模板要求的全部命名参数 |
| 取消、deadline 或成本超限 | 本模块不检测、不生成终态 | 由 reasoning Guard 和 LLM 生命周期组件处理 |

本模块没有资源所有权，也没有“部分成功”“返回 `None`”或异常转 SSE 的语义。

## `PromptTemplate` 接口

| 方法或属性 | 输入与默认值 | 返回及异常 |
| --- | --- | --- |
| `PromptTemplate(template: str)` | 模板原文 | 创建包装对象；不预编译、不校验占位符 |
| `format(**kwargs)` | 命名插值参数 | 返回 `self._template.format(**kwargs)`；格式错误按 Python 原生异常传播 |
| `raw` | 无 | 返回构造时保存的原始模板字符串 |

`PromptTemplate` 是包级公开能力，但当前 `PromptManager` 和生产策略直接使用模板字符串
常量，没有依赖该包装类。其实现与接线状态以 [ALIGNMENT](../../ALIGNMENT.md) 为准。

## 最小调用示例

通过包级公开入口构建系统提示词：

```python
from app.domain.prompts import PromptManager

tool_catalog = "- query_batch_yield: 查询指定批次的良率趋势"
system_prompt = PromptManager.build_system_prompt(tool_catalog)
messages = [{"role": "system", "content": system_prompt}]
```

使用领域端口提供的计数口径构建预算受限的规划提示词：

```python
from app.domain.ports.context_budget import ContextBudgetPort
from app.domain.prompts import PromptManager


def build_plan_prompt(context_budget: ContextBudgetPort) -> str:
    prompt = PromptManager.build_planning_prompt(
        "分析批次 B11 良率下降原因",
        "- query_batch_yield: 查询批次良率\n- query_equipment_alarm: 查询设备告警",
        max_tokens=4096,
        count_tokens=context_budget.count_tokens,
    )
    # 示例调用方仍须在真实 SDK 请求前对完整 prompt 做最终本地复查。
    return prompt
```

## 内部实现导航

| 组件 | 代码位置 | 职责 | 维护说明 |
| --- | --- | --- | --- |
| `PromptManager` | `manager.py` | 公开组装入口、固定模板开销计算和组件委托 | 本文 |
| `PromptTemplate` | `base.py` | 通用字符串模板包装 | 本文 |
| Reflection payload | `_reflection_payload.py` | evidence/draft/issues 的预算分配、优先选择和只读序列化 | [组件说明](reflection_payload.md) |
| Planner payload | `_planner_payload.py` | plan/replan/summarize 的分层投影和证据引用 | [组件说明](planner_payload.md) |
| 系统与工具模板 | `templates/system.py`、`templates/tools.py` | 通用 Agent 系统指令与工具说明 | 本文；模板正文以源码为唯一事实源 |
| Reflection 模板 | `templates/reflection.py` | 自查、修订及可选生成阶段 system 指令 | [Reflection 策略](../reasoning_doc/reflection.md) |
| Planner 模板 | `templates/planning.py` | 规划、重规划和证据链汇总指令 | [Planner 策略](../reasoning_doc/planner.md) |

`REFLECTION_SYSTEM_PROMPT` 当前已定义但未由 `PromptManager` 或生产策略接入；不能把它
视为当前 Reflection 生成阶段已经生效的约束。接线状态只在 ALIGNMENT 维护。

## 配置关联

本模块不读取 settings。`max_tokens` 与 `count_tokens` 由调用策略传入；生产中的
`max_context_tokens` 来源、默认值和装配方式见[配置参考](../../config_doc/config.md)。
`ContextBudgetPort` 的计数契约见[领域端口文档](../ports_doc/ports.md)。

## 相关文档

- [领域层说明](../README.md)
- [良率 RCA 业务对象与报告生命周期](../../project/product.md#良率-rca-的业务对象与报告生命周期)
- [Reflection payload 组件](reflection_payload.md)
- [Planner payload 组件](planner_payload.md)
- [ReflectionStrategy](../reasoning_doc/reflection.md)
- [PlannerStrategy](../reasoning_doc/planner.md)
- [语义上下文预算 ADR](../../../adr/domain/reasoning/2026-08-28-context-budget.md)
