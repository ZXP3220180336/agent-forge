# 请求执行组件说明

> 对应代码：`app/integration/llm/request_execution.py`
> 文档定位：LLM Facade 内部请求计划与 provider 调用生命周期
> 更新日期：2026-09-15
> 状态与映射：[ALIGNMENT](../../ALIGNMENT.md)

## 定位与职责

`request_execution` 是 `LLMService` 的内部请求执行边界。它负责把一次流式或非流式调用的配置组装成请求计划，并为每笔真实 provider `create` 执行预算准入、Reservation 预留、取消/deadline 控制和调用阶段结算。

它不负责公开 API、响应解析、流读取、业务结果提交、最终 usage 结算或事件日志；这些责任由 `LLMService`、`StreamingRectifier` 或对应通道接管。调用方不得绕过 `LLMService` 将本模块当作外部 Facade。

## 内部协作契约

| 符号 | 输入与输出 | 协作方责任 |
| --- | --- | --- |
| `build_request_plan(...)` | 接收模型、消息、工具、预算、取消和期限配置；返回 `_RequestPlan` | `LLMService.async_generate` / `generate` 显式传入配置，并消费计划中的闭包与 `active` |
| `_RequestPlan` | `retry`、共享 `active`、`ctx`、主请求、fallback、续接闭包和事件字段 | 通道将 `call_fn` 交给重试/整流，将响应和 usage 交回原 Owner |
| `_CallContext` | 保存 client、token 估算、Reservation 容器、取消信号和绝对 deadline | 同一 Facade 调用内的主请求、fallback、续接共享；不跨调用复用 |
| `_budget_guarded_call` | 执行单笔真实调用；返回 provider 响应或传播控制/传输异常 | 每次 attempt 都重新进入；调用后由通道根据实际 usage 完成最终结算 |

## 请求计划

计划构建阶段一次完成：

1. 组装 provider `kwargs`。流式请求附加 `stream_options.include_usage`，非流式请求可附加 `response_format`。
2. 取得 client 和 retry handler，并用 `TiktokenTokenCounter` 估算 prompt token。
3. 根据 `adaptive_reserve` 选择 `prompt_tokens` 或 `prompt_tokens + max_tokens` 的预留形态。
4. 构造主请求、同 provider fallback 和流式续接闭包。fallback 使用独立的 `"fallback"` 预算键与配额池，续接沿用主 `model_key`。

请求计划只保存一次调用内共享的可变 `active` 容器。新的 attempt 会替换其中当前 Reservation；最终结算责任仍由响应通道持有。

## 单笔请求生命周期

`_budget_guarded_call` 的顺序固定为：

```text
调用前取消/deadline 检查
  → 最终请求上下文预算校验
  → 受控 reserve 等待
  → reserve 返回后再次检查
  → provider create 受控等待
  → 将 Reservation 责任交给调用方或在本层收敛
```

| 阶段与事实 | 行为 |
| --- | --- |
| 预算拒绝，尚未 reserve | 抛出预算错误，不申请配额、不调用 provider |
| reserve 等待被取消或到期 | 由限流器负责回收已扣条目，传播控制信号 |
| 已得 Reservation，create 尚未启动即终止 | `cancel()` 全额退回并移出 `active`，不发起请求 |
| create 已调度后终止、硬取消或普通传输异常 | `settle(None)` 保守收敛；不据本地无响应断言远端未执行 |
| create 返回响应或迟回值 | 返回给通道 Owner；由其按实际 usage 或未知 usage 完成最终结算 |

`settle(actual)` 只退还未使用的 TPM 差额，RPM 不退。请求执行层不重复结算，也不把迟回响应丢弃后宣称请求未发生。

## 主请求、fallback 与续接

- 主请求和续接沿用调用方的 `model_key`，每次真实 attempt 都重新进行预算校验和 Reservation 预留。
- fallback 只替换模型 ID，复用主 client；预算和限流使用独立的 `"fallback"` 键，避免把备用模型的窗口和配额误算到主模型。
- 续接闭包只在流式且配置允许时创建，追加已有文本作为 assistant prefix；续接失败由整流器按尽力而为契约放弃，不改变主通道的终态。

## 边界与不变量

1. 入口组装在通道异常处理外完成，配置错误和 token 计数错误保持 fail-fast。
2. 每笔真实 provider 调用都必须经过同一入口；不能由重试、fallback 或续接路径绕过预算和限流。
3. create 启动后不能因为本地取消或断连而全额退款；未知远端副作用必须保留结算责任。
4. `active` 只表示当前调用链的 Reservation 所有权，不是全局并发计数器；同一 Reservation 只能由一个最终 Owner 结算。

## 验证入口

重点测试位于 `tests/unit/test_llm_request_budget.py` 和 `tests/unit/test_llm_service.py`：预算拒绝、reserve/create 取消竞态、fallback 独立配额、迟回响应、硬取消和主/备用/续接调用次数。实现状态以 [ALIGNMENT](../../ALIGNMENT.md) 为准。

## 设计依据

- [LLM-ADR-018：请求执行边界与 Facade 编排分离](../../../adr/integration/llm/2026-09-15-request-execution-boundary.md)
- [执行控制说明](execution_control.md)
- [请求预算说明](request_budget.md)
- [限流说明](limiter.md)
- [流式整流说明](streaming_rectifier.md)
- [LLMService 编排说明](llm_service.md)
