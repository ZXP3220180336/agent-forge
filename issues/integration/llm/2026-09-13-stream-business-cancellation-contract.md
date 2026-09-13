# LLM-050 流式业务取消公开契约不一致

> **状态**：✅ 已修复（2026-09-13）
> **优先级**：P1（同一业务信号产生不同公开终态）
> **发现来源**：C-12 流式续接取消路径复核
> **范围**：`streaming_rectifier` / `llm_service` / `LLMGateway` / ReAct 消费边界
> **关联**：[请求生命周期 ADR](../../../adr/integration/llm/2026-09-06-request-context-budget.md) · [整流器说明](../../../docs/integration_doc/llm_doc/streaming_rectifier.md)

## 现象与影响

同一个调用方提供的 `cancel_event` 会因命中时刻不同而产生两种公开结果：整流入口、读取期
和多数放弃路径把取消折成 SSE error 事件并正常结束 async generator；续接退避中的
`_StreamCancel` 则可能穿透到 `LLMService`，变成 `LLMCancelledError`。

调用方因此无法只按一个契约收尾：若同时兼容“正常结束 + 取消 SSE”和“抛类型化异常”，
ReAct 可能重复提交取消事件或把正常 generator 结束误当成功；若只处理其中一种，取消发生
在另一时刻时就会被错误分类。把取消写入 `StreamResult.error` 还会将执行控制事实伪装成
provider 失败，混淆熔断、重试与上层错误语义。

## 可复现条件与证据

- `cancel_event` 在首次 attempt 前置位：旧实现由整流器生成取消 SSE 后返回。
- `cancel_event` 在流读取、整流退避或 reserve/create 后置复查期间置位：多数旧分支仍返回
  取消 SSE，但资源收尾路径各异。
- `cancel_event` 在续接退避或续接 create 中命中：私有 `_StreamCancel` 可直接离开整流器，
  Facade 翻译为 `LLMCancelledError`。

修复前红测覆盖了入口、reserve 后取消、迟回流和续接退避，证实公开结果取决于取消时刻。

## 根因

`StreamingRectifier` 同时承担了两种责任：处理底层流与 reservation 的所有权，又决定面向
用户的取消 SSE。后续新增受控等待时，私有终止信号自然沿调用栈传播，而旧分支仍调用
`_cancel_exit`，导致“控制信号”和“传输事件”两套终止协议并存。

SSE 是流数据协议，适合表达已经提交的模型增量和 provider 失败；业务取消属于调用控制，
其最终文案、错误码与 done 事件应由拥有 Agent 运行终态的领域编排提交。Integration 只能
保证停止新副作用、接管已发生事实并完成资源收尾。

## 方案与取舍

采用单一公开契约：

1. `cancel_event` 命中后禁止整流、续接、retry 或 fallback 发起新请求。
2. 已取得 `Reservation`、SDK stream 或 usage 的路径先按阶段完成 cancel/close/settle；清理
   责任仍留在拥有资源的 Integration 组件。
3. 整流器所有业务取消出口抛携可得 usage 的 `_StreamCancel`。Facade 统一翻译为 shared
   `LLMCancelledError`，不生成取消 SSE，不写 `StreamResult.error`。
4. 已经产出的 content、reasoning 与 usage 是不可抹除事实，继续保留在调用方传入的
   `StreamResult`；异常同时携带可得 usage，供仅观察异常的上层归账。
5. 外部 task 硬取消继续传播 `asyncio.CancelledError`；整体业务期限继续传播
   `LLMDeadlineExceededError`。三种终止来源不合并。
6. ReAct 独占取消终态的 SSE 与 done 提交责任，避免 Integration 与 Domain 双写终态。

未采用“把续接退避也改成取消 SSE”：该方向会保留双层终态所有权，并要求所有非 ReAct
调用方解析特定文案才能识别取消。也未给 helper 增加 SSE 回调，因为执行控制原语不应依赖
传输协议。

## 工业级参照

- [Python asyncio 任务取消](https://docs.python.org/3/library/asyncio-task.html#task-cancellation)：
  取消通过异常传播，协程用 `try/finally` 完成清理后通常继续传播；这支持“先收尾、后传播
  控制信号”，并将外部 task 取消与业务事件分开。
- [gRPC Cancellation](https://grpc.io/docs/guides/cancellation/)：取消用于终止进行中的 RPC，
  服务端需要停止后续工作并释放资源；取消本身不等于业务响应成功或 provider 错误。
- [WHATWG Server-sent events](https://html.spec.whatwg.org/multipage/server-sent-events.html#server-sent-events)：
  SSE 定义服务器向客户端发送事件的传输通道。最终 Agent 取消事件由拥有运行状态机的一层
  统一提交，可避免多个内部组件各自生成终态事件。

## 实施记录

| 文件 | 改动 |
| --- | --- |
| `app/integration/llm/streaming_rectifier.py` | 移除 `_cancel_exit` 及仅包装构造的取消辅助函数，各取消点在既有资源收尾后直接抛携 usage 的 `_StreamCancel`；保留 result 已获事实，不写 error，不生成 SSE |
| `app/integration/llm/llm_service.py` | 明确 Facade 以 `translate_abort` 统一输出 `LLMCancelledError`，并记录三类终止边界 |
| `app/domain/ports/llm_gateway.py` | 明确 `async_generate` 的业务取消、deadline、硬取消与 `StreamResult` 事实保留契约 |
| 测试 | 更新旧取消 SSE 断言；新增流读取期 content/usage 接管、关流、实际结算与 Facade 异常验收 |
| 文档 | 同步 LLM 总览、Facade、整流器、端口说明和请求生命周期 ADR；历史问题增加后继指针 |

## 验证结果

- `tests/unit/test_streaming_rectifier.py`、`test_stream_rectify.py`、`test_llm_request_budget.py`
  已覆盖入口、reserve、create 迟回、chunk 读取、整流/续接退避和续接 create 的一致出口。
- 执行控制、Facade、整流与 ReAct 组合回归：210 项通过。
- 全量 `uv run pytest -q`：985 项通过；唯一告警为既存 Starlette/httpx 弃用提示。
- `scripts.verify_alignment` 通过（包含当前仓库 Markdown 链接检查）；`git diff --check` 通过。

## 可复用教训

- 控制平面终止与数据平面事件必须只有一个公开归属；底层资源拥有者负责清理，上层运行
  状态机负责最终用户事件。
- 异常既是控制信号也是事实所有权移交凭证。统一异常类型时必须同时验证 reservation、
  stream 与 usage 的单次接管，不能只改异常断言。
- “取消非失败”需要落实到 `StreamResult.error`、熔断 feeding 和终态事件三处，不能只改文案。
