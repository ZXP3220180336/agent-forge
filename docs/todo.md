# 2026-08-20 工具模块代码审查修复（TOOLS-049）

> 来源：工具模块整体代码审查（四维度：正确性 / 安全 / 性能 / 规范）。完整生命周期见 [TOOLS-049 问题记录](../issues/integration/tools/2026-08-20-code-review-fixes.md)。

- [x] **重要项 5**：executor 重试全败归因 / SSRF CGNAT 盲区 / 审计脱敏 error+content / 外部工具冷启动扫描 / RCA 证据链时间锚点（各带回归测试）
- [x] **次要项 15**：executor（execution_time/注释）、loader（_drop_modules 前缀过滤）、assembler（单工具失败隔离）、security（敏感键正则/DNS 注释）、result_processor（docstring）、validator（完整路径）、tool_gateway（__str__ 兜底）、prompts（截断提示）、RCA（FDC 判定/空结果归因/冗余 int）、hooks（async 注释）
- [x] **取舍项保持现状**：validator schema 缓存 / 嵌套 additionalProperties / getaddrinfo 超时 / loader 重载竞态 / scan_once 线程化 / 裸 IP 拒绝（ADR 保守策略）
- [x] **文档同步**：executor / security / rca / tool_service / external + ALIGNMENT verify
- [x] **全量回归**：uv run pytest 通过

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
|---|---|---|
| 1 | `TokenBucket.acquire` 对 `refill_rate <= 0` 直接放行 | `test_bucket_zero_refill_disabled` |
| 2 | 锁外 sleep 循环重检 | `test_bucket_wait_does_not_block_others` / `test_bucket_cancel_does_not_corrupt_state` |
| 3 | `_count_prompt_tokens` 加 `max_tokens` 输出余量 | 37 测试无回归 |
| 4 | docstring 明确返回值语义 | 纯文档 |
| 5 | 移除 `__aenter__/__aexit__` 死代码 | `py_compile` + 全测试 |
| 6 | 由问题 2 重构连带解决 | `test_bucket_cancel_does_not_corrupt_state` 覆盖 |

## 关联文件

| 文件 | 改动 |
|---|---|
| `app/services/llm/rate_limiter.py` | TokenBucket.acquire / RateLimiter.acquire / docstring |
| `app/services/llm/llm_service.py` | `_count_prompt_tokens` 输出余量（问题 3） |
| `tests/unit/test_rate_limiter.py` | 新增各问题回归测试 |
| `docs/llm/rate_limiter.md` | 附录问题标记修复 + 正文已知边界同步 |

## 评审

（待各问题修复后逐条补充）

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
