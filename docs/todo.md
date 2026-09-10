# 2026-09-10 第一阶段：收紧 P1/P2 验收边界

> 状态：实现、审查与验证已完成；独立提交待执行。范围以 2026-09-10 总体审核路线为准；LLM-044～047、REASON-012～015 当前保留在工作区。

- [x] **TimeoutError / UNKNOWN 红测与修复**：ReAct 仅在 timeout scope `expired()` 时归 TIMEOUT；内部普通异常归 UNKNOWN，并保留当前轮成果与未归账 usage。
- [x] **续接上下文超限闭环**：真实续接请求因前缀扩大触发 `ContextWindowExceededError` 时，保留当前轮内容和可得 usage，停止续接并只产出一次 done。
- [x] **真实 deadline 清理链**：用 ReAct + 真 LLMService 验证 `_drain → close → settle` 在清理窗口内完成；断言 SDK create 恰一次，禁止整流、续接和 fallback。
- [x] **超时取消兜底**：真实资源清理超过 grace 时由 `max_execution_time` 触发 task 取消；Integration `finally` 接管未终态 Reservation，不遗留资源所有权。
- [x] **fallback reserve 专项**：主请求进入 fallback 后，fallback 独立池 reserve 排队响应 cancel/deadline；覆盖部分 RPM/TPM 回收、fallback SDK create 为零、终止异常不被主异常包装。
- [x] **时间契约收紧**：明确 `max_execution_time` 是业务循环的 timeout 取消触发点；领域终态分发位于 timeout scope 外，不再发起副作用但可能形成额外尾部延迟，不承诺同步阻塞或吞取消代码的绝对返回时限。
- [x] **文档漂移修正**：更新 todo 过时状态、`streaming_rectifier.md` deadline 签名与测试数量，并同步对应 issue/ADR/组件文档。
- [x] **验证**：执行控制、LLMService、整流、structured、ReAct 定向测试和全量测试通过；alignment/diff check 通过。
- [ ] **独立提交**：第一阶段代码、测试、ADR、issue、文档已划定提交范围；当前环境拒绝写入 `.git/index`，尚未形成提交。

> **评审结果**：第一阶段通过。TimeoutError 来源、UNKNOWN/current result、续接上下文超限、真实 close/settle 清理链和 fallback reserve cancel/deadline/R5 均有跨层回归；最终只保留一个已修正的措辞问题——timeout scope 外的可扩展 handler 可能产生额外尾部延迟，不能称为有界绝对返回。
> **验证结果**：定向套件 **348 passed**；全量 **917 passed**（1 个既存 StarletteDeprecationWarning）；`verify_alignment` 与 `git diff --check` 通过。

---

# 2026-09-09 ReAct 区分硬超时与内部 TimeoutError（LLM-046 跨层补强）

> 状态：✅ 已完成。LLM-046 已阻止续接 EOF 后收尾异常触发新请求；ReAct 现按 timeout scope 的实际到期状态区分硬超时与内部普通 `TimeoutError`。

- [x] **红测**：LLM 在硬期限未到时抛普通 `TimeoutError`，ReAct 必须走 UNKNOWN，不得走 TIMEOUT；现有真硬超时仍走 TIMEOUT。
- [x] **实现**：保留 `asyncio.timeout_at()` 上下文对象，仅在 `expired()` 为真时把 `TimeoutError` 映射为总时长耗尽；其余同名异常按未知组件失败处理。
- [x] **跨层回归**：用真实 `LLMService.async_generate` + 续接 EOF 后收尾 `TimeoutError` 复现 LLM-046 穿透路径，断言不再续接且 ReAct 不误报总超时。
- [x] **文档与验证**：登记独立 reasoning 问题、回链 LLM-046，同步 ReAct 文档与 lessons；运行定向测试、全量 pytest、alignment 和 diff check。

> **评审结果**：ReAct 保存 `asyncio.timeout_at()` 返回的 scope，仅当 `expired()` 为真时归 TIMEOUT；内部普通 `TimeoutError` 与其他异常归 UNKNOWN。两条 UNKNOWN 出口均接管 `current_result` 与未归账 usage。外部 task cancel 继续传播，异 task `aclose()` 干净退出。
> **验证结果**：修复前 3 个红测稳定失败；修复后相关定向 140 passed，全量 911 passed（1 个第三方 StarletteDeprecationWarning）；`verify_alignment` 与 `git diff --check` 通过。仓库未安装 `ruff`，未新增该工具依赖。

---

# 2026-09-06 会话交接：主副模型请求守卫与上下文预算跨策略闭环

## 2026-09-09 ReAct deadline 部分成果与清理宽限（问题 4 / 5）

> 状态：✅ 已完成。问题 4 为当前轮流式内容在类型化 deadline 出口丢失；问题 5 为 Integration 内部 deadline 与 ReAct 外层 timeout 同刻竞争，可能打断 close/settle/log 清理。

- [x] **问题 4 红测**：当前轮写入 content/reasoning 后抛 `LLMDeadlineExceededError`，TIMEOUT outcome 必须保留当前轮部分成果；当前轮尚无成果时仍保留上一轮结果。
- [x] **问题 4 实现**：显式维护未完成的 `current_result`，终止收尾按“当前轮有用户可见进度则优先，否则沿用 last_result”选择；异常 usage 只累计一次。
- [x] **问题 5 红测**：内部 deadline 到期后进入延迟清理，清理耗时小于 grace 时不得被外层 task 取消打断；忽略内部 deadline、但配合取消的调用仍必须在 timeout 触发点停止。
- [x] **问题 5 实现**：`max_execution_time` 是业务循环的 timeout 取消触发点；内部 LLM deadline 提前 `min(1s, 总时长×10%)`，让 close/settle/log 有机会在 task 取消前完成。
- [x] **回归与文档**：同步 ReAct ADR/组件文档、分别登记问题记录与 lessons；运行 ReAct 定向测试、全量 pytest、alignment 和 diff check。

> **评审结果**：两项缺口已闭环。类型化 deadline 与外层 timeout 均优先保留当前轮可见成果，无当前成果时回退上一轮；usage 以异常携带值优先并仅归账一次。清理窗口不延后 timeout 触发点；忽略内部 deadline、但配合 task 取消的 LLM 会在该时刻停止。领域终态组装位于 scope 外，不承诺整个调用绝对按秒返回。
> **验证结果**：ReAct 定向 100 passed；全量 905 passed（1 个第三方 StarletteDeprecationWarning）；`verify_alignment` 与 `git diff --check` 通过。

> 本节保留后续 Slice 的执行交接信息；当前事实以代码、独立 issue 和顶部第一阶段评审为准。
> 最近进展：主副模型请求守卫 **A-E 已落地并提交，第一阶段 P1/P2 验收已落地并通过验证、待提交**——B+C+D 见 [LLM-041](../issues/integration/llm/2026-09-06-fallback-window-and-quota.md)，E 见 [LLM-043](../issues/integration/llm/2026-09-08-structured-cancel-deadline.md)，每笔真实 SDK 调用及等待的执行控制见 [LLM-044](../issues/integration/llm/2026-09-08-execution-control-through-every-call.md)；**F（Slice 2-4 / 6-7）仍待续**。
> 注意：下节 §2「`_build_fallback_fn` 直接 SDK create」与 §3 冲突①的描述已被本轮修复，读取时以代码为准（主/副真实请求现由统一目标闭包工厂 `_make_guarded_call` 构造，fallback 走独立键窗口 + 独立池）。
> LLM-045：执行控制 abort 判赢后「迟回值」接管（reserve/create 资源不泄漏）见 [LLM-045 条目](../issues/integration/llm/2026-09-09-execution-control-late-result-drop.md)；helper abort 三结局分派 + 直测/跨层锁定已落地，随 LLM-044 所在分支提交。
> LLM-047：流式 deadline 已获 usage 传播闭环（[LLM-047 条目](../issues/integration/llm/2026-09-09-deadline-usage-propagation-closed-loop.md)）——代码收紧 + 全闭环测试已落地：整流/续接退避 deadline 与 cancel 出口补全（usage/取消事件不丢）、attempt 入口 / 续接 create 段裸抛收紧；整流退避保留 usage 单测、续接退避 deadline+cancel 单测、async_generate Facade 整流 deadline usage 不丢跨层直测、非流式迟回 cancel/deadline 参数化、structured 携 usage 终止累计+停止降级断言均已加；模块文档与 lessons 已同步。第一阶段最终回归与 alignment 结果记录在顶部评审。

## 1. 目标、范围与用户约束

产品目标是让多 Agent 良率 RCA 在超长证据、模型故障、取消和总时长耗尽时可靠终止，并保留证据链与部分成果。

近期优先项是主副模型在 `await client.chat.completions.create(**kwargs)` 前后的请求生命周期：最终请求预算、预留限流、取消、绝对截止时间、usage 回填、资源收尾。完成该基础后，继续下方“上下文预算跨策略闭环”的 Slice 2-7。

用户已经明确：

- 两类上下文分开管理：Application 的历史语义裁剪与 Integration 的最终请求准入不合并。
- `RequestBudgetGuard` 由 LLM 模块内部按模型配置管理，类似现有 Manager；**不得为了调用预算闸修改 `LLMGateway` 的接口契约**。
- 遵守 [架构依赖原则](architecture.md)：Domain 依赖端口，Integration 实现端口，Integration 不依赖 Application 的上下文管理实现；配置经 Container 注入。
- 主副模型请求都应按实际目标校验窗口；副模型需要考虑预留限流，配额是否共享取决于供应商真实配额范围。
- 取消与总执行时间约束覆盖限流前、限流等待中、限流后，以及 SDK 调用和流读取；所有阶段共用一个截止时间。
- 调研定案进入 ADR 的“工业级参照”，不在 docs 下新建研究目录；不要新增重复的研究记录。
- 全程中文；非平凡任务先计划；超过 3 个文件拆分；复杂任务使用子智能体；Bug 先写失败测试；纠正后记录教训。

规范先读仓库 `AGENTS.md` / `CLAUDE.md`，以及 [组件规范](component_doc.md)、[模块规范](module_doc.md)、[层 README 规范](layer_readme_doc.md)。项目实际使用本文件和 [lessons.md](lessons.md) 管理计划与教训；不另建 tasks 目录。用户引用的 `编码规范文档.txt` 本次未在仓库根找到，后续应搜索确认，不宣称已读。

## 2. 当前代码基线（已核实）

| 位置 | 当前实现与交接重点 |
| --- | --- |
| `app/integration/llm/request_budget.py` | Config / Result / Guard / Manager 已存在；按 key 缓存；注册配置清缓存；有 reset；校验窗口、余量、输出正整数；白名单计 messages/tools/response_format；JSON 序列化后 tiktoken 估算，加 16 协议开销；特殊 token 字面量按普通文本编码。仍使用 `default=str`，缺失 key 默认 128000/1024。 |
| `app/integration/llm/llm_service.py` | 主请求已使用 `_budget_guarded_call(budget_guard, ...)`，Facade 获取 Manager 实例；入口先校验预算，再 reserve，再 SDK create。流式初次请求、整流、续接和非流式调用复用该入口。不要按旧摘要继续查找 `_rate_limited_call`。 |
| 同文件 `_make_guarded_call` | 主/副同构目标闭包工厂（guard_key/model_id 参数化），复用同一 client/base_url/key（同 provider，LLM-012）。✅ 本轮已修复：fallback 独立键窗口校验 + fallback 独立配额池 reserve → create，Reservation 写入共享 `active` 统一结算（LLM-041）。 |
| `app/integration/llm/retry.py` | 主链路重试与熔断；CLOSED 重试耗尽、OPEN 拒绝、HALF_OPEN 探针失败可进入 fallback；fallback 单次调用，不参与主熔断健康记账。 |
| `app/integration/llm/errors.py` | 错误知识单一归属。未知异常默认 NON_RETRYABLE；`decide_downstream_error` 对非 SDK 的不可重试异常原样上抛。不要在多处复制错误分类。 |
| `app/integration/llm/streaming_rectifier.py` | 当前代码已经对 create 与续接 create 的 `ContextWindowExceededError` 记录日志后原样上抛；其余普通流式失败仍走 result.error/SSE。不要沿用较早会话“所有流式错误均转字符串”的结论。 |
| `app/integration/llm/structured.py` | JSON Schema → JSON mode → prompt 提取；还有输出上限翻倍、校验回喂；每次均通过 generate。可得 usage 累计到调用方传入字典；上下文超限在集成层应短路。 |
| `app/integration/llm/reservation_limiter.py` | Manager 按 model_key 缓存；RPM/TPM 组合预留；TokenBucket 等待可被任务取消，但不直接消费业务 cancel_event。预留过程中部分扣款有异常退款逻辑，需实测取消竞态，不凭推断重写。 |
| `app/shared/exceptions.py` | `ContextWindowExceededError` 当前属于 `NonRetryableError`，携带 model_key/input_tokens/input_budget/max_tokens。 |
| `app/api/middleware/error_handler.py` | 已有上下文超限 → 422 映射；这只是当前实现，不能据此证明所有策略均有正确降级。 |
| `app/domain/reasoning/react.py` | 流式和非流式已有 CONTEXT_EXCEEDED 收尾；非流式完成响应会先结算并携 usage 传播取消/deadline。deadline、cancel、UNKNOWN、硬 timeout 与续接上下文超限均按统一 current/last 规则保留部分成果和未归账 usage。 |
| `app/domain/reasoning/_common.py` | `guard_exceeded` 仍返回文案二元组；取消/时间/成本尚未演进为类型化无状态判断。 |
| `app/domain/reasoning/reflection.py`、`planner.py` | 自查/修正/规划/重规划/汇总共 5 处 except AppError 会把上下文超限按普通失败处理；需要具名终止与成果保留。Planner 剩余时长仍有 0.05 秒下限，取消前子步骤 usage 吸收顺序需修正。 |
| `app/domain/prompts/manager.py` | evidence/step results 有字符切片；draft/issues/schema 等缺少阶段语义预算。应保留原始审计证据，只缩减发给模型的视图并显式标记省略。 |
| `app/application/context/context_manager.py` | 当前实际路径；负责历史语义裁剪，不能移入 provider 最终请求准入或向 Integration 反向注入实现对象。 |

