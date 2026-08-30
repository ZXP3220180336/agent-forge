# 循环停滞检测（动作指纹 + STALLED 分发硬终止）

> 日期：2026-08-30 ｜ 层级：domain + shared

## Context

- 对标增强项 #24：模型陷入「反复调用同一工具同一参数」死循环时，现有 `max_iterations` / 总时长 / 成本上限三层护栏最终能兜底，但**兜底前每轮都在烧预算、重复产生无意义副作用**（如重复写文件 / 重复调用外部 API）——典型死循环需要更早的主动停机。
- 工业界参照（2026-08-27 调研 + 2026-08-30 复核）：
  - **SMOLagents `same_action_llm_turn_limit`**：连续相同动作超限即停（默认 3）。
  - **SMOL 官方教程 step_callback 方案**：`deque(maxlen=3)` 存 `(tool_name, json.dumps(tool_input, sort_keys=True))`，连续 3 个全相同 → 中断（避免继续浪费 API 调用）。
  - **ml-intern doom-loop 检测**（fork 实现）：`ToolCallSignature`（name + args_hash + 可选 result_hash）；`_normalize_args` 对 JSON 参数 parse → `sort_keys=True` 紧凑重 dump（语义相同的 key 顺序 / 空白 hash 一致，非法 JSON 回退原始串）；`detect_identical_consecutive` 判 3+ 连续相同调用；`detect_repeating_sequence` 判周期模式（A→B→A→B，长度 2-5 重复 2+ 次）；检测到后**注入纠正性用户消息**而非再调 LLM（软干预）。
- 本项目约束：react.py 只依赖 ports + shared + 标准库；错误处理已确立「终结护栏先 dispatch（handler 可 RAISE），默认 STOP 组装 outcome」范式（TIMEOUT / COST_EXCEEDED 先例）；`#25` 空输出重试上限已确立「连续计数 + 配置化」先例。

## Decision

1. **动作指纹 `_action_fingerprint(tool_calls)`**：本轮所有工具调用的 `(name, args)` 序列化。参数 `json.loads` 后 `json.dumps(sort_keys=True, ensure_ascii=False)`——语义相同的不同 key 顺序 / 空白指纹一致；参数 JSON 非法回退原始字符串。**排除 `final_answer`**（终止工具，非循环动作；整轮仅 final_answer 时指纹为空 → 不检测）。多工具并行轮整轮作为指纹（gather 保序，同轮顺序稳定，不因并行顺序误判）。
2. **连续计数**：`self._stall_count` + `self._last_action_fp`（execute 开头重置）。本轮指纹与上次相同 → `+1`；不同 → `=1` 且更新指纹。非工具轮（stop / 空输出 / LLM 失败）**保留状态不更新**——「连续请求相同动作」跨越异常轮仍累计（更早停机，方向保守安全）。
3. **硬终止**：`count > max_same_action_turns`（默认 3 → 连续相同 4 轮终止）→ `_finalize_stalled`：先 `_dispatch(STALLED, ...)`（handler 可 RAISE 上抛；CONTINUE 被忽略——对齐 COST_EXCEEDED 终结护栏语义），默认 STOP 组装 outcome（success=False，error=「连续 N 轮相同工具调用（工具名），已终止」）+ info + done。**检测点在本轮工具执行前**：停滞判定后不执行本轮工具（执行无意义 + 防重复副作用 / 烧钱）。
4. **新错误 kind**：`AgentErrorKind.STALLED`（第 11 类，终结性），`_DEFAULT_ACTIONS[STALLED] = STOP`。触发场景「连续相同工具调用」，默认行为「停机」。
5. **配置**：`agent_max_same_action_turns: int = 3`（validator ≥ 1；0 无意义——首次工具调用 count=1 即停）。透传链路对齐 `max_empty_retries`：settings → container.agent_params → chat AgentContext → executor execute() 标量参数。
6. **软干预不做**：ml-intern 的「注入纠正消息继续」被否决——死循环重试 4 轮后模型自纠概率低，且纠正消息会污染证据链（产品核心）；硬终止 + 可配置 handler 覆盖更贴合本项目错误分发范式。

## Consequences

- ✅ 增强项 #24 闭合：典型死循环（同工具同参数原地打转）在兜底前主动停机，且不执行停滞轮的重复副作用。
- ✅ 与 #25 空输出重试上限独立互补：停滞检测在 tool_calls 分支（动作维度）、空输出计数在 finish_reason 判定（输出维度），互不冲突。
- ✅ 行为可配置 / 可扩展：默认 3 给合法重复留空间；注册 STALLED handler 可 RAISE 上抛 / 自定义。
- ⚠️ 误判边界——**合法轮询**：同参数连续查询但结果在变（如批次良率轮询），连续 4 轮同参数可能被误判停机。默认上限 + 参数规范化已降低概率；精确判定需结果维度（升级路径 ①）。
- ⚠️ 检测粒度为整轮 tool_calls 指纹：同轮多工具时参数组合整体比较（gather 保序，同轮顺序稳定）；跨轮部分相同部分不同 → 指纹不同 → 重置（不误判）。
- 📌 升级路径 ①：**result_hash 防轮询误判**——动作签名加工具结果内容 hash，「同工具 + 同参数 + 同结果」连续重复才算死循环，结果在变不算。需跨轮保存上轮工具结果，状态管理 + 测试面扩大，出现真实轮询误判再落地。
- 📌 升级路径 ②：**周期模式检测**（A→B→A→B）——滑动窗口签名历史 + 长度 2-5 重复匹配，检出跨动作周期死循环。需维护最近 N 轮签名序列，复杂度高、误杀风险高（合法对比查询易误伤），出现真实周期死循环再落地。
- 📌 与 #19 断点续跑：停滞停机后若无 checkpoint，恢复 = 重跑（再次付费）；「副作用不重放」原则下重跑安全。
