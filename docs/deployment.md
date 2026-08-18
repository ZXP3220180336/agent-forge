# 部署说明文档

> **更新日期**：2026-08-03
> **文档定位**：本地开发运行方式、环境要求、依赖基础设施与配置。

---

## 📋 目录

- [环境要求](#环境要求)
- [启动方式](#启动方式)
- [依赖基础设施](#依赖基础设施)
- [环境变量配置](#环境变量配置)
- [常见问题](#常见问题)

---

## 环境要求

| 项 | 要求 |
| --- | --- |
| Python | ≥ 3.14（`requires-python = ">=3.14"`） |
| 包管理 | uv |
| 平台 | Windows 11（开发）/ 跨平台 |
| 外部服务 | PostgreSQL（可选，当前 DB 恒降级）、Redis（可选，缺失时服务降级） |

### 依赖安装

```bash
uv sync           # 安装生产依赖 + dev 依赖（pytest / pytest-asyncio / debugpy）
```

---

## 启动方式

### 方式 1：直接运行 main.py（内置 uvicorn reload）

```bash
uv run python app/main.py
```

等价于 `uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`。

### 方式 2：uvicorn 命令

```bash
uv run uvicorn app.main:app --reload
```

### 启动日志（预期）

```
正在初始化应用服务...
  [OK] Redis 连接成功          ← Redis 可用时
  [WARN] Redis 不可用（服务降级）  ← Redis 缺失时
  [OK] 数据库引擎创建成功      ← PG 可用时（当前缺 asyncpg 驱动则降级）
  [OK] 已注册工具: ['CodeExecTool', 'ReadFileTool', 'WriteFileTool', 'SearchTool', 'WebBrowseTool', 'QueryBatchYieldTool', 'QueryEquipmentAlertsTool', 'QueryFdcParamsTool', 'QueryDefectMapTool', 'SearchHistoricalRcaTool']
应用初始化完成
```

### 健康检查

```bash
curl http://localhost:8000/api/health
# → {"status":"ok","version":"1.0.0"}
```

---

## 依赖基础设施

### PostgreSQL（可选）

- 用途：会话与消息持久化（SessionModel / MessageModel）
- **当前状态**：`asyncpg` 驱动未安装 → DB 恒降级，服务仍可启动但会话/消息无法持久化
- 若要启用：在 `pyproject.toml` 依赖加 `asyncpg`，并配置真实 `DATABASE_URL`

### Redis（可选）

- 用途：会话热缓存、会话列表缓存、会话统计缓存
- **当前状态**：Redis 缺失时 `container` 降级（`redis=None`），但 `SessionManager.create_session()` 会因 `self.redis.set()` 抛错——**HTTP 闭环依赖 Redis**

---

## 环境变量配置

所有配置经 `app/config/settings.py`（Pydantic Settings）从 `.env` 加载。

### 最小配置（核心）

```bash
# LLM
LLM_API_KEY="sk-xxx"
LLM_BASE_URL="https://api.deepseek.com"
LLM_MODEL_ID="deepseek-v4-flash"
LLM_REASONING_MODEL_ID="deepseek-pro"

# 数据库（当前占位符，DB 恒降级）
DATABASE_URL="postgresql+asyncpg://user:pass@localhost/db"

# Redis
REDIS_URL="redis://localhost:6379/0"
```

### 配置优先级

```
系统环境变量 > .env 文件 > 代码默认值
```

---

## 常见问题

### Q1: `python app/main.py` 直接运行报错？

**A:** `sys.path` 会变成 `app/` 目录。当前 `main.py` 已用 `sys.path.insert(0, 项目根目录)` 修复，但仍推荐 `uv run python -m app.main` 或 `uv run python app/main.py`。

### Q2: 启动时控制台报 UnicodeEncodeError？

**A:** Windows GBK 控制台无法编码 emoji/特殊符号。`main.py` 已统一 `stdout/stderr` 切换 UTF-8，并防御性处理 None/无 reconfigure/OSError 三种环境。

### Q3: 启动后 Redis 不可用，会话接口报错？

**A:** `SessionManager` 直接调用 `self.redis.set()`，Redis 缺失时 `self.redis` 为 None。需确保 Redis 可用，或后续实现降级逻辑。

---

## 相关文档

- [架构设计](architecture.md)
- [config 模块](config_doc/config.md)
- [api 模块](api_doc/api.md)