## 3. 已解决的代码、文档与讨论冲突

1. **同 provider 不等于同窗口。** fallback 已按独立 `model_key` 使用自己的窗口配置；同 provider 只表示当前复用 client/端点，不再隐含共享主窗口。
2. **预算闸不通过新增 LLM 调用参数接入。** RequestBudget Manager 在 `LLMService` 内按真实目标 key 选择；`LLMGateway` 新增的 cancel/deadline 仅服务跨 await 执行控制。
3. **流式上下文超限类型化上抛。** create 与续接准入拒绝原样穿透到 ReAct 的 `CONTEXT_EXCEEDED` 终态，不折叠为 SSE 字符串失败。
4. **请求生命周期执行控制已覆盖主副目标。** retry、退避、fallback、整流、续接、reserve、create 和 chunk 读取共享取消/绝对 deadline；fallback 排队专项测试锁定部分预留回收。
5. **估算不是 provider 精确计数。** JSON + tiktoken + 固定余量仍不保证所有模型、多模态和协议字段的精确容量；默认模型与窗口配置一致性继续由配置责任边界保证。

## 4. 已讨论的目标流程与资源规则

主副模型共用单次实际调用流程；重试/熔断/fallback 是其外层编排。

```text
策略入口：确定本次运行绝对截止时间、取消信号、累计成本
  → 按策略缩减语义上下文
  → 外层取消/期限/成本检查
  → 可靠性编排选择实际调用目标
      → 检查取消与绝对截止时间
      → 构造最终请求并按目标模型校验上下文窗口
      → 在取消/截止时间约束下等待对应配额
      → 获得预留后再次检查取消/截止时间
      → 在同一作用域内 await client.chat.completions.create(**kwargs)
      → 非流式：解析并记录可得 usage → 结算 → 返回后终止检查
      → 流式：移交流与预留的所有权 → 逐 chunk 受控读取 → 记录 usage → 关闭与结算
  → 策略吸收 usage 后执行取消/期限/成本检查
  → 输出结果，或保留部分成果终止
```

落实该流程时必须满足：

- 主调用重试、退避、fallback、整流、续接、结构化降级/回喂都消耗同一绝对时间预算，不能逐层重新计时；时间使用 monotonic。
- 业务 cancel_event 置位与硬任务取消都要覆盖。若使用竞争等待，必须取消并等待辅助任务结束，处理“配额已取得但外层同时取消”的竞态，不遗留后台 SDK 请求或无人持有的 Reservation。
- 取消、整体 DeadlineExceeded 不得被通用 TimeoutError 分类误当成可重试网络超时。尤其 deadline 若经过 retry/structured 的 catch，必须能保留执行终止语义。
- SDK 自身连接/读超时仍按既有传输策略处理；有剩余总时间才允许下一次实际尝试。
- 拿到预留但尚未发起 SDK 调用时终止，应退款。create 抛错目前按项目既有规则 cancel 退款；不能把“未成功返回 response”解释为远端绝对没有收到请求。
- create 已成功后的解析失败、取消、超时仍须结算，不能统一 cancel 退款；无实际 usage 时按现有 settle(None) 保守记账。清理应有确定的所有权和幂等终态。
- 流式 create 成功只表示取得流对象，不能立即结算；若此时取消，要关闭流。预留移交给读取器前后必须避免所有权空档或双重结算。
- 上下文闸只在每次最终请求发出前检查；同一请求成功返回后不重复检查窗口。续接前缀、结构化修正提示、schema 和翻倍 max_tokens 改变后要重新计数。
- 可得真实 usage 在终止判断前回填且只累计一次；不可得的传输失败用量不能用估算冒充实际 usage。上下文准入、TPM 预留、成本统计保持各自口径。

## 5. 主副模型配置与异常编排的现行决策

| 问题 | 约束与建议 |
| --- | --- |
| 目标模型窗口如何绑定 | 主模型使用 `main` key，副模型使用 `fallback` key，分别读取实际目标配置；业务角色与供应商模型不混为一谈。 |
| 配额池如何绑定 | 当前配置为主副独立 ReservationLimiter 池；若供应商实际共享 RPM/TPM，后续需新增显式共享池映射，不能仅靠 key 名推断。 |
| 是否保留模型 client 复用 | 当前仅支持同 provider fallback，暂不扩展跨供应商路由。相同端点的连接故障换模型通常无法隔离，不能宣称实现了独立网络容灾。 |
| 是否让副模型再重试 | 沿用现有一次 fallback，不递归调用主 Facade 形成套娃重试或再次 fallback。 |
| fallback 异常如何传播 | `_ExecutionAbort` 与 `ContextWindowExceededError` 在 CLOSED/HALF_OPEN/OPEN 均保持终结语义，不包装为主网络故障；普通 fallback 异常维持现有 cause 关系。 |
| 熔断健康如何记账 | 本地预算拒绝不作为下游故障；副模型成功不证明主模型恢复；主请求已发生的真实网络故障不能因之后取消而无条件抹除。 |
| 通用方法放哪里 | `_common.py` 可放领域层无状态判断、剩余时间计算与类型化结果；供应商请求计数/限流编排留在 Integration。跨 await 的取消作用域具体放置须先审阅依赖和现有机制。 |

## 6. 建议续接顺序（先更新计划再按切片推进）

- [x] **A：核验基线与决策。** ✅ 本轮完成：核验代码与交接描述吻合；ADR Decision 7 修订（否决扩展 LLMGateway 调用级预算）；副模型承载定案为独立 `fallback` 键 + 参与限流。
- [x] **B：主副模型预算配置闭环。** ✅ 本轮完成：settings/container/.env 加 fallback 窗口 + RPM/TPM；fallback 独立键窗口校验（主窗可容/备用窗拒绝正反例）。
- [x] **C：单次调用执行控制与预留所有权。** ✅ 本轮完成：fallback 进统一请求入口（独立池 reserve → create → 共享 active 结算）；retry 对 fallback 预算超限直抛不包装；`_budget_guarded_call` reserve 后 create 前取消复查 + `_StreamCancel` 整流映射（拿到预留但已取消 → 退款不请求）。
- [x] **D：副模型预留与流式收尾。** fallback 成功/中断走主链路同一 settle 收尾（单次、不双结算）；流式与非流式 generate 都把 cancel/deadline 传到 reserve、create 和重试等待，Facade 翻译为 shared 终止异常并保留可得 usage。
- [x] **E：结构化内部调用闭环。** ✅ 2026-09-08 完成（见 [LLM-043](../issues/integration/llm/2026-09-08-structured-cancel-deadline.md)）：`generate_structured` 增加可选 `cancel_event`/`deadline`，`StructuredOutput` 降级链每条真实子调用前（`_call_generate` 入口）拦截命中即 return None（与降级耗尽同出口）；内置 `TimeoutError` 直抛防终止被吞；reflection/planner 透传 + usage 上抛路径保留。
- [ ] **F：领域策略闭环。** 按下方 Slice 2-4 分别处理 _common/ReAct、Reflection、Planner 与 prompts；每批区分公共判断和策略私有降级，保留最近稿、步骤进度与证据引用。含 E 划界后续：Reflection/Planner 策略层 `asyncio.timeout` 整链硬超时兜底（Decision 8 总兜底）、取消/超时与降级耗尽的 error 文案区分（[LLM-043](../issues/integration/llm/2026-09-08-structured-cancel-deadline.md) 决策 3）。
- [ ] **G：文档、问题与验证（收尾）。** 后续批完成后更新 ALIGNMENT 与交接评审；只生成提交信息，不擅自提交。

## 7. 必须覆盖的回归矩阵

| 场景 | 核心断言 |
| --- | --- |
| 主/副模型窗口不同 | 大窗口可放、小窗口拒绝；取目标配置；拒绝请求 SDK 调用为 0。 |
| 超大 user/system/tools/schema | 完整最终输入被检查；拒绝前不 reserve，不增加熔断故障统计。 |
| 主模型熔断 CLOSED/OPEN/HALF_OPEN | 所有 fallback 入口都受守卫；健康记账保持主副模型分离。 |
| 副模型普通调用 + 降级并发 | 共享真实配额时共享池；没有两份虚增容量；独立配额不互相扣减。 |
| 限流前/等待中/返回后取消或超时 | 无新 SDK 调用；部分预留可回收；辅助任务全部退出；重复取消不双退。 |
| SDK create 成功后立刻取消 | 非流式可得 usage 不丢；流式 response 关闭；请求已发生的配额正确结算。 |
| 整体超时与 SDK 超时 | 前者终止整个执行链，后者按传输重试规则；不重置 deadline。 |
| 主网络失败 + 副模型超限/超时 | 异常 cause、外层决策、结构化是否继续调用均有断言；防止终止信号被降级吞掉。 |
| 结构化扩容/回喂/下一级格式 | 每个新 payload 重新准入；拒绝后不追加调用；前几次真实 usage 保留。 |
| 半流续接超限/取消 | 当前内容、已知用量和之前审计证据可保留；流关闭，预留无泄漏。 |
| Reflection/Planner 超限 | 不把超限当普通失败继续付费重试；采用最近稿/已完成步骤，标明不完整性。 |
| SSE 与结果一致 | 终结 done 只产生一次，token 总数与 outcome 一致；不依赖错误文案驱动控制流。 |
| 配置与缓存 | 非法值 fail fast；register/reset 生效；测试恢复所有全局 Manager 配置和缓存。 |

## 8. 新会话必读文件与运行环境

仓库：`E:\MyWorkSpace\Agent\VSCodeDemo\PersonalProject\agent-forge`；Shell：PowerShell；当前工作区有大量未提交修改及未跟踪文件，必须保留。新会话可能与其他编辑共享工作区，先重新读取，不用旧快照覆盖。

按顺序阅读：

1. `CLAUDE.md`、`docs/architecture.md` 的依赖原则、本交接及下方 Slice 计划。
2. `app/domain/ports/llm_gateway.py`，核对公开契约及 StreamResult 用量语义。
3. `app/integration/llm/llm_service.py`、request_budget.py、client.py、reservation_limiter.py、retry.py、errors.py、streaming_rectifier.py、structured.py、token_counter.py。
4. `app/config/settings.py`、`app/container.py`；不要输出 .env 密钥。
5. `app/domain/reasoning/_common.py`、react.py、reflection.py、planner.py，以及 prompts/manager.py、Application context_manager.py / cost_limiter.py。
6. [请求预算 ADR](../adr/integration/llm/2026-09-06-request-context-budget.md)、[语义预算 ADR](../adr/domain/reasoning/2026-08-28-context-budget.md)、[LLM 文档入口](integration_doc/llm_doc/llm.md) 及其组件导航、[异常处理说明](shared_doc/error_handling.md)。

