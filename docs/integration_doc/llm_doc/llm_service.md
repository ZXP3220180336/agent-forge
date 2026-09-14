# LLMService 编排说明

> 对应代码：`app/integration/llm/llm_service.py`
> 文档定位：公开 Facade 的通道编排、响应接管与结果边界
> 更新日期：2026-09-15
> 状态与映射：[ALIGNMENT](../../ALIGNMENT.md)
> 对外接口契约见 [llm.md](llm.md)；请求计划与真实 provider 调用见 [request_execution.md](request_execution.md)。

## 定位与职责

`LLMService` 是 LLM 集成层唯一公开 Facade，实现 `LLMGateway` 的生成、结构化生成、成本查询和 token 计数入口。它负责选择通道、连接 `request_execution`、接收响应或流、交给解析/整流组件，并在公开边界翻译异常和提交最终结果。

请求参数构建、预算准入、Reservation 预留和 provider `create` 的阶段语义集中在 [请求执行组件](request_execution.md)，本文不重复其状态机。

## 通道编排

```text
LLMGateway
   → LLMService.async_generate
       → request_execution.build_request_plan
       → StreamingRectifier.rectified_stream
       → SSE 事件 / StreamResult / 类型化取消或 deadline 异常

LLMGateway
   → LLMService.generate
       → request_execution.build_request_plan
       → RetryHandler.execute
       → StreamParser.parse_non_stream
       → usage 结算、观测和最终 Guard

LLMService.generate_structured
   → StructuredOutput.extract
       → LLMService.generate
```

`async_generate` 保留流式整流、半流续接和部分结果语义；`generate` 负责非流式响应解析、最终 Reservation 结算、观测隔离及返回前的取消/deadline 复查。`generate_structured` 只负责结构化降级链的 Facade 委托，schema 和 JSON 转换见 [结构化输出说明](structure.md)。

## 公开结果边界

- provider 响应或流由通道先接管，再交给解析器或整流器；已取得的 usage 和部分内容不能因终止被抹除。
- 业务取消和 deadline 在 Facade 统一翻译为 shared 异常；不把控制终止折成普通可恢复错误或取消 SSE。
- 非流式解析失败、最终结算失败和硬取消沿既有异常契约传播；非关键事件日志使用有界 best-effort，不覆盖主业务终态。
- 返回前不再启动新的业务请求。重试、fallback 和续接的每笔真实调用均由请求执行入口重新准入。

## 组件关系

| 组件 | Facade 侧责任 |
| --- | --- |
| `request_execution` | 提供一次调用的请求计划和每笔真实 create 的执行边界 |
| `RetryHandler` | 保护 create 阶段的重试、熔断和 fallback 决策 |
| `StreamingRectifier` | 流式读取、整流、续接和流内结果接管 |
| `StreamParser` | 非流式响应解析 |
| `StructuredOutput` | 结构化输出的多级降级与调用计数 |
| `CostTracker` / `TiktokenTokenCounter` | 成本与 token 计数代理 |
| `errors` / `execution_control` | 错误归一、控制信号和公开异常翻译 |

内部组件不是外部替代入口；跨层调用仍经 `LLMGateway` 或 `LLMService`。

## 验证入口

Facade 接线、异常翻译、流式与非流式结果边界见 `tests/unit/test_llm_service.py`；请求执行生命周期见 `tests/unit/test_llm_request_budget.py`；流式整流见 `tests/unit/test_streaming_rectifier.py`。实现状态以 [ALIGNMENT](../../ALIGNMENT.md) 为准。

## 相关文档

- [LLM 模块接口](llm.md)
- [请求执行组件](request_execution.md)
- [结构化输出](structure.md)
- [流式整流](streaming_rectifier.md)
- [LLM-ADR-018](../../../adr/integration/llm/2026-09-15-request-execution-boundary.md)
