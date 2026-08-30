# ReAct 断点续跑 / 持久化（checkpointer）升级路径

> 日期：2026-08-30 ｜ 层级：domain + application + infrastructure ｜ 状态：🔶 预留（升级路径，未实现）

## Context

- 对标增强项 #19 ❌：ReAct 循环无断点续跑。长任务（多轮工具 + 推理）中途被超时 / 取消 / **成本上限停机** / 进程重启打断后，已完成轮次的工具调用、上下文、累计 token 全部丢失，只能从头重跑（重复付费、副作用重放）。
- 工业级参照（LangGraph checkpointer）：将 Agent 运行状态快照到持久层（内存 / Redis / DB），支持在 checkpoint 处恢复；粒度分**会话级**（整段对话可重放）与**循环级**（每轮 LLM 调用后落点）。
- 与 #20 成本上限是同一「长任务经济性」问题的两面：成本停机后若无 checkpoint，恢复 = 重跑全部（再次付费），断点续跑才有实际意义。
- 当前约束：单用户本地、单进程、无多 Agent 编排；会话消息已持久化（SessionManager / Redis / DB），但「运行中 Agent 内部状态」（迭代计数 / 工具执行记录 / 累计 usage）未持久化。工具副作用不可原子回滚——恢复时对已执行工具是否重放是工业界难点，单用户场景收益低。

## Decision

1. **当前不做**（仅列升级路径）：ReAct 循环可重放——恢复等价于「已完成的工具结果作为历史消息回喂 + 用户追加」，SessionManager 已能提供等价会话历史；工具副作用不可原子回滚，单用户场景收益低；顺序上先落地 #20 成本上限（停机）再谈恢复。
2. **若做，粒度选「循环级」checkpoint**（每轮 LLM 调用完成后写）优先于会话级：恢复点最多丢「当前轮正在执行的工具」，不丢已完成轮次。
3. **序列化快照结构（CheckpointPort 契约，届时定义）**：
   - 元数据：session_id / iteration / created_at / 触发原因（timeout / cancel / cost / …）
   - messages（含 assistant.tool_calls 配对，天然可恢复）
   - 累计 usage（prompt / completion / total tokens）
   - 工具调用记录（tool_call_records，证据链，恢复后补全 outcome）
   - Agent 配置快照（max_iterations / temperature / max_tokens / max_cost ceiling）
4. **存储**：复用 SessionManager 会话级存储（Redis / DB 已存在）；多实例 / 分布式场景需共享存储 + 版本号并发控制。
5. **恢复语义**：从 checkpoint 重建 messages → 跳过已完成轮次 → 续跑 ReAct 循环；已执行工具的副作用视为「已发生」，不重放（记录在案，进证据链）。

## Consequences

- ✅（若实现）长任务可恢复，成本停机后可提额续跑而非重跑；token 不重复付费。
- ✅ 与 #20 互补：COST_EXCEEDED 停机时保留 checkpoint，恢复是「重建上下文 + 续跑」。
- ⚠️ 工具副作用不可原子化：恢复时对已执行副作用承担「已发生」语义，需文档化（对分析型工具幂等无碍，对写类工具需谨慎）。
- 📌 升级路径：先定义 `CheckpointPort` 端口 + SessionManager 存储实现（复用现成会话存储），再在 ReAct 每轮末尾写 checkpoint；恢复 API = 从 checkpoint 重建 context + messages 再走既有 `run()`。
