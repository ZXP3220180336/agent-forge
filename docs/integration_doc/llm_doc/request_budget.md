# 请求预算闸说明文档

> **对应代码**：`app/integration/llm/request_budget.py`
> **更新日期**：2026-09-06
> **职责**：在 provider 网络调用前，依据最终请求与模型窗口配置拒绝无法容纳的请求
> 状态与验证见 [ALIGNMENT](../../ALIGNMENT.md)。
> **配套**：由 `LLMService` 的真实请求入口（`_budget_guarded_call`，预算准入 → 限流闭环，职责独立于限流）调用；模型窗口配置由装配根注入

## 定位与职责

`RequestBudgetGuard` 校验最终请求的输入 token 估算值是否落在可用输入额度内。它处理
messages、工具定义、`response_format` 与输出预留，不裁剪会话历史；历史的语义取舍属于
`ContextManager`。

## 接口契约

| 符号 | 签名 | 返回 / 行为 |
| --- | --- | --- |
| `RequestBudgetConfig` | `(context_window_tokens, safety_margin_tokens)` | 单个 `model_key` 的窗口能力和保守余量；非法组合（窗口非正整数 / 余量不在 `[0, window)`）构造即抛 `ParameterValidationError` |
| `RequestBudgetResult` | `(input_tokens, input_budget, max_tokens)` | 本次请求的客户端估算，供日志和测试使用 |
| `RequestBudgetGuard.validate` | `(model, request)` | `max_tokens` 非正整数抛 `ParameterValidationError`；超限抛 `ContextWindowExceededError`；否则返回 `RequestBudgetResult`（模型键由 guard 构造时绑定） |
| `RequestBudgetManager.register_config` | `(configs)` | 整体替换按 `model_key` 索引的运行期配置并重建缓存实例（内部调 `reset`） |
| `RequestBudgetManager.reset` | `()` | 清空按键缓存的 guard，保留已注入配置（测试隔离复用） |
| `RequestBudgetManager.get` | `(model_key="main")` | 获取按键缓存的 guard；缺失键使用默认配置 |

可用输入额度为：

```text
context_window_tokens - max_tokens - safety_margin_tokens
```

计数对象是移除 `model`、`max_tokens` 与流式控制字段后的最终请求参数的稳定 JSON 序列化，
再加固定协议余量。计数是客户端保守估算，provider tokenizer 是最终事实源。

## 行为边界

| 场景 | 行为 |
| --- | --- |
| `input_tokens > input_budget` 或额度为负 | 在网络调用前抛 `ContextWindowExceededError`，不会调用 provider |
| `tools` / `response_format` 存在 | 与 messages 一并进入序列化估算（sampling / metadata 等非输入字段不计） |
| 流式整流、fallback、结构化降级或扩大输出重试 | 每次实际调用重新进入校验 |
| 未注册的 `model_key` | 采用配置参考中定义的默认窗口和安全余量 |
| `max_tokens` 非正整数（None/负数/0/bool/str/float） | 抛 `ParameterValidationError`，不发起计数 |
| 流式 create / 续接链命中超限 | 异常上抛（不折「LLM 调用失败」事件），由领域层终结为 `CONTEXT_EXCEEDED` |
| 会话历史需要裁剪或摘要 | 不在本组件处理，交由 `ContextManager` 的语义预算策略 |

## 配置项


配置键的完整定义与默认值见 [配置参考](../../config_doc/config.md)；本节仅记录与本组件相关的行为。

| 配置 | 说明 |
| --- | --- |
| `LLM_MAIN_CONTEXT_WINDOW_TOKENS` | `main` 模型窗口 |
| `LLM_REASONING_CONTEXT_WINDOW_TOKENS` | `reasoning` 模型窗口 |
| `LLM_FAST_CONTEXT_WINDOW_TOKENS` | `fast` 模型窗口 |
| `LLM_FALLBACK_CONTEXT_WINDOW_TOKENS` | `fallback`（备用模型）窗口：启用 `LLM_FALLBACK_MODEL_ID` 时按该模型官方窗口填写 |
| `LLM_CONTEXT_SAFETY_MARGIN_TOKENS` | 所有模型键共用的保守余量 |

> 默认窗口值不宣称适配所有模型：更改任一档 `model_id` 时须同步其 `_CONTEXT_WINDOW_TOKENS`
> （禁止按模型名猜测窗口）。完整配置说明见 [config.md](../../config_doc/config.md)。

## 测试状态

[test_request_budget.py](../../../tests/unit/test_request_budget.py) 覆盖工具与 schema 计入、窗口内请求、超限拒绝、非法配置与非法 max_tokens 校验、manager reset 与默认键兜底、sampling 不计与特殊 token 拼写；[test_llm_service.py](../../../tests/unit/test_llm_service.py) 与 [test_llm_request_budget.py](../../../tests/unit/test_llm_request_budget.py) 覆盖超限零 provider 调用与各通道契约，熔断 fallback 按独立 `fallback` 键窗口校验（含「主窗可容 / 备用窗拒绝」正反例，以及 CLOSED/HALF_OPEN 主失败后 fallback 超限直抛不包主错误）；整流器 / ReAct 测试覆盖流式与续接链超限上抛后终结为 `CONTEXT_EXCEEDED`。

## 设计决策

- [Provider 请求上下文准入](../../../adr/integration/llm/2026-09-06-request-context-budget.md)：语义预算与 provider 请求准入分离，最终校验留在 Integration。

## 相关文档

- [LLM 模块接口](llm.md)
- [token_counter.md](token_counter.md)
- [语义上下文预算 ADR](../../../adr/domain/reasoning/2026-08-28-context-budget.md)
