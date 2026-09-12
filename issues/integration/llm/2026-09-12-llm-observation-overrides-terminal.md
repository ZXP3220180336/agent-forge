# LLM 非关键日志覆盖调用终态（LLM-049）

状态：已修复。优先级：P1。发现来源：G0-6 代码符合性审查。范围：logger、llm_service、
streaming_rectifier。

## 现象与影响

`fill_llm_event_fields` 直接等待 `log_event_async`。日志抛错或永久阻塞时会覆盖已经取得的 SDK
成功、原始传输错误、cancel/deadline；非流式最终 Guard 后还存在日志 await。旧
LLM-046/REASON-014 测试曾把日志 TimeoutError 传播到 ReAct UNKNOWN 固化为现状。

## 根因与修复

结算和日志相邻，但没有按业务重要性分界。按照 G0-6、R6 和
[ADR-003](../../../adr/2026-09-12-sdk-call-guard-response-commit.md)：reservation/usage 结算仍是
必要责任；`llm_call` 日志在共享入口使用私有短期限并隔离普通观测异常；外层硬取消继续传播；
非流式先完成有界观测，再做最终 Guard 并同步返回；流式 EOF 完成态继续禁止重发。

新增日志异常/挂起、非流式日志期间取消测试，并把旧日志 TimeoutError 用例改为验证不覆盖已
完成流。完整验证见[完成记录](../../../docs/history/completed-work.md)。

## 教训

同一个 finish helper 内的结算和日志不具有相同错误语义；只能隔离非关键观测，不能用宽泛
catch 连同账务失败一起吞掉。
