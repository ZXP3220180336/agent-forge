# 中间件模块对外接口文档

> **对应代码**：`app/api/middleware/`
> **更新日期**：2026-08-29
> **文档定位**：中间件模块对外接口文档——统一错误信封契约 + 内部组件导航；服务对象为 API 层调用方（路由 / 客户端）
> **实现状态**：error_handler ✅ 已实现 · auth / rate_limit ⬜ 预留
> **配套**：路由端点契约见 [routes.md](../routes_doc/routes.md)；层总览见 [README.md](../README.md)

---

## 📋 目录

- [中间件模块对外接口文档](#中间件模块对外接口文档)
  - [📋 目录](#-目录)
  - [模块概述](#模块概述)
    - [核心定位](#核心定位)
    - [模块结构](#模块结构)
    - [设计原则](#设计原则)
    - [当前组装](#当前组装)
  - [对外接口](#对外接口)
    - [统一错误信封](#统一错误信封)
    - [auth — 认证与鉴权](#auth--认证与鉴权)
    - [rate\_limit — API 限流](#rate_limit--api-限流)
  - [内部实现组织](#内部实现组织)
  - [相关文档](#相关文档)

---

## 模块概述

### 核心定位

中间件模块是 FastAPI 的**请求前置处理层**，在请求进入路由之前统一做三类横切关注点处理：

- **认证与鉴权**（`auth.py`）：识别请求身份，拦截未授权访问
- **限流**（`rate_limit.py`）：按用户 / IP / 全局维度约束请求频率，防滥用
- **错误处理**（`error_handler.py`）：捕获并归一化所有异常，保证错误响应格式统一

与依赖注入（`app/api/deps.py` 的 `get_current_user`）相比，中间件的核心差异是**全局性**：中间件对所有请求生效，不依赖路由函数显式声明依赖；而依赖注入是**按端点**精确控制。二者是互补关系，而非替代关系（详见 [auth 设计要点](#auth--认证与鉴权)）。

### 模块结构

```text
app/api/middleware/
├── auth.py           ← 预留：JWT 认证与请求鉴权
├── rate_limit.py     ← 预留：API 限流
└── error_handler.py  ← ✅ 已实现：统一异常处理（AppError → HTTP 状态 + 信封）
```

### 设计原则

1. **横切关注点下沉**：认证 / 限流 / 异常处理不应散落在各个路由函数中，统一收敛到中间件模块
2. **单一职责**：每个中间件只做一件事，通过 `app.add_middleware()` / `@app.middleware("http")` 在 `main.py` 组装
3. **复用既有能力**：限流可复用 LLM 层 `ReservationLimiter`（reserve/settle）思路；异常格式遵循统一信封（见「对外接口」）
4. **最小侵入**：中间件只做前置拦截与归一化，不承载业务逻辑，业务逻辑仍留在路由与服务层

### 当前组装

`app/main.py` 当前组装了 CORS 中间件、SPA 回退中间件与统一异常处理：

- **CORSMiddleware**：`allow_origins=["*"]`、`allow_credentials=True`、`allow_methods=["*"]`、`allow_headers=["*"]`，用于前端跨域访问
- **SPA 回退**：`@app.middleware("http")` 定义的 `spa_fallback`，对非 `/api/` 前缀的 404 请求回退返回 `static/index.html`，解决前端路由刷新 404 问题
- **统一异常处理**：`register_error_handlers(app)` 注册 AppError 处理器（见「对外接口 → 统一错误信封」）

新增 `auth` / `rate_limit` 时，需要在 `main.py` 中通过 `app.add_middleware()` 或 `@app.middleware("http")` 注册，并注意中间件**注册顺序**（先注册的执行在外层）。

---

## 对外接口

### 统一错误信封

**文件**：`app/api/middleware/error_handler.py`（✅ 已实现，54 行）

#### 信封格式

API 层所有 `AppError` 异常经 error_handler 翻译为统一信封（`code` 为业务错误码，HTTP 状态反映通信语义，二者解耦）：

```json
{ "code": "业务错误码", "message": "可读错误描述", "details": null }
```

#### 状态码映射

`AppErrorCode` → HTTP 状态映射（`_CODE_TO_STATUS`，来自 error_handler.py）：

| AppErrorCode | HTTP | 场景 |
| --- | --- | --- |
| `VALIDATION` | 400 | 参数校验失败 |
| `UNAUTHORIZED` | 401 | 未认证（缺凭证） |
| `FORBIDDEN` | 403 | 已认证但无权访问 |
| `NOT_FOUND` | 404 | 资源不存在 |
| `LLM_TOOL_CALL` | 400 | LLM 工具调用信号 |
| `SSRF_BLOCKED` | 400 | SSRF 拦截 |
| `LLM_REFUSAL` | 502 | LLM 拒答 |
| `LLM_TRUNCATED` | 502 | LLM 截断 |
| `CIRCUIT_OPEN` | 503 | 熔断开启 |
| `INTERNAL`（默认） | 500 | 未分类错误 |

#### 职责

把统一异常树（`AppError`）在对外边界翻译为 HTTP 状态 + 统一信封。API 层不直接抛 `HTTPException`，全走统一异常树（异常体系见 [error_handling.md](../../shared_doc/error_handling.md)）。

#### 实现组成

| 组件 | 说明 |
| --- | --- |
| `_CODE_TO_STATUS` | `AppErrorCode` → HTTP 状态映射表（边界翻译表，见上） |
| `_status_for(code)` | 查映射表，未映射兜底 500 |
| `app_error_handler` | `AppError` → JSON 信封 `{code, message, details}` |
| `register_error_handlers(app)` | 在 `main.py` 调用，注册 `AppError` 处理器 |

#### 设计要点

- **注册方式**：`main.py` 启动阶段调用 `register_error_handlers(app)`（`app.add_exception_handler(AppError, app_error_handler)`）
- **断言收紧**：handler 第二参按 `Exception` 标注（Starlette 签名要求），注册时已限定 `AppError`，运行时断言验证
- **不吞异常**：非 `AppError` 异常不在本处理器覆盖范围，交由 FastAPI 默认兜底

### auth — 认证与鉴权

**文件**：`app/api/middleware/auth.py`（预留）

#### 预期功能（auth）

1. **JWT 认证**：解析 `Authorization: Bearer <token>`，校验 JWT 签名、`exp` 有效期、`iss` / `aud` 等声明
2. **请求鉴权**：按路由路径或角色对请求做权限放行 / 拒绝
3. **用户上下文注入**：认证通过后将解析出的用户信息（`user_id` 等）注入请求上下文，供后续路由与依赖使用

#### 设计要点（auth）

- **与 `get_current_user` 的关系**：中间件做**全局预检**（未认证请求在进入路由前即被 401 拒绝），`get_current_user` 保留做**端点级精细化控制**（如部分路由允许匿名访问时）。当前认证由 `deps.get_current_user()` 模拟实现（见 [routes.md「认证方式」](../routes_doc/routes.md)）；迁移 JWT 解析逻辑到中间件后，`get_current_user` 可退化为读取中间件注入的请求上下文，避免重复解析
- **错误响应**：认证失败统一返回 `401`（无 Token / 签名错误 / 过期），并遵循统一信封格式（见上）
- **白名单**：对无需认证的路径（健康检查 `/api/health`、登录接口等）做放行配置，避免误拦截
- **安全细节**：使用标准 JWT 库（如 `python-jose` / `PyJWT`），私钥走配置与环境变量管理，不硬编码

### rate_limit — API 限流

**文件**：`app/api/middleware/rate_limit.py`（预留）

#### 预期功能（rate_limit）

1. **多维度限流**：按 `user_id` / IP / 全局三个维度分别限流，防止单用户滥用与全局限速
2. **灵活配额**：不同端点可配置不同速率（如聊天接口更严格、健康检查不限）
3. **限流响应**：超限返回 `429 Too Many Requests`，附 `Retry-After` 响应头

#### 设计要点（rate_limit）

- **复用 LLM 层限流思路**：LLM 层限流经 `ReservationLimiter`（reserve/settle 形态，先预留配额再结算）实现，RPM / TPM 双 Token Bucket 算法本体保留在 `reservation_limiter.py`（见 [limiter.md](../../integration_doc/llm_doc/limiter.md)），API 中间件限流可直接借鉴：
  - **Token Bucket 算法**：允许突发 + 长期平滑，适合 Agent 场景的短时并发尖峰
  - **reserve / settle 形态**：与「请求进入 → 处理 → 放行」的中间件生命周期天然契合
  - **`Retry-After` 支持**：与 LLM 层处理上游 429 的逻辑对称，中间件在超限时对客户端返回 `Retry-After`
- **与 LLM 层限流的区别**：LLM 层限流保护**上游模型 API**（防打爆服务商），中间件限流保护**自身服务**（防滥用 / 防 DDoS）。两层各自独立、互不替代
- **实现形态**：可选择复用 LLM 层的限流组件，或针对中间件场景做轻量封装（按维度建 Token Bucket 实例）
- **内存与分布式**：单机场景内存存储即可；多实例部署时需考虑集中式存储（如 Redis），与 `SessionManager` 的 Redis 连接池对齐

---

## 内部实现组织

| 组件 | 文件 | 职责 | 状态 |
| --- | --- | --- | --- |
| 统一异常处理 | error_handler.py | `AppError` → HTTP 状态 + `{code, message, details}` 信封（main.py 已注册） | ✅ |
| 认证鉴权 | auth.py | JWT 认证、请求鉴权（当前由 deps.get_current_user 模拟） | ⬜ 预留 |
| API 限流 | rate_limit.py | 按用户 / IP / 全局维度限流（可复用 LLM 层 reserve/settle 思路） | ⬜ 预留 |

---

## 相关文档

- [层总览 README](../README.md)（api 层结构树 / 实现状态）
- [路由模块](../routes_doc/routes.md)（端点契约 / 认证方式 / 业务状态码）
- [异常体系](../../shared_doc/error_handling.md)（统一异常树 `AppError` / `AppErrorCode`，error_handler 的上游）
- [LLM 限流](../../integration_doc/llm_doc/limiter.md)（reserve/settle + TokenBucket，rate_limit 参考）
- [架构设计](../../architecture.md)（演进路径：Phase D 中间件）