研究参照沿用已有 ADR；关于配额范围的补充事实可查 [OpenAI 官方限流说明](https://developers.openai.com/api/docs/guides/rate-limits)：配额可能按组织/项目/模型及共享模型组约束。它只支持“需要核实供应商范围”，不能当作当前项目已配置供应商的具体配额事实。

常用验证：

```powershell
git -c safe.directory=E:/MyWorkSpace/Agent/VSCodeDemo/PersonalProject/agent-forge status --short
uv run pytest tests/unit/test_request_budget.py tests/unit/test_llm_request_budget.py tests/unit/test_llm_service.py tests/unit/test_streaming_rectifier.py tests/unit/test_stream_rectify.py tests/unit/test_generate_structured.py tests/unit/test_retry.py tests/unit/test_react_strategy.py tests/unit/test_react_strategy_nonstream.py -q
uv run pytest tests/unit/test_reservation_limiter.py tests/unit/test_retry_handler_manager.py tests/unit/test_token_counter.py tests/unit/test_container.py tests/unit/test_settings.py -q
uv run pytest tests/unit/test_reflection.py tests/unit/test_planner.py tests/unit/test_prompts.py tests/unit/test_reflection_agent.py tests/unit/test_planner_agent.py -q
uv run pytest
uv run python -m scripts.verify_alignment
git -c safe.directory=E:/MyWorkSpace/Agent/VSCodeDemo/PersonalProject/agent-forge diff --check
```

## 9. 本次交接验证记录

- 相关 9 个测试文件：**265 passed in 39.35s**（上节第一条 pytest 命令）。
- 文档对齐与空白检查：`verify_alignment` 通过，`git diff --check` 通过；Git 仅提示工作区 LF/CRLF 转换。
- 未在本次交接中运行全量测试；不继承旧会话的全量通过声明作为当前工作区证据。
- 当前 `test_open_circuit_fallback_shares_main_window_guard` 检查的是待修正行为；即使通过也不能验收副模型窗口隔离。

### 本批（2026-09-06 B+C+D）补充

- 受影响文件 9 组 **223 passed**；全量 **855 passed**（94s，1 外部依赖 deprecation 警告）；LLM-042 补修后复跑 **856 passed**（见下方 LLM-042 条目）。
- `test_open_circuit_fallback_shares_main_window_guard` 已改名改向为 fallback 独立键（`test_open_circuit_fallback_uses_fallback_window_guard`），并新增 `_open_circuit` helper——置 `_last_failure_time` 向未来使冷却未过，确保真触发 fallback 路径。
- 文档对齐与空白：`verify_alignment` 通过；`git diff --check` 通过（本批完成后复核）。

## 10. 可复制到新会话的启动指令

> 请先阅读 `docs/todo.md` 顶部“2026-09-06 会话交接：主副模型请求守卫与上下文预算跨策略闭环”，核验当前未提交 diff 和最新代码。目标是完善主、副模型每次实际 SDK 请求前后的上下文准入、预留限流、取消与同一绝对截止时间约束，以及 usage/流资源/Reservation 的可靠收尾，再继续原 Slice 2-7。先指出现有代码与交接目标冲突，给出按文件拆分的实施计划。不得为了请求预算闸修改 LLMGateway 调用契约，不得把 Integration 请求准入放进 Application ContextManager，不得假设同端点模型同窗口或不同 model_key 必然配额独立。保留工作区已有修改，测试先行；文档遵守 CLAUDE.md，调研定案进既有 ADR，不在 docs 新建研究目录。以新会话明确授权的范围执行。

---

# 上下文预算跨策略闭环（Slice 1 / 5 已落地 · Slice 2-4 / 6-7 待续）

> 目标：在不破坏半导体良率 RCA 证据链的前提下，使 ReAct、Reflection、Plan-then-Execute 的每一次实际 LLM 请求均受上下文窗口保护；请求无法容纳时阻止网络调用，并按策略保留可用的部分结果。
> 进度：集成层预算闸（Slice 1，2026-09-06）与结构化调用取消/期限闭环（Slice 5，2026-09-08，见 [LLM-043](../issues/integration/llm/2026-09-08-structured-cancel-deadline.md)）已实现；Slice 2-4 / 6-7 待续。
> **阻塞标注**：reflection/planner 的 5 处 `except AppError`（critique/refine/plan/replan/summarize）会把超限当普通结构化失败吞掉降级——该缺口归入 Slice 3/4 专项处理。

## 已核实现状与范围

| 调用路径 | 现有保护 | 本次缺口 |
| --- | --- | --- |
| ReAct 流式/非流式循环 | 调用前 `trim_messages`；整体超时；调用后成本检查 | 单条超长输入、tools、`final_answer` schema 与实际模型窗口未计入；裁剪后不验证是否仍可请求 |
| Reflection 的 ReAct 收集 | 复用 ReAct | 同 ReAct |
| Reflection 自查/修正 | 阶段入口取消/总时长/成本检查 | evidence、draft、issues 构成单条 user 消息，未做预算（语义缩减归 Slice 3）；`generate_structured` 内部重试的取消/期限逐次检查已由 Slice 5 覆盖（[LLM-043](../issues/integration/llm/2026-09-08-structured-cancel-deadline.md)） |
| Planner 步骤执行 | 每步复用 ReAct | 同 ReAct |
| Planner 规划/重规划/汇总 | 阶段入口取消/总时长/成本检查 | tool catalog、executed、failed step 构成单条 user 消息，未做预算（语义缩减归 Slice 4）；结构化内部重试的取消/期限逐次检查已由 Slice 5 覆盖（[LLM-043](../issues/integration/llm/2026-09-08-structured-cancel-deadline.md)） |

## 设计约束（实施前不可破坏）

- `ContextBudgetPort` 继续负责 Agent 多轮消息的语义裁剪；Provider 请求编码、tools 或 JSON Schema 的计数逻辑留在 Integration 的 `LLMService` 请求准入闸。
- LLMService 在发起 provider 请求前拥有最终预算校验权；校验须覆盖当前 `model_key`、messages、tools、response_format 及该次 `max_tokens` 输出预留。
- Domain 决定语义缩减策略和降级结果：Reflection 保留最近可用稿与证据引用，Planner 保留已完成步骤与证据，ReAct 保留工具配对；Integration 不擅自删除业务证据。
- 预算口径是单次请求的输入窗口，不与累计 token 成本、总执行时间或轮次数混用；所有超限路径都不得发起网络调用。
- 不吞 `asyncio.CancelledError`；保留 ReAct 流式取消、SSE 事件和 usage 计量的既有语义。

## 计划

- [x] **Gate 0：调研并固化边界。** LangGraph 与 Semantic Kernel 将会话历史裁剪/摘要置于状态或历史 reducer；OpenHands 将 provider 共享窗口规则置于 LLM adapter；Amazon Bedrock 对输入与输出预留施加硬窗口校验。语义预算决策记录在 `adr/domain/reasoning/2026-08-28-context-budget.md`，请求准入决策记录在 `adr/integration/llm/2026-09-06-request-context-budget.md`；不以模型名称或固定常量猜测窗口。
- [x] **Gate 1：先定契约与异常语义。** ADR 已定义有效输入额度、模型窗口配置、`ContextWindowExceededError` 和策略降级语义；`max_context_tokens` 仍保持既有 AgentContext 注入语义。
- [x] **Slice 1：Integration 最终请求预算闸。** 新增 `request_budget.py`，由 `LLMService` 真实请求入口 `_budget_guarded_call` 在每次主请求前校验最终参数（预算准入是入口内先于限流预留的独立第一步）；流式整流、续接、非流式、JSON Schema/JSON mode/fallback/回喂/扩容重试均复用该入口。新增窗口配置和零 provider 调用回归测试。
- [ ] **Slice 2：ReAct 预缩减与收尾一致性。** 修改 `app/domain/reasoning/react.py`、`_common.py` 和必要端口：保留现有轮次/assistant-tool 原子裁剪；将取消、剩余总时长、成本准入判断演进为类型化无状态结果，避免通过错误文案驱动控制流；调用成功后先归并可得 usage，再处理取消、超时和成本收尾。Integration 最终闸拒绝后走明确的上下文超限终止分支，而非误判为空输出或 LLM 临时失败。
- [ ] **Slice 3：Reflection 阶段上下文缩减。** 修改 `app/domain/reasoning/reflection.py` 与 prompts 序列化边界：为 evidence、draft、issues 定义稳定的预算分配和截断标记，优先保留证据 ID、结论、量测值、时间锚点与未解决问题；缩减后仍超限则采用最近可验证稿完成降级。自查、修正的每次调用前后都执行通用执行护栏；请求级准确性由 Integration 最终闸保证。
- [ ] **Slice 4：Planner 阶段上下文缩减。** 修改 `app/domain/reasoning/planner.py` 与 planning 序列化边界：规划保留目标和可用工具摘要，重规划保留依赖、成功步骤摘要和失败原因，汇总保留各步骤结构化结论及证据引用。超限时不丢弃已完成步骤：汇总降级为既有纯文本/部分报告路径，并明确其不完整性。
- [x] **Slice 5：结构化调用的取消和总时长闭环。** ✅ 2026-09-08 实现（[LLM-043](../issues/integration/llm/2026-09-08-structured-cancel-deadline.md)）：`generate_structured` 实际子调用获得取消/期限边界（信号下沉 + 每条子调用前检查，命中返回 None 与降级同出口）——Reflection/Planner 在总时长耗尽/取消后不再发起 JSON 降级、回喂、扩容重试；与预算闸共同覆盖所有真实请求。
- [ ] **Slice 6：测试驱动实现。** 每个 Slice 先添加失败用例，再实现。至少覆盖：单条超长 user/system；超大 tool 定义与 schema；不同 `model_key` 窗口；ReAct 裁剪后仍不可容纳；Reflection evidence/draft/issue 超限；Planner replan/summarize 超限；结构化 JSON 降级、回喂、扩容重试再次超限；拒绝时 SDK 调用次数为零；取消、超时、成本与上下文检查的优先级；usage 不重复计入；SSE 只产生一个 done；证据和部分进度保留。
- [ ] **Slice 7：文档与评审。** 同步 `docs/domain_doc/reasoning_doc/`、`docs/application_doc/context_doc/context.md`、`docs/integration_doc/llm_doc/`、配置文档、`docs/ALIGNMENT.md`；新增一个问题记录说明本次发现、修复、验证和教训。运行相关测试、全量 `uv run pytest` 与 `uv run python -m scripts.verify_alignment`，在本节填写评审结果和边缘情况。

## 文件影响预估

| 范围 | 预期文件 |
| --- | --- |
| Domain 契约与策略 | `app/domain/ports/llm_gateway.py`、`context_budget.py`、`app/domain/reasoning/_common.py`、`react.py`、`reflection.py`、`planner.py`、必要 prompts 模板/manager |
| Application / 配置 | `app/application/context/context_manager.py`、`app/config/settings.py`、实际 Container 装配文件 |
| Integration | `app/integration/llm/token_counter.py`、`llm_service.py`、`structured.py`、必要的 `client.py` / `errors.py` |
| 测试与文档 | 对应 unit/integration 测试、reasoning/context/llm/config 文档、ALIGNMENT、ADR、issue |

## 可选项（Gate 1 决策）

- [ ] **严格 provider 计数**：为已知 provider 采用精确协议计数；未知 OpenAI 兼容 provider 使用显式保守余量。优点是容量利用率高，代价是配置与适配复杂度增加。
- [ ] **语义摘要**：暂不纳入本次。工具原始证据摘要可能破坏 RCA 可追溯性；本次只做可标识的字段级缩减。真实产品需求出现后再单独设计证据摘要和可追溯引用。
- [ ] **调用前成本预留**：暂不纳入本次。现有成本护栏在取得精确 usage 后判定；严格防止单次调用越过成本上限需要独立的最坏成本预留策略。

## 评审（待实施后填写）

- [ ] 所有真实 LLM 请求均在网络调用前通过最终预算闸。
- [ ] 三种策略在超限时均保留定义好的部分进度，且不将上下文超限误归类为临时 LLM 故障。
- [ ] 取消、总时长、成本、上下文四类护栏的优先级和 usage 口径有单测覆盖。
- [ ] 全量测试和文档对齐校验通过；未新增未登记模块。

---

# 2026-09-06 主副模型请求守卫：fallback 独立键 + 独立池限流 + 取消/收尾闭环（LLM-041）

> 对应交接 §6 B+C+D。ADR：请求准入 Decision 3/7/9 修订（fallback 独立键、否决端口扩展、副模型生命周期）。Issue：[LLM-041](../issues/integration/llm/2026-09-06-fallback-window-and-quota.md)。

- [x] **§3 冲突处置**：①同 provider≠同窗口 → fallback 独立键（修订）；②ADR Decision 7 否决端口扩展（修订）；③流式超限上抛核验通过不撤销；④测试绿灯不验收（原共享主窗口用例实际测的是 HALF_OPEN 主链路探针，未真走 fallback，已修正）；⑤估算非精确 → config/request_budget 补窗口一致性责任句。
- [x] **B 配置接线**：settings `llm_fallback_context_window_tokens/rpm/tpm` + container 注册 `fallback` 键（RequestBudget/ReservationLimiter）+ .env.example/config 文档。
- [x] **B/D fallback 进统一入口**：`_build_fallback_fn` → async 闭包经 `_budget_guarded_call`（fallback 键窗口 + fallback 独立池 reserve → create）；Reservation 写入共享 `active` 由调用方统一 settle/cancel；`active` 构造前移（async_generate/generate）；过时注释同步。
- [x] **C1 预算超限直抛**：`retry.execute` 两处 fallback 失败分支对 `ContextWindowExceededError` 直抛（不 `raise last_exc from ...`），防下游把终结信号当可恢复错误触发再调主。
- [x] **C2 取消复查**：`_budget_guarded_call` 预留后 create 前复查业务 `cancel_event` → `cancel()` 退款 + 抛 `_StreamCancel`，整流器映射用户取消出口（拿到预留但已取消不发起请求）。
- [x] **测试**：fallback 独立键拒绝（model_key=="fallback"）、主窗可容/备用窗拒正反例、CLOSED/HALF_OPEN fallback 超限直抛、fallback 成功走独立池 settle 恰一次、reserve 排队期间业务取消退款零 SDK；`_open_circuit` helper 置 `_last_failure_time` 向未来保证真走 fallback。
- [x] **文档/ADR/issue**：ADR Decision 3/7/9 + Consequences；llm_service.md（fallback 参与闭环/约束边界/流程 ②.5）/ request_budget.md（fallback 窗口/测试状态）/ config.md（字段 + 窗口一致性责任）；issue LLM-041 + README 登记。
- [x] **验证**：受影响 223 passed；全量 **855 passed**；verify_alignment 通过；git diff --check（见下）。

---

# 2026-09-08 执行控制贯穿每笔真实 SDK 调用及等待（LLM-044）

> LLM-043 覆盖边界修正（E 只在 generate 门口检查一次）。Issue：[LLM-044](../issues/integration/llm/2026-09-08-execution-control-through-every-call.md)。当前实现位于工作区。

- [x] **统一语义**：主副、流式/非流式共享准入与终止（每 attempt 入口 → 预算 → 受控 reserve → reserve 后复查 → deadline 约束 create → 结算/读取期竞争）；覆盖 retry/整流/续接退避 + reserve 排队 + chunk 读取。
- [x] **终止信号双层**：integration 私有 `_ExecutionAbort`/`_StreamCancel`(升格)/`_DeadlineExceeded` + shared 领域出口 `LLMCancelledError`/`LLMDeadlineExceededError`（NonRetryableError 携 usage）——Facade 翻译，Domain 不依赖私有；react 收编 CANCELLED/TIMEOUT。
- [x] **_budget_guarded_call create_started 状态机**：create 调度后主动终止且任务配合取消时 `settle(None)`；若任务吞取消迟回 response，则返回值先接管所有权，调用方按实际 usage 结算/关闭后再抛终止。自然传输异常维持 cancel。
- [x] **等待统一原语**：wait/await_with_execution_control（直接抛类型化 + 收协程工厂）；retry 退避/整流续接退避/reserve 排队走同一控制。
- [x] **整流四方竞争**：_drain（anext×cancel×deadline×idle，确定性判定序）取消即时响应；attempt/迭代 deadline 类型化终止（不整流/续接）。
- [x] **structured 整链终止一次**：_call_generate 累计 usage + raise；extract 外层 return None。
- [x] 审查补强：修复流式 create retry 与续接 deadline 漏传、chunk 同时完成 usage 丢失、未关闭流、structured 入口空转、ReAct 终止 usage 丢失；执行控制组件更名为 `execution_control.py`。
- [x] 全量 **878 passed**；`verify_alignment` 与 `git diff --check` 通过。
- [ ] 遗留：F（Slice 2-4 / 6-7）仍待续。

---

# 2026-09-08 generate_structured 降级链取消/期限闭环（LLM-043）

> 对应交接 §6-E / Slice 5（"结构化内部调用闭环"）。Issue：[LLM-043](../issues/integration/llm/2026-09-08-structured-cancel-deadline.md)。

- [x] **发现**：StructuredOutput 三级降级链（每级含截断扩容 + 回喂，单次 generate_structured 最坏 ~9 次真实请求）无任何取消/期限检查点；reflection/planner 只在阶段入口 guard，一旦进入 generate_structured 内部跑满为止。
- [x] **信号下沉**：`generate_structured`/`StructuredOutput.extract` 增加可选 `cancel_event` + `deadline`（monotonic 绝对，调用方现算 start_time+max_execution_time）；检查点单点放 `_call_generate` 入口（三级初始/截断扩容/回喂/fallback 全部真实请求必经），命中返回 None（与降级耗尽同出口）。
- [x] **TimeoutError 防御**：`_call_generate` except 对内置 `TimeoutError` 直抛（整体期限终止不得被 decide_downstream_error 当 RETRYABLE 吞成降级再调用）；`APITimeoutError`/`httpx.TimeoutException` 仍走可恢复路径。
- [x] **领域层透传**：reflection/planner execute 现算 deadline，`_critique`/`_refine`/`_plan`/`_replan`/`_summarize` 加形参透传；usage 上抛路径保留（except AppError 返回 `usage or None`）。
- [x] **测试**：7 红转绿（cancel/deadline/TimeoutError 集成）+ reflection 透传/usage 保留 2 + planner 透传 1；既有 recoverable 用例改 `httpx.ReadTimeout`（内置 TimeoutError 不再冒充网络超时）。
- [x] **验证**：全量 **867 passed**（857 + 10）；verify_alignment 通过。

---

# 2026-09-07 reserve R5 兜底退款二次取消 RPM 泄漏（LLM-042）

> 独立问题（一个问题一个文件）。发现 → 分析 → 修复 → 验证 → 教训见 [LLM-042](../issues/integration/llm/2026-09-07-reserve-r5-cancel-interrupted-rpm-leak.md)。

- [x] **发现（探针红）**：`_acquire` R5 兜底 `res.cancel()` 退款 await 被二次取消时 RPM 桶不回满（探针 `test_reserve_r5_refund_interrupted_by_cancel_leaks_rpm` 修复前 4≠5）。
- [x] **根因**：reserve 的 res 不外传 `_acquire`、无外层续退——「未终态供外层续退」契约的适用前提（Reservation 被外层持有）在 reserve 内不成立。
- [x] **方案取舍**：A（shield 进 `Reservation.cancel`）改动共享契约面大；B（采用）R5 就地循环补齐退款到终态再传播取消，只改无外层续退死角。
- [x] **修复**：`reservation_limiter.py` `_acquire` R5 分支 `while not res.settled: try cancel except CancelledError: continue; raise`。
- [x] **验证**：reservation_limiter 39 passed + 集成组 109 passed + 全量 856 passed；既有未终态契约测试零改动通过。
- [x] **登记**：issue LLM-042 + issues/integration/llm/README 索引；LLM-041 与本节不再挂靠。

---

# 2026-09-06 上下文预算闸对齐收尾（Slice 1 落地收尾 + 流式穿透）

> 目标：请求预算闸按 llm 模块子组件纪律收尾——流式/非流式一致终结、request_budget 对齐规范、异常树与 API 语义自洽、全量测试与文档同步。ADR：`adr/integration/llm/2026-09-06-request-context-budget.md`。

- [x] **Slice 1 实现收尾**：request_budget.py 补 `RequestBudgetConfig` 非法组合校验（`ParameterValidationError`）/ `RequestBudgetManager.reset()` / `validate` 的 max_tokens 正整数校验——test_request_budget 3 组红转绿；意外 2 组红揭示规格：sampling/metadata 不计入窗口（序列化从黑名单剔除改**内容白名单**，贴合 ADR Decision 4）、特殊 token 拼写（`<|endoftext|>`）不得使估算崩溃（`disallowed_special=()`）
- [x] **流式契约对齐**：`ContextWindowExceededError` 在整流器 create 阶段与半流续接链识别后**原样上抛**（不折「LLM 调用失败」error 事件、不置 result.error）→ react 流式主循环也走 `CONTEXT_EXCEEDED` 终结（与非流式一致，落 ADR Decision 5）；test_llm_request_budget stream 分支改 `pytest.raises`，整流器两穿透用例 + react 流式终结回归新增
- [x] **异常树**：`ContextWindowExceededError` 改挂 `NonRetryableError`（不可重试语义，与 CircuitBreakerOpenError 同位；无 except BusinessError 消费方，行为零变化）；test_exceptions 补父类断言
- [x] **API 层**：error_handler 登记 `CONTEXT_WINDOW_EXCEEDED → 422`（原误落 500，区别于 LLM_API_ERROR 502 / CIRCUIT_OPEN 503）+ 映射测试
- [x] **fallback 预算裁定**：fallback 共享主模型窗口（同 provider 同端点 LLM-012，不单独注入配置）；test_open_circuit_fallback 改向为「熔断 OPEN 走 fallback 时预算闸仍生效（主 key 窗口）」
- [x] 顺带修复 react.py:133 except 逗号语法（AGENT-001 元组约定）；test_container 补 `RequestBudgetManager` 全局快照与装配断言、LLMService `_continuation_max_retries` 既有遗漏
- [x] 文档：error_handling（async_generate「错误转事件」例外）/ streaming_rectifier（失败信号表外上抛通道）/ llm（双通道上抛）/ __init__ 组件枚举 / request_budget 契约与测试状态 同步
- [x] 验证：受影响 197 passed；全量 pytest + verify_alignment（见下）
- [ ] 遗留：reflection/planner 结构化调用超限被 `except AppError` 吞（顶部阻塞标注）；Slice 2 取消/成本护栏类型化与 ReAct 收尾其余项；Slice 3/4 阶段上下文缩减

---

# 2026-09-05 Planner Strategy（Plan-then-Execute 单 Agent 编排）+ PlannerAgent 桥接落地

> 产品主链路第一步「主 Agent 拆分」原语落地：PlannerStrategy（规划 → 逐步骤执行 → 证据链汇总）两层结构 + PlannerAgent 桥接 + planning 提示词完整化。

- [x] 实现：`reasoning/planner.py`（PlannerStrategy + PlannerOutcome + PLAN_SCHEMA/PLAN_STEP_SCHEMA/REPLAN_SCHEMA/RESULT_SCHEMA + execute 三阶段 + replan 循环 + best-effort 降级 + PLAN_FAILED 分发）+ `agent/planner.py`（PlannerAgent 桥接，plan/steps_executed/replan_rounds/degraded 进 metadata；replan 预算复用 ctx.max_refine_rounds）+ planning 三模板（PLANNING/REPLAN/SUMMARIZE）+ manager 三 builder（_serialize_step_results）+ AgentErrorKind.PLAN_FAILED（13→14 类，默认 CONTINUE）
- [x] 测试：test_planner 13 + test_planner_agent 3 + test_prompts 5 通过
- [x] 文档：planner.md / planner_benchmark.md 新建；reasoning.md / agent.md / prompts.md / error_handling.md 增量；ALIGNMENT / domain-layer-plan-tmp 登记更新
- [x] ADR：`adr/domain/reasoning/2026-09-05-planner-strategy.md`（两层结构「生命周期依赖」分界 / 每步复用 ReAct execute 取代 execute_tool_calls / replan 取舍与 max_refine_rounds 复用 / PLAN_FAILED / 每步上下文隔离 / 规划器工具定稿）
- [x] 验证：verify_alignment + 全量 pytest

---

# 2026-09-01 Reflection P4 次要项：explicit_abstention 必填 + 单次使用声明 + max_tokens 评估

> 评审遗留 P4 三项：required 缺 explicit_abstention（产品「显式放弃」软契约）、实例非协程安全未声明、critique/refine 未传 max_tokens。

- [x] P4-2：`REFLECTION_SCHEMA.required` 加 `explicit_abstention`（模型必须产出，空数组表示无放弃；jsonschema 校验强制，fixture 均含该字段）
- [x] P4-3：ReflectionStrategy docstring + reflection.md 声明实例单次执行（outcome 覆盖，不并发复用）
- [x] P4-1：评估 critique/refine max_tokens——走 generate_structured 默认预算（`llm_structured_max_tokens`=2048），输出紧凑 JSON 足够；截断由集成层短路返回 None → 降级兜底，**非缺口**，文档说明
- [x] 测试：test_reflection 35 + agent 5 通过
- [x] 文档：reflection.md（Schema 契约 / 边界情况）
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-09-01 Reflection 反思循环终止护栏（P3）：cancel + 超时降级

> 评审发现：Reflection 自查/修正循环只有 cost_limiter 一重护栏，`cancel_event` / `max_execution_time` 只透传 react 收集阶段——用户取消或超时后仍发起付费调用（与 ReAct 三重护栏不对称）。

- [x] reflection.py：`_should_abort`（用户取消 / 总时长超限检查）+ execute 循环顶部调用 → 停机降级采用最近稿（复用 `_finalize` 降级路径）
- [x] 测试：`_should_abort` 单元 3 例（取消 / 超时 / 正常）+ 循环中取消降级集成 1 例；test_reflection 30 + agent 5 通过
- [x] 文档：reflection.md（测试状态 26→30 / 边界情况补 cancel + 超时）
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-09-01 Reflection done 事件口径修复（REASON-011 / P2）+ 抑制 ReAct 中间 done

> 评审发现：`_finalize` 的 done 事件只用 react 阶段 total_tokens，漏计 critique/refine 用量——SSE 事件与 outcome 两个事实源漂移（成本审计失真）；且 Reflection 透传 ReAct 的 done 造成事件流 2 个口径不同的完成事件（噪音）。

- [x] 修复 reflection.py：`_finalize` 的 `build_done_event` total_tokens 改为 `react + _structured_usage`（与 outcome 一致）
- [x] 抑制 ReAct 中间 done：Reflection 透传 ReAct 事件时过滤 done（`AgentEventType.DONE.value` 匹配），事件流仅保留收尾 1 个 done
- [x] 测试：`test_reflect_done_event_tokens_match_outcome` + `test_reflect_suppresses_react_done_event`；test_reflection 26 + agent 5 通过
- [x] 文档：reflection.md 测试状态（22→26）+ issue REASON-011（教训 2 更新为已修复）
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-09-01 llm_doc/error.md：传输错误处理组件文档 + 文档链接收敛

> 用户要求 llm 异常处理文档同步：新建 error.md（组件模板 B），其他文档异常相关说明链接到它（一个事实一个家）。

- [x] 创建 `docs/integration_doc/llm_doc/error.md`（分类 / 归一 / 降级判定 / 下游决策 + 契约归属 + 测试 / ADR / issue）
- [x] retry.md：错误处理两节缩减为链接 error.md（目录 / 架构表 / 对外接口 / 测试状态同步）
- [x] llm.md：模块结构 + 内部组件表 + 对外异常契约链接 error.md
- [x] llm_service.md / structure.md / streaming_rectifier.md：异常说明链接 error.md（顺带修正 `_is_unsupported_response_format_error` 旧名 → `is_unsupported_response_format_error`）
- [x] ALIGNMENT：errors.py 文档列 → error.md；层 README 结构图 / 组件表 / 相关文档登记 errors.py
- [x] 验证：verify_alignment 通过

---

# 2026-09-01 ErrorCategory 契约归属修正：shared → llm/errors.py

> 上移 shared 后复核发现：ErrorCategory 是 LLM 传输层分类，仅集成层 LLM 消费（领域/应用层实际不引用），放 shared 是过早泛化（shared = 被所有层引用的零依赖核心库）。契约与实现同归 `app/integration/llm/errors.py`。

- [x] errors.py：ErrorCategory/ErrorClassifier 契约从 shared/error_category.py 移回本模块（契约与实现同层，单一消费方）
- [x] 删除 app/shared/error_category.py + adr/shared/error_category/（上移决策被修正）
- [x] import 源收敛：retry / streaming_rectifier / test_classify_error / test_error_category → app.integration.llm.errors
- [x] 文档：ALIGNMENT（删 error_category 行 + 更新 errors.py 行）/ retry.md / error_handling.md / issue REASON-010
- [x] 测试：全量 pytest + verify_alignment 通过

---

# 2026-09-01 llm 模块异常处理收敛：新建 llm/errors.py（错误知识 + 决策 helper）

> 用户指出 llm 模块异常处理散乱（分类/归一/降级判定散在 retry.py + structured.py，4 处消费方各自决策）。收敛：新建 `app/integration/llm/errors.py` 作为传输错误处理单一归属 + `decide_downstream_error` 统一 generate 下游决策。

- [x] errors.py：迁移白名单 / `classify_error` / `normalize_transport_error` / `is_unsupported_response_format_error`（自 retry.py / structured.py）+ 新增 `DownstreamDecision` / `decide_downstream_error`
- [x] retry.py：删迁移符号，从 errors 导入 classify_error（保留熔断/重试机制）
- [x] llm_service.py / structured.py：generate /_call_generate except 改用 decide_downstream_error；unsupported 400 降级下一级特判保留 structured（降级链私有语义 + 诊断日志）
- [x] streaming_rectifier.py：classify_error import 源收敛
- [x] 测试：新增 test_errors.py（16 用例：normalize / unsupported / decide 决策矩阵）；test_classify_error / test_error_category import 源更新；全量 755 passed
- [x] 文档：ALIGNMENT（登记 errors.py + 更新 retry/llm_service 行）+ retry.md（错误处理节归属与决策描述）
- [x] 验证：verify_alignment

---

# 2026-09-01 集成层 openai 异常归一（LLMAPIError）+ ErrorCategory 契约上移 shared

> 用户批准归一方案（REASON-010 遗留闭环）并新增「契约上移 shared」决策，实施完整重构（TDD）。归一目标：领域层 `except AppError` 统一兜住集成层透出的不可恢复错误。

- [x] **契约上移**：新建 `app/shared/error_category.py`（ErrorCategory + ErrorClassifier 类型别名 + 契约 docstring，零依赖）；retry.py 删本地定义改 import shared，classify_error 标注结构实现契约；llm_service/structured/streaming_rectifier/test_classify_error import 源收敛（grep 全量核对）
- [x] **异常归一**：`app/shared/exceptions.py` 新增 `LLMAPIError(NonRetryableError)`（code=LLM_API_ERROR，携带 status_code）+ `AppErrorCode.LLM_API_ERROR`；retry.py 新增 `normalize_transport_error`（包装 APIStatusError + 非 HTTP 永久性异常，其余 None）；llm_service.generate except 边界 `raise LLMAPIError(...) from e`（非 openai 原样透传）
- [x] **流式不归一**：async_generate 保持 StreamResult.error 字符串语义（无异常逃逸，不构成缺口）
- [x] **status_code 硬约束**：`_is_unsupported_response_format_error` 依赖 status_code==400 + message 关键词 → 保留两属性，response_format 400 降级链存活（test_generate_structured 既有用例保护）
- [x] **TDD（先写失败测试）**：test_reflection.py 新增 LLMAPIError(401/403) 自查/修正降级 2 用例（修复前红色：模块缺失）；test_llm_service.py 新增归一 4 例（401→LLMAPIError + cause 链 / APIResponseValidationError→status_code=None / ValueError 透传 / APITimeoutError→return None）；test_error_category.py 契约归属 3 例
- [x] **测试**：受影响 189 passed → 全量 740 passed（test_verify_alignment::test_current_repo_passes 在 ALIGNMENT 更新后转绿）
- [x] **文档同步**：error_handling.md（异常清单 LLMAPIError/全景/传播链/四码边界）+ retry.md（契约位置 + normalize_transport_error 节）+ llm_service.md（边界 5）+ llm.md（对外异常契约）+ ALIGNMENT.md（新增 error_category 登记 + 更新 3 行 + 日期）
- [x] **ADR**：`adr/integration/llm/2026-09-01-openai-error-normalization.md`（归一决策）；`adr/shared/error_category/2026-09-01-error-category-contract-up.md` 已删除（契约归属决策被修正，见顶部「契约归属修正」条目）
- [x] **issue 收尾**：REASON-010 遗留标记闭环 + 「遗留闭环」节 + reasoning README 索引登记（REASON-010）
- [x] **验证**：全量 pytest + verify_alignment 通过

> 评审要点：归一位置选 generate 边界而非 retry 层（retry 内部对原始异常分类/记账零扰动）；不复用 UnauthorizedError/ForbiddenError/NotFoundError（BusinessError 承载 API 会话语义，避免混淆）；error_handler 映射 LLM_API_ERROR→502（不映射 401 防误导客户端会话失效）；reflection.py 零代码改动（except AppError 已就位）。

---

> 评审发现：`_critique`/`_refine` 只捕获 StructuredRefusalError/StructuredToolCallError，漏掉会冒泡的不可恢复错误（熔断 CircuitBreakerOpenError 等 AppError）——违反「不抛错降级」承诺。修复：except 扩展为 AppError（异常树根），编程错误仍冒泡不掩盖。迭代核实：截断（StructuredTruncationError）由集成层 StructuredOutput.extract 短路返回 None，非本层缺口，删除虚假锚定测试；openai 4xx/认证未归一 AppError，留集成层遗留。

- [x] issue：`issues/domain/reasoning/2026-09-01-reflection-degradation-coverage.md`（REASON-010）
- [x] TDD：新增 3 用例（自查/修正抛不可恢复 AppError → 降级；编程错误仍冒泡）——初版含 2 截断用例，核实 extract 短路返回 None 后删除
- [x] 修复 reflection.py：`_critique`/`_refine` 的 except `(StructuredRefusalError, StructuredToolCallError)` → `AppError`
- [x] 测试：test_reflection.py（22）+ test_reflection_agent.py（5）通过；全量 pytest 无回归
- [x] 文档：reflection.md（降级路由/边界情况/测试状态/错误分发）+ reflection_benchmark.md（#9）；ALIGNMENT 无模块结构变化，校验通过
- [x] 验证：verify_alignment 通过

---

# 2026-09-04 ReAct 流式/非流式 LLM 通道切换（stream_mode）

> Phase C 后台子 Agent 前置：ReAct 支持非流式通道（generate() 一次拿结果）——单循环换 LLM 调用点，非另一套循环（工业实证：OpenAI run/run_streamed 同 agent loop、Claude include_partial_messages 选项式同构）。

- [x] `react.py`：execute 加 `stream_mode: bool = True` + 主循环第 3 步双通道分叉 + `_llm_round_non_streaming` helper（generate None/AppError→LLM_FAILED、非 AppError→UNKNOWN；整条 reasoning/message 合成）+ 非流式轮末 cancel 补查（仅 False 生效）
- [x] `base.py` AgentContext.stream_mode + `executor.py`_strategy_cycle 透传
- [x] 测试：test_react_strategy_nonstream.py（11 用例：合成事件+model_key/工具循环/None→LLM_FAILED/cancel 补查/AppError→LLM_FAILED/RuntimeError→UNKNOWN/usage/空输出/reasoning-only/final_answer/双通道参数化）+ test_agent 透传哨兵；全量 799 passed
- [x] 文档：react.md（签名/流程第 3 步/架构图/边界 #20 非流式通道契约/测试状态/决策表）/ executor.md / agent.md / error_handling.md
- [x] ADR：`adr/domain/reasoning/2026-09-04-react-stream-channel.md`（新增）+ reasoning-feedback Decision #3 更新（非流式回喂同构，原「非流式不处理」失效）
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-09-01 Reflection 自查/修正 token 统计 + cost 护栏（benchmark #11 去 ⚠️）

> generate_structured 回传 usage（仿 async_generate result 模式），critique/refine 纳入 token/成本统计 + cost_limiter。

- [x] 端口 llm_gateway.generate_structured 加 `usage` 可变参数（返回签名不变，向后兼容）
- [x] LLMService / StructuredOutput（extract / _try_extract / _fallback_extract）透传 + 回填 usage
- [x] ReflectionStrategy：_generate_* 返回 (result, usage)；累计 _structured_usage → outcome.total_tokens/usage 合并；循环顶部 cost_limiter.check 超限停机降级
- [x] 测试：新增 usage 累计 + cost 超限 2 用例；假对象适配 usage 参数；全量 728 passed
- [x] 文档：ADR（cost 缺口 → 完整）/ benchmark #11 ✅ / reflection.md / ports.md / structure.md / llm.md / llm_service.md

---

# 2026-09-01 Reflection 修正循环缺陷修复（REASON-009）

> 审查发现：修正循环复用同一批 issues 反复修正（工业反模式）。调研确认「迭代必须新反馈」后重构为真迭代。

- [x] issue：`issues/domain/reasoning/2026-09-01-reflect-refine-loop.md`（发现→分析→修复→验证→教训）
- [x] 重构 reflection.py：阶段二+三改为 while 真迭代（修正 refined → 重新自查 refined → ok 采用 / 新 issues 再修正 → 达上限 best-effort）
- [x] 达上限判断修正：`refine_round >= max_refine_rounds - 1`（max=1 时初稿有 issues 即不修正）
- [x] 测试：新增真迭代 2 用例（复查新 issues 再修正 / 达上限采用最后稿）+ 更新 issues_refine 测试，22 passed
- [x] 文档：ADR（re_critique 决策改为真迭代 + REASON-009 链接）/ reflection.md / reflection_benchmark.md（#5/#17）
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-31 React 策略完成度/职责划分评估 + 文档漂移同步 + 遗留问题收尾

> 两项子智能体评估（完成度 26 项对照 + 策略/编排职责划分）→ 文档漂移同步 4 份 → 未解决问题清单 #1~#4 逐项修复（REASON-006/007/008 + 取舍标注），全部提交推送。

- [x] **完成度评估**：核心必备 13/13 + 增强 8 项，26 项 ✅21 / 🔶1（沙箱降级）/ ❌4（guardrail/compaction/checkpointer/final_answer_checks 均「不做或预留」）；无过设计，达工业级核心基线
- [x] **职责划分评估**：代码层「策略=算法、编排=生命周期」无越界；文档滞后于 08-30 三批能力（2 高危 + 4 中危 + 数低危漂移）
- [x] **文档漂移同步**：executor.md（`__init__` 契约补 cost_limiter/cancel_event、优雅/硬取消、测试数 5→8）、agent.md（异常契约 9→12 类）、react.md（架构图 12 类、execute 签名、max_turns 文案、终止分支表、reasoning 语义、_finalize_terminal 复用）、react_benchmark.md（#4 文案、#23 计数）
- [x] **问题 #1**（REASON-004 扩展）：无工具 + 非空 tool_calls → 协议异常短路（`or not has_tools`）；REASON-006 并入 REASON-004 单档案
- [x] **问题 #2**（REASON-007）：LLM 失败重试独立上限 `max_llm_fail_retries`（settings→AgentContext→透传→execute 完整链路 + 硬终止，对齐空输出护栏）
- [x] **问题 #3**：LLM_FAILED 不脱敏 vs UNKNOWN 脱敏取舍标注（诊断价值 vs 无诊断价值，文档）
- [x] **问题 #4**（REASON-008）：空输出重试轮不追加空 assistant 消息（纯空轮不写历史）+ AGENT-001 except 元组回归
- [x] 验证：全量 700 passed + verify_alignment；提交推送（c9e7f00 / 0d2bed2 / 5b67e60）

---

# 2026-08-31 Reflection 推理决策 + ReflectionAgent 编排（领域层 Slice 4）

> 以工业级 Reflection 决策为评价指标（reflection_benchmark.md 对标）。三阶段分离 + Grounding（证据锚定，反内在自查）。

- [x] 共享内核：AgentErrorKind 加 CRITIQUE_FAILED（默认 CONTINUE，12→13 类）+ 配置 agent_max_refine_rounds
- [x] `reasoning/reflection.py`：ReflectionStrategy（生成复用 ReAct.execute(output_schema) + 自查 CRITIQUE_SCHEMA + 修正 REFINE_SCHEMA + 降级路由）+ ReflectionOutcome + schema 契约
- [x] prompts：templates/reflection.py（REFLECTION_SYSTEM / CRITIQUE_PROMPT 9 维清单 / REFINE_PROMPT）+ manager builder
- [x] `agent/reflection.py`：ReflectionAgent 桥接（构造注入 + _map_outcome 进 metadata）
- [x] 测试：test_reflection.py（15 用例）+ test_reflection_agent.py（5 用例），全量 723+ passed
- [x] 文档：reflection.md / reflection_benchmark.md（核心必备 12 项对照）/ reasoning.md / agent.md / config.md / ALIGNMENT / domain README
- [x] ADR：`adr/domain/reasoning/2026-08-31-reflection-strategy.md`
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-30 Agent 运行异常处理全面性评审（5 问题逐项修复）

> 评审 Agent 运行异常处理全面性，发现 5 个问题（部分进度 / 取消语义 / 协议异常 / handler 防御 / error 脱敏），按问题修复工作流逐项完成（测试驱动 → issue → 文档 → 停等审核），全部修复并推送。

- [x] 问题 1（REASON-002）：UNKNOWN 未保留部分进度 → `_finalize_unknown` 用 last_result 组装 outcome（对齐 TIMEOUT/COST_EXCEEDED/STALLED 部分进度保留模式）
- [x] 问题 2（REASON-003）：取消信号语义错位 + /chat/stop 未接线 → cancel_event 链路贯通（ReAct 识别 → CANCELLED，不重试）+ TaskService 会话级取消注册表 + chat 真实优雅取消
- [x] 问题 3（REASON-004）：finish_reason=tool_calls 但 tool_calls 空 → 短路 PARSE_FAILED 协议异常分发（不入空输出计数 / 不进空转执行，默认重试）
- [x] 问题 4（SHARED-001）：错误处理 handler 自身异常无防御 → ErrorHandlerRegistry.dispatch 捕获降级默认 action（except Exception，CancelledError 穿透不吞）
- [x] 问题 5（REASON-005）：UNKNOWN error 拼接完整异常文本泄漏内部细节 → 只留异常类型名（产品侧脱敏）+ 完整异常含 traceback 进日志（运维诊断）
- [x] 验证：全量 692 passed + verify_alignment 通过；提交推送至远端（05a3f5d / 问题2 / 9a4ed74 / 91e5ecc / f7b5861 / b8277b7）

---

# 2026-08-30 Token 计量并入 LLMGateway：移除 TokenCounter 端口

> 延续 Facade 收窄原则：Token 计量（模型特定编码）同成本估算一样是 LLM 能力，从独立 `TokenCounter` 端口并入 `LLMGateway`（端口 6→5），`ContextManager` 经 `LLMGateway.count_*` 接入，不再接触独立端口。

- [x] `llm_gateway.py`：加 `count_tokens` / `count_messages_tokens`（LLM 能力归属）
- [x] `llm_service.py`：委托 `TiktokenTokenCounter`（主模型，惰性构建）
- [x] 删除 `domain/ports/token_counter.py` + `__init__` 导出（端口 6→5）
- [x] `context_manager.py`：`token_counter` → `llm`（LLMGateway 端口）
- [x] container：ContextManager 移到 LLMService 之后，`llm=llm_service`
- [x] 测试：test_chat_flow / test_react_strategy / test_container 构造改 `llm=`（位置参数自动绑定）
- [x] 文档：ports.md(5 端口) / context.md / ALIGNMENT / llm.md / token_counter.md / 层 README / types.md / ADR
- [x] 验证：全量 648 passed + verify_alignment

---

# 2026-08-30 LLM 包 Facade 收窄：`__init__` 只导出 LLMService

> 对齐 llm.md 既有原则「LLMService 是唯一对外 Facade，内部组件不对外暴露」：此前 `llm/__init__.py` 全量导出 12 个子组件（ClientManager/CostTracker/RetryHandler/…）与原则矛盾。

- [x] `llm/__init__.py`：收窄为仅 `from .llm_service import LLMService`（对外接口 = Facade）
- [x] `llm_service.py`：包内组件改相对深路径 import（消除依赖 `__init__` 重导出的循环）
- [x] container.py（装配根）：`register_config` 接线改深路径 import（组合根例外，消费方仍只见 LLMService）
- [x] 测试：test_container / test_client_manager / test_stream_rectify 深路径对齐
- [x] 验证：全量 648 passed + verify_alignment

---

# 2026-08-30 成本上限自动停机（增强项 #20）+ 断点续跑 ADR（#19 升级路径）

> 对标收尾：#20 CostTracker 记账有、无 cost ceiling 自动停机。经 CostLimiterPort 端口注入（镜像 ContextBudgetPort），累计成本超限走 COST_EXCEEDED 分发默认 STOP 停机。约束：react.py 只依赖 ports+shared；成本估算是 LLM 能力，归属 LLMGateway.calculate_cost，应用层 CostLimiter 经该端口取成本（不直接 import 集成层子组件）。

- [x] `domain/ports/cost_limiter.py`：CostLimiterPort（check 无状态纯函数，返回 exceeded+cost）
- [x] `domain/ports/llm_gateway.py`：LLMGateway 加 `calculate_cost`（成本估算归 LLM 能力，Facade 结构实现）
- [x] `application/context/cost_limiter.py`：CostLimiter（ceiling + llm 端口注入 + model 配置）
- [x] `error_handling.py`：AgentErrorKind.COST_EXCEEDED（10 类，默认 STOP）+ 默认表 + 测试
- [x] react.py：构造注入 + 每轮 usage 累加后检查（error 判断前）+ `_finalize_cost_exceeded` 降级
- [x] 注入链：settings `agent_max_cost` → container.cost_limiter 单例（llm=llm_service Facade）→ deps.get_cost_limiter → chat → ReActAgent；AgentContext 不改
- [x] 测试：test_cost_limiter 7 例 + react 6 例 + error_handling/container/settings/agent 同步
- [x] 文档：react.md / react_benchmark（#20 ✅）/ ports.md / context.md / config / .env.example / ALIGNMENT / cost_tracker.md
- [x] [ADR #20](../adr/domain/reasoning/2026-08-30-cost-limit.md) + [ADR #19 checkpointer-resume](../adr/domain/reasoning/2026-08-30-checkpointer-resume.md)（升级路径，不实现）
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-30 上下文预算放置位置修复（REASON-001）

> 审查发现：预算原放 `_handle_tool_calls` 尾部，仅覆盖「工具调用 → 回喂」路径；LLM 失败重试 / final_answer 回喂重试 / 空输出重试三条非工具继续路径漏裁，上下文无限增长、护栏失效。根因：护栏放置点锚定「工具调用后」而非「LLM 调用前」（横切护栏应锚定其约束的调用点）。

- [x] 测试驱动：新增 `test_react_context_budget_trims_on_no_tool_retry`（空输出重试路径，修复前 assistant=6 失败 → 修复后 ≤3）
- [x] 修复：预算移至主循环顶部（第 0 步，每次 LLM 调用前裁剪，所有继续路径共用）；`_handle_tool_calls` 移除预算与 `max_context_rounds`/`max_context_tokens` 参数
- [x] 文档：react.md（上下文预算节 / 行为边界 / `_handle_tool_calls` 行 / 测试节 30→31）+ [REASON-001 问题记录](../issues/domain/reasoning/2026-08-30-context-budget-placement.md)
- [x] 验证：react 31 passed + 全量 pytest + verify_alignment

---

# 2026-08-29 config 文档重构：移出优化建议为 backlog

> 依 module_doc 规范重构 `docs/config_doc/config.md`，原「后续优化建议」为规划内容（Rule 2 写当前状态），移出登记为 backlog，出现真实需求再落地。

- [ ] 缺失配置项：`AGENT_ENABLE_REFLECTION` / `AGENT_MAX_TOOL_CALLS` / `DATABASE_SSL_MODE` / `DATABASE_CONNECT_TIMEOUT` / `REDIS_MAX_CONNECTIONS` / `LOG_MAX_FILE_SIZE` / `LOG_BACKUP_COUNT` / `RATE_LIMIT_REQUESTS` / `RATE_LIMIT_PERIOD`
- [ ] 默认值优化：`AGENT_TIMEOUT`→600s / `DATABASE_POOL_SIZE`→50-100 / `AGENT_MAX_CONCURRENT_TASKS`→20-50 / `JWT_EXPIRE_MINUTES`→60-480
- [ ] 架构优化：`LLM_` 前缀统一（`MAX_CONTEXT_TOKENS`）/ 配置热更新 / 启动 `model_validator` 依赖检查 / 多环境 `env_file` 模式 / `export_yaml` / `export_json`

---

# 2026-08-28 异常体系完整优化（error_handler 边界 + 命名 + API 收敛）

> 工业级调研（OpenAI/LangChain/Spring AI/SMOL/FastAPI）：分层分类 + 宽松统一基类是正确形态，四套分类（传输可重试/工具系统码/Agent 编排/对外业务码）不应合并。真实缺口是边界未接线 + 命名冲突。

- [x] 调研工业级异常体系设计（统一 vs 分层结论 + 诊断）
- [x] `error_handler.py` 实现（AppError → HTTP 状态 + 统一信封）+ main.py 注册
- [x] `exceptions.py`：加 UNAUTHORIZED/FORBIDDEN/NOT_FOUND 码 + 3 异常
- [x] API 层收敛：deps/session/chat 8 处 HTTPException → AppError 子类
- [x] 命名修正：`AgentError` → `AgentRunError`（解耦语义）
- [x] 测试：error_handler 7 用例 + test_api 信封断言
- [x] 文档：error_handling.md / ALIGNMENT（error_handler ✅）+ [ADR](../adr/shared/exceptions/2026-08-28-exception-system-optimization.md)
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-28 Agent 错误处理策略可扩展（领域层横切，调研 + 决策定稿）

> 对标增强项 #23（⚠️）：先调研工业级详细内容并记录决策文档。四要素「错误分类→注册机制→分发时机→决策动作」；回喂/拦截双层；本项目选 OpenAI 式 ErrorHandlerRegistry + BaseAgent 横切注入。

- [x] 调研工业级错误处理扩展机制（OpenAI Agents SDK / LangGraph / Claude SDK / SMOLagents / LangChain / AutoGen，源码级）
- [x] [ADR](../adr/domain/agent/2026-08-28-agent-error-handling.md)：工业级对照 + 设计方向（AgentErrorKind + Handler 协议 + Registry + BaseAgent 注入）
- [x] 实现：`app/shared/error_handling.py`（共享内核）9 kind 全接入——BaseAgent 注入 + run 分发；ReAct 循环 6 处错误分支改为分发（默认行为零变化）；测试 13 新增（registry 单元 + 分发集成 + run 上抛）

---

# 2026-08-28 结构化输出约束（增强项 #15，Final Answer 工具模式）

> 工业界调研定稿：严格 output_type 会抑制工具调用；选 Final Answer 工具（模型原生结构化 + 循环终止 + 无额外调用）。决策过程（时机/方式/取舍）完整沉淀于 ADR。

- [x] `react.py`：execute 加 `output_schema`，注入 final_answer 工具；识别 → 成功终止（outcome.structured）/ 校验失败回喂（VALIDATION）
- [x] 数据契约：ReActOutcome / AgentResult 加 `structured` + `_map_outcome` 透传
- [x] 测试：3 用例（成功提取终止 / 校验失败回喂 / 未配置不注入）
- [x] 文档：react.md / react_benchmark（#15 ✅）+ [ADR](../adr/domain/reasoning/2026-08-28-structured-output.md)（含讨论概念）
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-28 上下文预算管理（核心必备 #10，对标收尾）

> 分层调研定稿：预算管理是横切能力（所有 Agent 模式共享），归 context_manager 统一（复用 TokenCounter），经领域端口 ContextBudgetPort 注入 Agent。双层护栏：轮次（保配对原子）+ token 硬上限。

- [x] `domain/ports/context_budget.py`：ContextBudgetPort 端口 + 登记
- [x] `context_manager.trim_messages`：轮次滑动窗口 + token 预算（复用 TokenCounter，补 tool_calls/reasoning 低估修正）
- [x] 注入链：settings `agent_max_context_rounds=8` / AgentContext 两字段 / ReActAgent 注入 / container / chat
- [x] 测试：context_manager 3 用例（轮次/ token / noop）+ react 集成 + agent_params 同步
- [x] 文档：react.md / react_benchmark（**13/13 完备**）/ config + [ADR](../adr/domain/reasoning/2026-08-28-context-budget.md)
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-27 reasoning_content 回喂策略（文档更正 + has_reasoning 防御）

> 对标 P2 标注为错误外推（误用 OpenAI o1 规则）——DeepSeek V4 thinking + tools 必须回喂 reasoning_content（否则 400）。补 has_reasoning 信号防御空 reasoning 边界。

- [x] `StreamResult` / `ParsedChunk` 加 has_reasoning；parse_chunk 字段被填充（含空串）即置位
- [x] rectifier `_apply_chunk` 传递；react.py 回喂条件改 `full_reasoning or has_reasoning`
- [x] 测试：parse_chunk has_reasoning 三态 + react 回喂两用例
- [x] 文档：react_benchmark P2 更正移除 / react.md / [ADR](../adr/domain/reasoning/2026-08-27-reasoning-feedback.md)
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-27 截断标记（P3）

> 对标确认工具结果回喂截断无标记，模型误以为结果完整。修复：截断时追加 `[结果已截断]`（预留标记长度）。

- [x] `react.py`：`_truncate_with_marker` 辅助 + tool 消息（2000）/ SSE（200）两处应用
- [x] 测试：2 用例（长结果带标记不超限 / 短结果无标记）
- [x] 文档：react.md（测试节 16 用例）/ react_benchmark.md（#9 瑕疵移除，P3 完成）+ ADR 并入
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-27 解析 / JSON 失败降级（核心必备 #7）

> 对标确认解析失败静默降级空参（掩盖错误）。修复：解析失败不执行工具，构造失败 ToolResult（JSON_PARSE）走失败回喂闭环。

- [x] `react.py` _execute_one：解析失败不执行工具，构造失败 ToolResult 回喂 + JSON_PARSE 证据链
- [x] 测试：1 用例（不执行 / 回喂解析失败 / 证据链 JSON_PARSE）
- [x] 文档：react.md / react_benchmark.md（#7 ✅，12 完备 / 0 部分 / 1 缺失）+ ADR 并入
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-27 工具异常回喂 + 无效工具名处理（核心必备 #5/#6，P0）

> 对标 [react_benchmark.md](domain_doc/reasoning_doc/react_benchmark.md) 确认工具失败信息不回喂模型：失败 `content=""` 回喂空串，模型无法自愈；`error` 未进证据链。修复根因 = 失败信息在两个出口同时丢失。

- [x] `react.py` execute_tool_calls：失败回喂 `str(result)`（"错误: <error>"），成功回喂 content；证据链记录加 `error` / `error_code`
- [x] 无效工具名：NOT_REGISTERED 走同一失败回喂分支，无需额外逻辑
- [x] 顺带修复 AGENT-001：except 逗号语法 → 显式元组
- [x] 测试：`_FailingTool` + 3 用例（失败回喂模型 / 证据链记录 / 无效工具名）
- [x] 文档：react.md / react_benchmark.md（#5/#6 ✅，11 完备 / 1 部分 / 1 缺失）+ [ADR](../adr/domain/reasoning/2026-08-27-tool-error-feedback.md)
- [x] 验证：全量 pytest + verify_alignment

---

# 2026-08-27 ReAct 策略总时间上限（max_execution_time）

> 对标 [react_benchmark.md](domain_doc/reasoning_doc/react_benchmark.md)（核心必备 #2）确认时间上限缺失；`settings.agent_timeout=300` 存在未使用，接入为 ReAct 循环总时长护栏。

- [x] `react.py`：execute 加 `max_execution_time`（None=不限）+ `asyncio.timeout` 包循环 + 超时降级（对齐 max_iterations 兜底，判别 finalizer 关闭）
- [x] 注入链：base.py AgentContext 字段 / executor.py 透传 / container.py agent_params / chat.py / deps.py docstring
- [x] 测试：新增 4 用例（首轮超时 / 中途超时保留进度 / 宽松上限 / None 不触发）+ 3 处 agent_params 覆盖同步
- [x] 文档：react.md / react_benchmark.md / config.md + [ADR](../adr/domain/reasoning/2026-08-27-reactor-max-execution-time.md)
- [x] 验证：全量 593 passed + verify_alignment 通过

---

# 2026-08-27 领域层推理决策架构完整建设（Phase C 前置）——总体规划与进度

> 完整计划见 [domain-layer-plan-tmp.md](domain_doc/domain-layer-plan-tmp.md)（临时计划文件，源自 2026-08-27 会话记录）。
> 目标：领域层四模块（策略 / prompt / 记忆 / 编排）建设完整、跑通单 Agent 任务后再上应用层多任务编排（Orchestrator 依赖 PlannerAgent 拆分）。
> 产品锚点：PlannerAgent.plan() = 主 Agent 拆分原语（Phase C 主链路第一步）。

- [x] **Slice 0** ReAct 抽离：reasoning/react.py（ReActStrategy 收标量参数）+ executor.py 变薄桥接（保留 _execute_tool_calls 转发）
- [ ] **Slice 1** Prompts 完整化：planning.py 从 draft 完整化（depends_on / re-plan / 汇总）+ reflection 模板 + manager 3 builder + run() 默认 system 注入
- [ ] **Slice 2** Memory 基座：VectorStorePort + memory/ 六文件（端口 + 骨架，向量实现留 Phase D）
- [ ] **Slice 3** PlannerAgent：agent/planner.py（plan 结构化拆分 / depends_on 拓扑执行 / replan 限 2 次 / summarize 降级）
- [x] **Slice 4** ReflectionAgent：reasoning/reflection.py + agent/reflection.py（✅ 2026-08-31 完成，见当日条目）
- [ ] **Slice 5** 装配接线：container / deps / chat / task_service / base（memory_enabled 默认 False 惰性零行为变化）
- [ ] **Slice 6** 文档 + ADR + 对齐：ALIGNMENT / architecture / 各模块文档 + 4 ADR / verify_alignment

## 关键设计决策

- ReActStrategy 收标量参数 → 遵守 reasoning→ports+shared 依赖方向，策略可独立测试
- PlannerAgent 执行阶段复用 execute_tool_calls 不复用完整 ReAct 循环（plan-then-execute 灵魂）
- generate_structured 返回 None 降级：plan None→ReAct 兜底；summarize None→纯文本；critique None→采用初稿
- 记忆不 mock 向量库：LongTermMemory 端口注入，None 时 no-op（Phase D 接 Milvus）
- 记忆避免孤儿：BaseAgent 完成钩子 + chat 接线（memory_enabled 默认 False 惰性）

---

# 2026-08-27 领域层推理决策架构建设 · Slice 0：ReAct 策略抽离

> Phase C 前置：领域层四模块完整建设（策略 / prompt / 记忆 / 编排），单步执行。
> Slice 0：ReAct 循环逻辑从 `agent/executor.py` 抽离到 `reasoning/react.py`（ReActStrategy），executor.py 变薄桥接。

- [x] `reasoning/react.py`：ReActStrategy + ReActOutcome + execute()（完整循环）+ execute_tool_calls()（工具并行原语）
- [x] `agent/executor.py`：ReActAgent 桥接（_strategy_cycle 委托 + _map_outcome + _execute_tool_calls 转发）
- [x] `reasoning/__init__.py`：导出 ReActStrategy / ReActOutcome
- [x] 新增 test_react_strategy.py（6 用例）：工具循环 stop / error 短路 / 空输出重试 / 迭代兜底 / 并行保序 / 并发
- [x] 文档：reasoning.md（react.py ✅）/ ALIGNMENT / agent.md（executor 桥接定位）
- [x] ADR：`adr/domain/agent/2026-08-27-react-strategy-extraction.md`
- [x] 验证：全量 589 passed（既有 test_agent 3 用例零改动 + test_chat_flow 集成链路通过）

---

# 2026-08-24 Embedding 端口化落地（Phase B 剩余任务 #4，最后一项）

> 架构文档端口层目标：EmbeddingPort。孤儿服务 EmbeddingService 端口化（结构实现），补测试；RAG 接线留 Phase D。

- [x] `app/domain/ports/embedding_port.py`：EmbeddingPort 协议（embed / embed_batch），ports/**init** 登记
- [x] `embedding_service.py`：docstring 标注结构实现 EmbeddingPort（签名已匹配，构造依赖保持）
- [x] 新增 test_embedding.py（9 用例）：单文本/批量保序/分批切批/缓存命中/缓存穿透/disable_cache/空输入/clear_cache
- [x] 文档：ALIGNMENT（embedding_service ✅ + 端口登记）/ architecture（EmbeddingPort ✅、Phase B 全完成）/ embedding.md / domain README
- [x] ADR：`adr/integration/embedding/2026-08-24-embedding-port.md`
- [x] 验证：全量 pytest + verify_alignment 通过

---

# 2026-08-24 types 通用类型落地（Phase B 剩余任务 #3，最小集）

> 架构文档共享内核目标：通用类型 / 标识。最小集只做有真实消费方的类型（SessionId/UserId 标识 + Messages 消息别名），不做 Task 枚举（Phase C）。

- [x] `app/shared/types.py`：SessionId/UserId（NewType）+ Messages（TypeAlias）
- [x] 签名标注：SessionManager 9 方法 + ContextManager.build_messages + AgentContext 字段 + 端口契约（llm_gateway/token_counter）
- [x] 边界保持 str：Pydantic schemas / deps / 路由 / SQLAlchemy Column / dict key
- [x] 新增 test_types.py（4 用例）：NewType 运行时恒等 + 别名等价
- [x] 文档：types.md（新建）/ ALIGNMENT / architecture / todo
- [x] ADR：`adr/shared/types/2026-08-24-type-identifiers.md`
- [x] 验证：全量 pytest + verify_alignment 通过

---

# 2026-08-24 exceptions 统一异常体系落地（Phase B 剩余任务 #2）

> 架构文档共享内核目标：统一异常与错误码。7 个平级散落异常收敛到 `app/shared/exceptions.py`，集成层 re-export。

- [x] `app/shared/exceptions.py`：AppError 根 + NonRetryableError/BusinessError 两支 + AppErrorCode（7 码）
- [x] 收敛：retry/structured/security/validator 删本地定义 + re-export（测试零断裂，相关 135 passed）
- [x] 新增 test_exceptions.py（8 用例）：树结构 / 错误码 / message / ValueError 多重继承 / re-export 兼容
- [x] 文档：error_handling.md（异常树 + 清单表）/ ALIGNMENT / architecture / todo
- [x] ADR：`adr/shared/exceptions/2026-08-24-exception-hierarchy.md`
- [x] 验证：全量 pytest + verify_alignment 通过

---

# 2026-08-24 TokenCounter 端口落地（Phase B 剩余任务 #1）

> 架构约束「零外部框架依赖层」：tiktoken 隔离到集成层。端口 + 实现 + ContextManager 改造 + 测试 + 文档 + ADR 全链路。

- [x] `app/domain/ports/token_counter.py`：TokenCounter 协议（count_tokens / count_messages_tokens），`ports/__init__.py` 登记
- [x] `app/integration/llm/token_counter.py`：get_encoder / content_to_text / TiktokenTokenCounter（tiktoken 唯一使用点）
- [x] `llm_service.py`：`_get_encoder`/`_content_to_text` 迁出 + 别名 import（单一事实源，test_llm_service 零断裂）
- [x] `context_manager.py`：去 tiktoken、注入 TokenCounter、count_* 委托（顺带修复 content=None 潜在 TypeError）
- [x] 测试：新增 test_token_counter.py（8 用例）+ 适配 test_context_manager / test_api / test_chat_flow / test_container
- [x] 文档：ALIGNMENT / domain README / context.md / architecture / token_counter.md / llm_service.md / deps 注释
- [x] ADR：`adr/integration/llm/2026-08-24-token-counter-port.md`
- [x] 验证：全量 562 passed + verify_alignment 通过

---

# 2026-08-20 工具模块代码审查修复（TOOLS-049）

> 来源：工具模块整体代码审查（四维度：正确性 / 安全 / 性能 / 规范）。完整生命周期见 [TOOLS-049 问题记录](../issues/integration/tools/2026-08-20-code-review-fixes.md)。

- [x] **重要项 5**：executor 重试全败归因 / SSRF CGNAT 盲区 / 审计脱敏 error+content / 外部工具冷启动扫描 / RCA 证据链时间锚点（各带回归测试）
- [x] **次要项 15**：executor（execution_time/注释）、loader（_drop_modules 前缀过滤）、assembler（单工具失败隔离）、security（敏感键正则/DNS 注释）、result_processor（docstring）、validator（完整路径）、tool_gateway（**str** 兜底）、prompts（截断提示）、RCA（FDC 判定/空结果归因/冗余 int）、hooks（async 注释）
- [x] **取舍项保持现状**：validator schema 缓存 / 嵌套 additionalProperties / getaddrinfo 超时 / loader 重载竞态 / scan_once 线程化 / 裸 IP 拒绝（ADR 保守策略）
- [x] **文档同步**：executor / security / rca / tool_service / external + ALIGNMENT verify
- [x] **全量回归**：uv run pytest 通过

---

# 2026-08-17 工具模块重构：对齐工业级六大子组件

> 背景：工具模块原为 Facade + 5 组件（registry/executor/stats/hooks/assembler），对照工业级 Agent 工具模块存在差距（参数校验仅查未知+必填、无统一结果处理、无风险分级与审计、无选择机制）。网络调研工业级方案后，与用户「六大子组件」蓝图对比整合，确认四个方向性决策：全盘对齐六大子组件 / 安全分级+审计留痕（不拦截）/ 选择器只留接口不实现 / 引入 jsonschema。

## 任务清单（垂直切片）

- [x] **Slice 0** Facade 骨架 + 最小链路：新建 selector / validator / result_processor / security 四组件，wire 进 ToolService/executor，既有测试全绿（12 passed）
- [x] **Slice 1** validator 细化（iter_errors 全量收集 + 中文归因 + reject_unknown + 类型名映射）；base.py 委托改造；`test_tool_validator.py`（13 用例）
- [x] **Slice 2** result_processor 细化（head+tail 截断 + 错误归一化）；内置工具删内联截断 + 元数据（risk/category/concurrency_safe/max_output_length）；`test_result_processor.py` + readFile 大文件截断集成用例
- [x] **Slice 3** security 细化（RiskLevel L0-L3 + ToolAuditor 审计到日志）；executor 审计全路径接入 + per-tool 串行化锁；`test_tool_audit.py` + executor 组件测试
- [x] **Slice 4** selector 接入 get_openai_tools；修 domain/agent/executor.py:210 PEP 758 语法；`test_tool_selector.py` + `test_tool_registry_metadata.py` + `test_tool_executor_components.py` + `test_tool_hooks.py`
- [x] **Slice 5** 文档：tools.md 重写为模块接口文档 + tool_service.md/builtin.md/集成层 README 更新 + validator/result_processor/security/selector 四子文档 + ALIGNMENT + ADR×3 + issue + verify_alignment 通过

## 评审（2026-08-17）

- **全量测试 414 passed**（原 12 工具测试 + 新增 ~40 用例），无回归
- **`uv run python -m scripts.verify_alignment` 通过**（4 新组件已登记，文档死链清零）
- **Container 装配冒烟**：5 工具注册 + code_exec 正确标注 L2_DANGEROUS + 审计默认启用
- **受控审计冒烟**：search 未配置 key 优雅失败路径触发 `tool_call` 审计事件
- **新增组件**：selector（接口+全量注入）/ validator（jsonschema 严格校验）/ result_processor（head+tail 截断）/ security（分级+审计）
- **行为变更**：参数校验从「未知+必填」升级为 jsonschema 完整校验（LLM 传字符串化数字会校验失败并归因）——ADR-002 记录；结果截断从「只留前 N」升级为 head+tail（含 marker）
- **顺带修复**：domain/agent/executor.py `except json.JSONDecodeError, KeyError:` → 显式元组（PEP 758 可移植性，issue AGENT-001）；hooks.py `asyncio.iscoroutinefunction` → `inspect`（3.16 弃用告警）

### 遗留（工具模块重构）

- `app/main.py:27`、`app/integration/llm/retry.py:595` 同型 PEP 758 逗号语法，超出本次范围，仅 issue AGENT-001 记录待后续处理
- 审计密钥脱敏（params 中的 api_key 等）列为未来增强；审计默认常开（不设 settings 开关）
- 选择器向量召回（embedding 粗排 + LLM 精排）留待工具数 >50 时实现（ADR-001 记录升级路径）

---

# 2026-08-15 文档/测试以代码架构为准对齐

> 背景：代码已迁移到新分层（domain/application/integration/infrastructure/shared），
> docs 仍按旧分层（core_doc/service_doc/integration_doc/tools_doc/model_doc），tests 无对齐清单。
> 已确认方案：docs 目录镜像 + ALIGNMENT.md 映射表 + 轻量 verify_alignment.py；模块 README 暂不加。

## 任务清单

- [x] 1. 建立 `docs/ALIGNMENT.md` 映射表（代码模块 ↔ 状态 ↔ 文档 ↔ 测试）
- [x] 2. 文档目录镜像迁移
  - [x] 2a. `service_doc` → `application_doc`（session/context/task）+ `integration_doc`（llm/embedding/tools）
  - [x] 2b. `core_doc` → `domain_doc`（agent/memory/prompts/reasoning）
  - [x] 2c. `tool_doc` → `integration_doc/tools/`，`model_doc` → `infrastructure_doc/models/`
  - [x] 2d. 新建 `shared_doc/events.md`（对应 `app/shared/events.py`）
- [x] 3. 修复全部旧路径交叉链接（docs 内互链 + README/HANDOFF/AGENTS）
- [x] 4. 编写 `scripts/verify_alignment.py` + `tests/unit/test_verify_alignment.py`
- [x] 5. 运行 `uv run pytest` 全量验证
- [x] 6. 同步 README / HANDOFF / AGENTS 的旧分层描述
- [x] 7. 补齐缺失测试（test_session_manager / test_context_manager / test_container / test_settings / test_events + integration/e2e 空文件）

## 评审

### 完成情况（2026-08-15）

- 文档目录已镜像到 `app/` 顶层：`application_doc / domain_doc / integration_doc / infrastructure_doc / shared_doc / platform_doc`（`api_doc / config_doc` 保留）；`utils_doc` 已废弃迁移（logger/metrics → platform_doc/observability，error_handling/class-design → shared_doc）。
- 旧路径链接（`docs/service_doc`、`docs/core_doc`、`docs/tool_doc`、`docs/model_doc` 及 `app/services`、`app/core`、`app/models`、`app/tools`、`app_state`）全部替换为新路径。
- `docs/ALIGNMENT.md` 登记全部代码模块；`scripts/verify_alignment.py` 校验三处对齐；新增 10 个单测。
- 全量测试 `224 passed`（原 214 + 新增 10）。

### 遗留

- 空目录 `docs/service_doc`、`docs/core_doc`、`docs/tool_doc`、`docs/model_doc` 因删除被沙箱策略拦截仍存在，git 不追踪、不影响运行，待环境允许时清理。

### 第 7 项完成情况（2026-08-15）

- 新增 5 个单元测试文件，`ALIGNMENT.md` 对应条目 🔶 → ✅：
  - `test_events.py`（13 用例）· `test_settings.py`（~24 用例）· `test_context_manager.py`（10 用例）· `test_session_manager.py`（22 用例，手写 `_FakeRedis`/`_FakeDB` 按 SQLAlchemy 语句分发）· `test_container.py`（6 用例，stub Redis/引擎 + autouse 恢复全局注册表）。
- 填充两个空测试文件：`test_tool_execution.py`（6 用例，内置工具真实执行，code_exec 用 `sys.executable` 防 PATH 依赖）、`test_api.py`（5 用例，TestClient 不触发 lifespan + monkeypatch container 服务）。
- `test_memory.py` 保持空文件（目标模块 `app/domain/memory/` 全为 0 字节空壳，无可测内容，与 ⬜ 状态一致）。
- 全量测试 `325 passed`（原 224 + 新增 101）；`scripts/verify_alignment.py` 校验通过。

### 教训（2026-08-15 补充）

- `_FakeDB` 语句分发不能依赖 `column_descriptions[0]["entity"]`：聚合 select（stats/count）的 entity 也是 FROM 映射类。改用 `descs[0]["expr"]` 是否为映射类（type）区分实体行查询与函数聚合查询。
- SessionManager 构造参数 `db_session_factory` 存为 `self.db_session`（非同名属性）；fake 的 db_session 必须实现 `__aenter__/__aexit__`（`async with self.db_session() as db`）。
- 复用 fake 时要清理其状态：`list_sessions` 首页会写缓存，连续两次调用需 `fake_redis.data.clear()`，否则第二次命中缓存不查库、断言落空。
- 内置工具测试显式 `register_config(api_key="")` 重置 key，否则会读到仓库根 `.env` 的真实 TAVILY_API_KEY 触发真实网络请求。

---

# RateLimiter 审核问题修复计划

> 来源：`docs/llm/rate_limiter.md` 附录「2026-08-01 代码审核记录」6 个遗留问题。
> 方式：**逐个修复**，每修完一个停下来总结并更新文档。

---

# 结算退差 + reserve/settle（后续任务）

> 承接「工业级对比」章节的可改进点（对比 3/4），2026-08-02 已实现。

- [x] `rate_limiter.py`：TokenBucket.refund + Reservation + ReservationTokenBucket + ReservationRateLimiter（超集单类）+ Manager 单类单缓存
- [x] `llm_service.py`：迁移到 reserve/settle 统一闭环（R1/R2/R3/R8 防护：create 失败 cancel、create 成功后 settle、迭代硬取消 finally 兜底）
- [x] `test_rate_limiter.py`：新增 11 个测试（refund/Reservation/reserve），24/24 通过
- [x] `test_stream_rectify.py`：stub 适配 reserve，15/15 通过
- [x] 文档：rate_limiter.md 组件详解/调用流程/工业级对比更新

## 进度

- [x] **问题 1（严重）** 配置 0 除零崩溃 —— `TokenBucket.acquire` 对 `refill_rate <= 0` 防御，直接放行
- [x] **问题 2（中）** 持锁 sleep —— `acquire` 重构为「锁内计算 → 锁外 sleep → 循环重检」（连带解决问题 6）
- [x] **问题 3（中）** TPM 只算 prompt —— `_count_prompt_tokens` 加 `max_tokens` 输出余量
- [x] **问题 4（低）** `acquire` 返回值表述不准 —— 修正 docstring
- [x] **问题 5（低）** `async with` 用法误导 —— 移除 `__aenter__/__aexit__` 死代码 + 更新 docstring
- [x] **问题 6（低）** `_tokens` 轻微为负 —— 已由问题 2 重构连带解决（只在 `_tokens >= tokens` 时扣减）

## 评审（2026-08-02 全部完成）

6 个审核问题全部修复，测试 13/13 通过（rate_limiter）+ 37/37（stream_rectify + retry）无回归。

| 问题 | 修复方式 | 验证 |
| --- | --- | --- |
| 1 | `TokenBucket.acquire` 对 `refill_rate <= 0` 直接放行 | `test_bucket_zero_refill_disabled` |
| 2 | 锁外 sleep 循环重检 | `test_bucket_wait_does_not_block_others` / `test_bucket_cancel_does_not_corrupt_state` |
| 3 | `_count_prompt_tokens` 加 `max_tokens` 输出余量 | 37 测试无回归 |
| 4 | docstring 明确返回值语义 | 纯文档 |
| 5 | 移除 `__aenter__/__aexit__` 死代码 | `py_compile` + 全测试 |
| 6 | 由问题 2 重构连带解决 | `test_bucket_cancel_does_not_corrupt_state` 覆盖 |

## 关联文件

| 文件 | 改动 |
| --- | --- |
| `app/services/llm/rate_limiter.py` | TokenBucket.acquire / RateLimiter.acquire / docstring |
| `app/services/llm/llm_service.py` | `_count_prompt_tokens` 输出余量（问题 3） |
| `tests/unit/test_rate_limiter.py` | 新增各问题回归测试 |
| `docs/llm/rate_limiter.md` | 附录问题标记修复 + 正文已知边界同步 |

## 评审

（待各问题修复后逐条补充）
