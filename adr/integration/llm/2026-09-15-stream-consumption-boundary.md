# LLM-ADR-019：单流消费与整流策略分离

日期：2026-09-15。决定状态：已接受（用户授权执行 R3）。实现状态：已验证。
范围：`streaming_rectifier.py`、`stream_consumption.py` 及对应测试、说明。关联：[R3 归档](../../../docs/history/completed-work.md#refactoring-plan)。

## 背景与备选

`StreamingRectifier` 同时负责跨 attempt 的整流/续接策略，以及单个 provider 流的逐 chunk
读取、解析累积、看门狗竞争、接缝剥离和提前关闭。前者拥有重试预算、熔断、日志和
Reservation 结算，后者拥有单条响应流的短生命周期；两类变化原因和测试边界不同。

继续保留单文件会让流读取竞态与恢复策略相互遮蔽；新增消费类没有需要跨调用保留的独立状态；
把结算迁入消费组件会形成 response 与 Reservation 的双 Owner。因此采用一个包内函数入口，
仅提取单流生命周期，不建设新的执行框架。

工业级参照：Python `asyncio.wait` 提供并发等待多个任务并返回完成集合的原语，适合显式确定
chunk 与终止信号的判定顺序；异步生成器提供 `aclose()` 以触发清理。OpenAI Python SDK 的
异步流也在迭代器 `finally` 中关闭响应，说明未读完流必须有明确释放责任。

## 决定

- `stream_consumption.py` 只提供包内 `drain_stream`：显式接收 response、`StreamResult`、
  attempt 内 tool deltas、取消信号、绝对期限、首包/空闲阈值和可选续接前缀。
- 新模块内部负责 SDK chunk 解码后的结果累积、SSE 事件构造、四方等待、任务回收、接缝剥离
  以及非自然 EOF 时关闭 response；不依赖 `StreamingRectifier`、retry、request execution、
  limiter、Reservation 或 settings。
- 同刻竞争保持既有次序：先吸收已完成 chunk，再按 cancel、deadline 传播终止；正常 EOF 后
  同样复查终止；两者均未发生时，idle 看门狗才形成传输超时。事实被接管后、终止传播前不
  yield 新事件。
- `StreamingRectifier` 保留 attempt/续接循环、重试与退避预算、tool-call 最终合并、EOF 完成态
  守卫、熔断 feeding、日志、`active` 和 Reservation 唯一结算。看门狗配置值由它显式传入。
- `LLMService` Facade 以及所有产出子生成器事件的整流委托边界显式执行 `aclose()`；关闭公开
  流时，关闭动作逐层传播到当前 response，并在整流器兜底结算 Reservation 前完成。
- `StreamParser` 继续只负责 SDK chunk 到解析结果的转换；`LLMService` 仍只调用整流器，公开
  `LLMGateway` 契约不变。

## 后果与边界

单条 response 的读取和关闭可以独立验证，整流器代码集中表达恢复与结算策略。代价是增加一个
包内组件和显式参数列表；这些参数用来阻止消费模块反向读取整流器配置或上下文。

本决定不改变首包/空闲阈值、整流与续接次数、异常分类、接缝窗口、SSE 形状、usage 口径或
Reservation 语义。流消费模块不合并 tool calls，不提交日志，不选择是否整流，也不结算配额。

## 实现与验证

- 新增直接测试覆盖 chunk/终止同刻、cancel 优先级、EOF 后终止、idle、消费者 `aclose` 和
  跨 chunk 接缝；原整流、请求预算和 Facade 测试继续覆盖跨组件 Owner。
- R3 定向回归 89 项通过；全量回归 1213 项通过，仅有一条既有 Starlette/httpx 弃用提示；
  文档对齐、模块编译、差异检查与独立复核结果见 [R3 归档](../../../docs/history/completed-work.md#refactoring-plan)。
- 当前代码、文档和测试映射见 [ALIGNMENT](../../../docs/ALIGNMENT.md)。

## 关联资料

- [Python `asyncio.wait` 官方文档](https://docs.python.org/3/library/asyncio-task.html#asyncio.wait)
- [Python 异步生成器方法](https://docs.python.org/3/reference/expressions.html#asynchronous-generator-iterator-methods)
- [OpenAI Python SDK 异步流实现](https://github.com/openai/openai-python/blob/main/src/openai/_streaming.py)
- [流式整流组件说明](../../../docs/integration_doc/llm_doc/streaming_rectifier.md)
