# create 已启动后的普通异常错误退回 reservation（LLM-048）

状态：已修复。优先级：P1。发现来源：ADR-003 生命周期复审。范围：`llm_service`。

## 现象与影响

`_budget_guarded_call` 已调度 SDK create task 后，普通传输异常和 Provider 401 响应仍调用
`Reservation.cancel()` 全额退回 RPM/TPM，并断言“请求未发出”。401 已证明请求到达 Provider；
超时或断连也不能证明远端没有执行。退款会虚增客户端可用配额，重试时放大 429 风险。

## 根因与修复

旧状态机把异常类型误当作请求是否启动的证据。按照 G0-7、R2 和
[ADR-003](../../../adr/2026-09-12-sdk-call-guard-response-commit.md)，结算依据改为 SDK create
是否已经调度：启动前终止才 `cancel()`；启动后的普通异常以 `settle(None)` 保守关闭
reservation。错误分类、retry 和 fallback 仍由可靠性层决定。

`test_generate_normalizes_openai_401_to_llm_api_error` 同时断言原异常链、单次 settle 和零
cancel。完整验证见[完成记录](../../../docs/history/completed-work.md)。

## 教训

异常类型描述传输结果，不描述远端副作用是否发生；reservation 结算必须依据调用阶段和事实
确定性，不能依据是否取得完整响应。
