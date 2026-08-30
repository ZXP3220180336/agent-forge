# REASON-005 UNKNOWN error 拼接完整异常文本：内部细节泄漏到产品侧

> **状态**：✅ 已修复（2026-08-30）
> **优先级**：P1（产品可见错误泄漏内部路径 / 参数 / 敏感值；UNKNOWN 是兜底路径，泄漏面覆盖所有未捕获异常）
> **来源**：2026-08-30 代码审查发现（Agent 运行异常处理全面性评审，问题 5）
> **涉及模块**：`app/domain/reasoning/react.py`（`_finalize_unknown`）
> **关联文档**：[react.md](../../../docs/domain_doc/reasoning_doc/react.md)

---

## 问题描述

### 现象

`_finalize_unknown` 的 error 文本为 `f"Agent 运行异常: {exc!s}"[:200]`——直接拼接异常 `str` 表示：

1. **内部细节泄漏**：异常 message 可能含内部路径 / 参数 / 敏感值 / 堆栈提示（如 `连接失败: http://10.0.0.1/api key=sk-xxx`），截断 200 字符不解决泄漏（敏感值可出现在前 200 字符内）；
2. **泄漏途径**：`outcome.error` → `ReActAgent._map_outcome` → `AgentResult.error` → SSE 事件 / 最终回答 / 根因报告（产品主链路）——**产品可见**。

### 影响

- 良率工程师（产品用户）看到内部异常细节（端点 / 密钥 / 内部结构），信息暴露面不合规；
- UNKNOWN 是主循环兜底（捕获所有未捕获异常），泄漏面覆盖全部异常路径——单个异常即可泄漏。

### 根因

`_finalize_unknown` 未区分「产品可见文本」与「运维诊断」——直接把异常完整文本放进产品可见的 error 字段。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| OpenAI / 云厂商错误信封 | 对外错误保留类型 / code（分类），message 通用化；内部细节关联错误 ID 进日志，产品侧不裸漏异常文本 |
| 本项目 LLM schema 校验脱敏（issue 2026-08-16） | 「给模型的错误」与「落盘的日志」分离——模型需要具体错误，日志只需诊断摘要；两套文本 |
| 本项目工具审计掩码（issue 2026-08-19） | 敏感键（key/token/secret/password）序列化前掩码 |

**核心**：产品可见错误 = 分类信息（异常类型名），完整异常 = 运维日志——两套文本分离，泄漏与可诊断兼得。

---

## 修复方案（含决策取舍）

**决策**：

1. **error 脱敏**：`f"Agent 运行异常: {type(exc).__name__}"`——只保留异常类型名（分类，如 `RuntimeError` / `TimeoutError`），**不拼接异常 message**（路径 / 参数 / 敏感值 / 堆栈提示全在 message 里，全部排除）。
2. **完整异常进日志**：`_logger.error("Agent 运行异常: %s", exc, exc_info=exc)`——完整 message + traceback 进日志（运维可诊断），产品侧不可见。
3. **logger 用标准库**：react.py 模块定位「只依赖 ports + shared + 标准库」——`import logging` + `logging.getLogger("app.domain.reasoning.react")`（命名空间对齐 `app.*`，可被 `setup_logging` 捕获），不用 platform 的 `get_logger`（对齐问题 4 shared 层做法）。
4. **`exc_info=exc` 显式传异常对象**：不依赖「在 except 块内」的隐式异常上下文（`_finalize_unknown` 函数体不在 try/except 中，静态分析警告 `exc_info=True` outside handler）；显式传对象更健壮。

**取舍理由**：

1. 保留异常类型名而非完全通用化（如「内部错误」）：类型名是错误分类（诊断价值），不泄漏敏感值（敏感值在 message），良率工程师能据此决策（重试 / 报告）；完全通用化会丢失分类信息且与日志难对应；
2. 不做 message 敏感正则过滤：message 形式不可枚举（路径 / 参数 / 查询 / 工具输出都可能嵌入），正则过滤容易漏且维护成本高——「只留类型名」是简单且彻底的隔离；
3. 不引入错误 ID：当前单用户本地 / 引擎主链路场景，用户可接触日志，ID 关联价值低（产品导向，不为假设买单）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/domain/reasoning/react.py` | `_finalize_unknown` error 改为 `f"Agent 运行异常: {type(exc).__name__}"`（去 `exc!s`）；新增 `_logger`（标准库 logging）+ `logger.error(... exc_info=exc)` 记录完整异常 | `tests/unit/test_react_strategy.py` 新增 1 例：脱敏（message 含敏感端点/key 不泄漏 + error 只含类型名）+ caplog 断言完整异常进日志 |

---

## 验证

- **测试驱动**：`test_react_unknown_error_redacts_exception_message`——异常 `RuntimeError("连接失败: 内部端点 http://10.0.0.1/api key=sk-secret")` → `outcome.error == "Agent 运行异常: RuntimeError"`（`sk-secret` / `10.0.0.1` 不出现）；caplog 断言日志含完整异常文本（运维可诊断）。
- 既有 UNKNOWN 断言（「Agent 运行异常」前缀，含 test_agent.py RAISE 场景）全部保持通过；
- `tests/unit/test_react_strategy.py` 69 passed；`uv run pytest` 全量 692 passed；`verify_alignment` 通过。

---

## 教训沉淀

- **产品可见文本与运维诊断必须分离**：error / SSE / 根因报告是产品侧（面向良率工程师），异常堆栈 / message 是运维侧——两套文本，敏感数据不泄露、诊断能力不损；
- **截断 ≠ 脱敏**：`[:200]` 只限长度，不挡敏感值（可出现在前 200 字符内）；脱敏应基于「语义剔除」（只留类型名）而非「长度裁剪」；
- **兜底路径的泄漏面最广**：UNKNOWN 捕获所有未捕获异常，其 error 必须最保守（只留分类）——兜底路径泄漏 = 全路径泄漏。
