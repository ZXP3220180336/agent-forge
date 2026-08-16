# 熔断器生命周期：每次调用新建，窗口无法跨请求积累（熔断失效）

> **状态**：✅ 已修复（2026-08-07）
> **优先级**：P0（严重，create 阶段熔断失效）
> **来源**：2026-08-07 工业级改造 · 2026-08-16 从 retry.md 提取归档
> **涉及模块**：`app/integration/llm/retry.py`（`RetryHandlerManager`）· `app/integration/llm/llm_service.py`
> **关联文档**：[retry.md](../../../docs/integration_doc/llm_doc/retry.md)

---

## 问题描述

### 现象

`_build_retry_handler()` 在每次 `async_generate`/`generate` 新建 `RetryHandler` + `CircuitBreaker` → 熔断窗口每次请求清空，`request_volume_threshold=20` 永远达不到 → **create 阶段熔断实际失效**（与「熔断器需要跨请求共享状态」设计意图矛盾）。

### 影响

熔断保护形同虚设——下游持续故障时 create 阶段不熔断，流量持续打到故障服务。

### 根因

熔断器生命周期绑定单次请求，未跨请求共享——滑动窗口统计需要跨请求积累。

---

## 工业级参照

| 结论 | 做法 |
| --- | --- |
| 熔断状态跨请求共享 | 熔断窗口（错误率/低流量保护）依赖跨请求统计，实例必须跨请求复用 |
| 按模型隔离 | 不同模型/端点独立熔断（reasoning 故障不熔断 fast）——与 ClientManager/限流器 Manager 架构一致 |

---

## 修复方案（含决策取舍）

**决策**：新增 `RetryHandlerManager`——按 model_key 缓存共享 RetryHandler（内含跨请求共享 CircuitBreaker），main/reasoning/fast 独立熔断；`LLMService` 改用 `RetryHandlerManager.get(model_key)`。

**修复要点**：

- **共享实例**：同一 model_key 复用同一个 `RetryHandler`——熔断窗口跨请求记账，每次 new 等于没熔断；
- **按 model_key 隔离**：reasoning 故障不熔断 fast；
- **懒加载 + 配置注入（2026-08-10）**：首次 `get()` 创建，按 `register_config()` 注入的配置构建（子模块零 settings 依赖）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/llm/retry.py` | 新增 `RetryHandlerManager`（按 key 缓存共享 + register_config 注入） | `test_retry_handler_manager.py` 同 key 共享/按 key 隔离/reset 用例 |
| `app/integration/llm/llm_service.py` | 改用 `RetryHandlerManager.get(model_key)` | 全量回归 |

---

## 验证

- 同 key 共享实例（熔断窗口跨请求积累）；不同 key 独立；create 阶段熔断生效
- 全量测试通过（2026-08-07 修复时验证）

---

## 教训沉淀

- **熔断器必须跨请求共享**：每次 new 会清空窗口、等于熔断永不触发——这是 create 阶段熔断失效的隐性缺陷根源。
- **按 model_key 隔离**：不同模型/端点独立熔断，与 ClientManager/限流器 Manager 架构一致。
