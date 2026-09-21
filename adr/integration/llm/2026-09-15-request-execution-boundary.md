# LLM-ADR-018：请求执行边界与 Facade 编排分离

日期：2026-09-15。决定状态：已接受（用户授权执行 R2）。实现状态：已实现、已验证。
范围：`llm_service.py`、`request_execution.py` 及对应测试、说明。关联：[R2 归档](../../../docs/history/completed-work.md#refactoring-plan)。

## 背景与备选

`LLMService` 同时承担公开网关、请求参数构造、预算与限流准入、provider create、响应解析、
结算和日志。真实请求的生命周期需要在主请求、fallback、重试、流式整流和续接之间保持一致，
但这些规则与 Facade 的通道编排混在同一文件，修改任何一侧都需要理解整条链路。

继续保留单文件不会形成清晰的所有权边界；新增有状态 Executor 类没有独立生命周期或可替换实现，
会重复现有 Manager 的职责。因此采用包内函数模块承载请求执行，并保留 `LLMService` 作为唯一公开入口。

工业级参照：Python 官方文档说明 `dataclass(frozen=True)` 可用于表达只读数据载体；
本模块据此保留冻结的调用上下文，而不增加行为类。OpenAI Python SDK 的异步接口仍以
`AsyncOpenAI` client 发起请求，拆分不改变 SDK 调用形状。

## 决定

- `request_execution.py` 负责请求 kwargs、事件基础字段、token 预估、请求计划以及每笔真实
  create 的预算校验、配额预留、执行控制和调用阶段结算。
- 模块只依赖 client、request budget、reservation limiter、retry、execution control、errors
  和 token counter；运行配置由 `LLMService` 显式传值，不反向导入 Facade。
- `LLMService.async_generate` 与 `generate` 直接调用 `request_execution.build_request_plan`，
  Facade 继续负责公开异常翻译、非流式响应解析、
  最终 usage 结算、流式整流接线、事件提交和成本/计数代理。
- 主请求与续接沿用 `model_key`，fallback 沿用独立 `"fallback"` 键且只替换模型 ID；各通道
  共享本次调用的 `active` 结算容器。
- create 前终止撤回已取得的 Reservation；create 调度后的错误或终止使用 `settle(None)`；
  成功或迟回值交给通道 Owner 按实际 usage 或未知 usage 结算。重试、整流和续接次数不变。
- 新模块为包内实现，不从 `app.integration.llm` 导出，也不为已迁移的私有符号保留兼容别名。

## 后果与边界

Facade 可以按流式、非流式和结构化通道阅读，请求执行的副作用边界集中在一个内部模块。
代价是新增一个包内依赖，测试替身需要在真实定义位置替换 Manager。

本决定不迁移流读取、chunk 累积或流关闭，它们仍归 `StreamingRectifier`；不改变公开参数、
配置默认值、预算算法、fallback provider 限制或熔断统计。流消费拆分属于后续 R3。

若未来出现第二种独立请求执行实现，或执行计划需要独立持久状态，再评估接口或对象抽象；
当前不建设通用 Executor 框架。

## 实现与验证

- 2026-09-15 文档勘误：原“保留 `_plan_request` 薄委托”表述由上述直接调用关系替代；
  该私有方法已在 R2 提取中删除，本次仅纠正记录，不改变公开契约或代码。
- `request_execution.py` 已接入 `LLMService` 的流式与非流式真实调用链；测试 patch 路径随定义迁移。
- 定向回归覆盖 Facade、预算与配额、整流和流消费边界，共 91 项通过。
- 全量 1204 项通过；文档对齐、差异检查及独立审查通过，结果回填
  [R2 归档](../../../docs/history/completed-work.md#refactoring-plan)。
- 当前代码、文档和测试映射见 [ALIGNMENT](../../../docs/ALIGNMENT.md)。

## 关联资料

- [Python `dataclasses` 官方文档](https://docs.python.org/3/library/dataclasses.html)
- [OpenAI Python SDK](https://github.com/openai/openai-python)
- [LLMService 编排说明](../../../docs/integration_doc/llm_doc/llm_service.md)
