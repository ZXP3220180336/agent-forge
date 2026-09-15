# 单流消费组件说明

> 对应代码：`app/integration/llm/stream_consumption.py`
> 文档定位：LLM 模块内部单个 provider 流的消费与资源释放契约
> 更新日期：2026-09-15
> 状态与映射：[ALIGNMENT](../../ALIGNMENT.md)

## 定位与职责

`stream_consumption` 接管一个已经成功创建的 provider 流，逐 chunk 读取并累积到调用方持有的
`StreamResult`，同时产出 SSE 事件。它管理单流看门狗、读取任务回收、续接接缝和提前关闭。

该组件不决定是否整流或续接，不拥有 retry、fallback、熔断、日志或 Reservation。合法调用方
是 `StreamingRectifier`；模块外调用者仍经 `LLMService` Facade。

## 内部协作契约

| 符号 | 输入与输出 | 调用方责任 |
| --- | --- | --- |
| `drain_stream(...)` | 接收 response、result、attempt 内 tool deltas、控制信号、看门狗阈值和可选接缝前缀；异步产出 SSE | 整流器为每个 attempt 创建 tool deltas，EOF 后决定是否合并，并处理异常、日志与结算 |
| `StreamResult` | 在 chunk 被解析后原位累积 content、reasoning、finish、refusal 和 usage | 由 Facade 创建、整流器持有；消费组件不替换对象或提交终态 |
| `tool_deltas` | 只累积当前 attempt 的工具调用增量 | 仅正常 EOF 后由整流器合并，失败 attempt 不污染最终 tool calls |
| `seam_prefix` | 续接 create 成功前已产 content 的快照；`None` 表示普通流 | 消费组件只剥离 message token 的有限首部重叠，不改变其他字段 |

## 竞争顺序与事实接管

每次读取让 `anext(stream)`、cancel、deadline 和 idle 看门狗竞争：

```text
已有 chunk 完成
  → 解析并接管 content / reasoning / tool delta / finish / refusal / usage
  → cancel 复查
  → deadline 复查
  → 允许产出本 chunk 的 SSE

没有 chunk 完成
  → cancel 或 deadline 类型化终止
  → 均未命中且 idle 到期时抛 TimeoutError
```

chunk 与终止同刻完成时，结果事实保留，但终止复查发生在 yield 前，因此不会在终态后继续向
消费者提交事件。EOF 后也复查取消与期限，保持同一优先级。

## 资源责任

| 出口 | response | Reservation |
| --- | --- | --- |
| 正常 EOF | SDK 自然关闭，组件不重复 close | 整流器按实际 usage 结算 |
| 读取异常、idle、取消或期限 | 组件尽力调用 response `close()` | 整流器完成中断结算与日志 |
| 消费者 `aclose()` | 各生成器委托边界显式向下传播关闭，单流 `finally` 主动 close | response 关闭完成后，整流器外层 `finally` 保守结算未终态 Reservation |
| close 普通异常 | 记录警告，不覆盖原始终止或传输异常 | 结算责任不变 |
| close 被硬取消 | 允许 `CancelledError` 传播 | 整流器仍通过外层 `finally` 收敛 Reservation |

每轮创建的 chunk 与 abort 任务都在 `finally` 中 cancel + gather，避免后台读取或终止等待泄漏。
LLM Facade、整流主流、放弃路径和续接路径使用显式异步关闭上下文包住子生成器，不能依赖
事件循环稍后的异步生成器 finalizer 释放当前 response。

## 接缝与解析边界

续接流的 content 首部按既有 64 字符窗口与 `seam_prefix` 尾部比较：部分重叠被剥离，纯重放
在 EOF 丢弃，超过窗口仍无法判明时按新内容放出，避免无限缓冲。reasoning、tool delta、usage、
finish 和 refusal 不参与接缝处理。

`StreamParser` 仍负责 SDK chunk 解码；本组件负责将解析结果写入 `StreamResult` 并构造 SSE。
是否已经产出、能否整流或续接由整流器从累积结果和 attempt 局部 tool deltas 判定。

## 验证入口

- `tests/unit/test_stream_consumption.py`：直接覆盖事实优先级、EOF/idle、提前关闭和接缝。
- `tests/unit/test_streaming_rectifier.py`：覆盖恢复策略、EOF 完成态和 Reservation Owner。
- `tests/unit/test_llm_request_budget.py`：跨层覆盖迟回流、关流—结算—日志顺序与硬取消。
- `tests/unit/test_stream_rectify.py`：经 Facade 覆盖整流/续接和配额调用次数。

实际状态见 [ALIGNMENT](../../ALIGNMENT.md)。设计取舍见
[LLM-ADR-019](../../../adr/integration/llm/2026-09-15-stream-consumption-boundary.md)。

## 相关文档

- [流式整流](streaming_rectifier.md)
- [流式解析](streaming.md)
- [执行控制](execution_control.md)
- [请求执行](request_execution.md)
