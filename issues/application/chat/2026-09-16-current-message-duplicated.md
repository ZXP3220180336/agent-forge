# CHAT-001：当前用户消息重复进入模型上下文

状态：已修复。优先级：P1。发现来源：R6 调用链复核。范围：ChatService、ContextManager、SessionManager。

## 现象与影响

旧聊天链先提交当前 user 消息，再调用 `ContextManager.build_messages`。真实 SessionManager 立即从
数据库历史读回该消息，而 `build_messages` 又把 `user_message` 追加到末尾，导致同一请求在发给
LLM 的 messages 中出现两次。既有测试的会话替身始终返回空历史，未覆盖这一持久化可见性。

## 根因与方案

持久化和上下文组装都把当前消息视为自己的输入，但没有跨边界身份。按文本去重会误删用户连续
发送的相同内容；改成先组装再持久化会改变长流开始前已接收消息的提交语义。

采用持久化快照方案：`add_message` 返回严格为正的当前消息 ID，`build_messages` 将该 ID 传给
`SessionManager.get_messages`，历史查询只读取 `id < current_message_id` 的消息，再把当前输入
追加一次。该边界同时排除当前行及并发期间稍后提交的兄弟消息，使每个运行看到自己提交时的历史
快照，也不影响此前内容相同的消息。数据库未返回有效主键时直接失败，避免 `0` 退化成错误快照。
历史查询取最早 N 条的既有分页行为不属于本问题。

## 实施与验证

失败测试先观察到当前文本出现 2 次；修复后只出现 1 次。ContextManager 单元测试同时验证快照 ID
被传至数据访问边界，ChatService 测试验证同会话兄弟运行的消息边界与无效主键失败，聊天闭环测试
验证保存、上下文与最终回复仍按原顺序工作。实际测试结果见
[R6 评审](../../../docs/todo.md#refactoring-plan)。

## 关联记录

- [CHAT-ADR-001：聊天用例与单次运行 Owner 边界](../../../adr/application/chat/2026-09-16-chat-run-owner.md)
- [ChatService 组件说明](../../../docs/application_doc/chat_doc/chat.md)
