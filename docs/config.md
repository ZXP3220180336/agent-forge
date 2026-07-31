# 配置管理模块说明文档

## 📋 目录

- [模块概述](#模块概述)
- [架构设计](#架构设计)
- [配置项详解](#配置项详解)
- [使用示例](#使用示例)
- [环境变量配置](#环境变量配置)
- [最佳实践](#最佳实践)
- [常见问题](#常见问题)

---

## 模块概述

### 核心功能

配置管理模块负责统一管理整个 Agent 系统的所有配置项，提供：

- **集中式配置管理**：所有配置项集中在一个模块
- **类型安全**：使用 Pydantic 进行类型检查和验证
- **环境隔离**：支持 `.env` 文件，方便不同环境配置
- **单例模式**：全局只有一个配置实例，避免重复加载
- **配置聚合**：将相关配置项聚合为字典，方便传递

### 模块结构

```
app/config/
├── __init__.py          # 模块导出
└── settings.py          # 配置类定义

.env                     # 环境变量配置文件
```

---

## 架构设计

### 设计理念

1. **单一配置源**
   - 所有配置项从环境变量加载
   - 支持 `.env` 文件
   - 避免配置散落在代码各处

2. **类型安全**
   - 使用 Pydantic 进行类型验证
   - 自动类型转换
   - 配置错误在启动时暴露

3. **默认值优先级**

   ```
   代码默认值 < .env 文件 < 系统环境变量
   ```

4. **单例模式**
   - 使用 `lru_cache` 装饰器
   - 全局只创建一次配置实例
   - 避免重复读取环境变量

### 配置加载流程

```
应用启动
    ↓
get_settings()
    ↓
Settings() 实例化
    ↓
加载环境变量（.env + 系统环境）
    ↓
Pydantic 类型验证
    ↓
返回配置实例（缓存）
```

---

## 配置项详解

### 1. 应用配置

| 配置项        | 类型 | 默认值            | 说明                     |
| ------------- | ---- | ----------------- | ------------------------ |
| `APP_NAME`    | str  | "AI Agent System" | 应用名称，用于日志和监控 |
| `APP_VERSION` | str  | "1.0.0"           | 应用版本                 |
| `DEBUG`       | bool | false             | 调试模式开关             |

**使用场景：**

- 日志标识：`logger.info(f"{settings.app_name} v{settings.app_version} 启动")`
- 条件分支：`if settings.debug: enable_detailed_errors()`

---

### 2. API 配置

| 配置项         | 类型      | 默认值 | 说明          |
| -------------- | --------- | ------ | ------------- |
| `API_PREFIX`   | str       | "/api" | API 路由前缀  |
| `CORS_ORIGINS` | list[str] | ["*"]  | CORS 允许的源 |

**使用场景：**

- 路由配置：`app.include_router(router, prefix=settings.api_prefix)`
- 中间件配置：`CORSMiddleware(allow_origins=settings.cors_origins)`

---

### 3. LLM 配置

#### 3.1 主模型配置

| 配置项            | 类型  | 默认值                      | 说明                 |
| ----------------- | ----- | --------------------------- | -------------------- |
| `LLM_API_KEY`     | str   | ""                          | LLM API 密钥（必填） |
| `LLM_BASE_URL`    | str   | "https://api.openai.com/v1" | API 端点             |
| `LLM_MODEL_ID`    | str   | "gpt-4"                     | 模型标识符           |
| `LLM_TEMPERATURE` | float | 0.2                         | 生成温度（0-2）      |
| `LLM_MAX_TOKENS`  | int   | 4096                        | 最大输出 Token 数    |
| `LLM_TIMEOUT`     | int   | 60                          | 请求超时时间（秒）   |

**使用场景：**

- 主要对话和决策
- 复杂任务处理

**温度建议：**

- `0.0-0.3`：确定性任务（代码生成、数据提取）
- `0.4-0.7`：平衡任务（对话、分析）
- `0.8-1.0`：创意任务（写作、头脑风暴）

#### 3.2 推理模型配置

| 配置项                      | 类型  | 默认值 | 说明                             |
| --------------------------- | ----- | ------ | -------------------------------- |
| `LLM_REASONING_MODEL_ID`    | str   | ""     | 推理模型标识符（空则使用主模型） |
| `LLM_REASONING_TEMPERATURE` | float | 0.7    | 推理温度                         |
| `LLM_REASONING_MAX_TOKENS`  | int   | 8192   | 推理最大 Token 数                |

**使用场景：**

- 数学推理
- 代码生成
- 复杂逻辑分析

**推荐模型：**

- DeepSeek-R1（推理专用）
- OpenAI o1
- Claude 3.5 Sonnet（thinking）

#### 3.3 快速模型配置

| 配置项                 | 类型  | 默认值 | 说明                             |
| ---------------------- | ----- | ------ | -------------------------------- |
| `LLM_FAST_MODEL_ID`    | str   | ""     | 快速模型标识符（空则使用主模型） |
| `LLM_FAST_TEMPERATURE` | float | 0.0    | 快速模型温度                     |
| `LLM_FAST_MAX_TOKENS`  | int   | 2048   | 快速模型最大 Token 数            |

**使用场景：**

- 文本分类
- 信息提取
- 简单问答
- 成本优化

**推荐模型：**

- GPT-3.5-turbo
- DeepSeek-V3
- Claude 3 Haiku

#### 3.4 嵌入模型配置

| 配置项                     | 类型 | 默认值                   | 说明           |
| -------------------------- | ---- | ------------------------ | -------------- |
| `LLM_EMBEDDING_MODEL_ID`   | str  | "text-embedding-3-small" | 嵌入模型标识符 |
| `LLM_EMBEDDING_DIMENSIONS` | int  | 1536                     | 向量维度       |

**使用场景：**

- 向量化存储
- 相似度搜索
- 记忆检索

---

### 4. 上下文配置

| 配置项               | 类型 | 默认值 | 说明                |
| -------------------- | ---- | ------ | ------------------- |
| `MAX_CONTEXT_TOKENS` | int  | 128000 | 最大上下文 Token 数 |
| `MAX_OUTPUT_TOKENS`  | int  | 4096   | 最大输出 Token 数   |
| `MAX_HISTORY_ROUNDS` | int  | 20     | 保留历史对话轮数    |

**注意事项：**

- 确保上下文 + 输出不超过模型限制
- 历史轮数过多会增加 Token 消耗

---

### 5. Agent 配置

#### 5.1 基础配置

| 配置项                 | 类型 | 默认值 | 说明                       |
| ---------------------- | ---- | ------ | -------------------------- |
| `AGENT_MAX_ITERATIONS` | int  | 10     | 最大迭代次数（防止死循环） |
| `AGENT_TIMEOUT`        | int  | 300    | 默认任务超时时间（秒）     |
| `AGENT_STREAMING`      | bool | true   | 是否启用流式输出           |

#### 5.2 任务优先级配置

| 配置项                        | 类型      | 默认值                              | 说明                       |
| ----------------------------- | --------- | ----------------------------------- | -------------------------- |
| `AGENT_PRIORITY_LEVELS`       | list[str] | ["low", "normal", "high", "urgent"] | 优先级等级                 |
| `AGENT_DEFAULT_PRIORITY`      | str       | "normal"                            | 默认优先级                 |
| `AGENT_HIGH_PRIORITY_TIMEOUT` | int       | 600                                 | 高优先级任务超时时间（秒） |
| `AGENT_LOW_PRIORITY_TIMEOUT`  | int       | 180                                 | 低优先级任务超时时间（秒） |
| `AGENT_PRIORITY_QUEUE_SIZE`   | int       | 100                                 | 优先级队列大小             |

**优先级使用场景：**

- **urgent**：实时对话、紧急任务
- **high**：重要但非紧急的分析任务
- **normal**：常规任务（默认）
- **low**：后台批处理任务

#### 5.3 并发控制配置

| 配置项                       | 类型 | 默认值 | 说明                   |
| ---------------------------- | ---- | ------ | ---------------------- |
| `AGENT_MAX_CONCURRENT_TASKS` | int  | 10     | 最大并发任务数         |
| `AGENT_MAX_CONCURRENT_TOOLS` | int  | 3      | 单个任务最大并发工具数 |
| `AGENT_TASK_QUEUE_SIZE`      | int  | 50     | 任务队列大小           |
| `AGENT_WORKER_POOL_SIZE`     | int  | 5      | 工作线程池大小         |

**调优建议：**

- **CPU 密集型**：`WORKER_POOL_SIZE = CPU 核心数`
- **IO 密集型**：`WORKER_POOL_SIZE = CPU 核心数 * 2`
- **LLM 调用密集**：降低 `MAX_CONCURRENT_TASKS`（避免 API 限流）

---

### 6. 记忆系统配置

| 配置项                  | 类型 | 默认值         | 说明             |
| ----------------------- | ---- | -------------- | ---------------- |
| `MEMORY_ENABLED`        | bool | false          | 是否启用长期记忆 |
| `MEMORY_MAX_SHORT_TERM` | int  | 10             | 短期记忆保留条数 |
| `MEMORY_VECTOR_DB`      | str  | "milvus"       | 向量数据库类型   |
| `MEMORY_COLLECTION`     | str  | "agent_memory" | 集合名称         |

**支持的向量数据库：**

- Milvus
- Qdrant
- Pinecone

---

### 7. 数据库配置

| 配置项                  | 类型 | 默认值                                        | 说明             |
| ----------------------- | ---- | --------------------------------------------- | ---------------- |
| `DATABASE_URL`          | str  | "postgresql+asyncpg://user:pass@localhost/db" | 数据库连接字符串 |
| `DATABASE_POOL_SIZE`    | int  | 20                                            | 连接池大小       |
| `DATABASE_MAX_OVERFLOW` | int  | 10                                            | 最大溢出连接数   |
| `DATABASE_ECHO`         | bool | false                                         | 是否打印 SQL     |

---

### 8. Redis 配置

| 配置项              | 类型 | 默认值                     | 说明                          |
| ------------------- | ---- | -------------------------- | ----------------------------- |
| `REDIS_URL`         | str  | "redis://localhost:6379/0" | Redis 连接字符串              |
| `REDIS_SESSION_TTL` | int  | 604800                     | 会话过期时间（秒，默认 7 天） |

---

### 9. 工具配置

| 配置项                    | 类型 | 默认值 | 说明                           |
| ------------------------- | ---- | ------ | ------------------------------ |
| `TOOL_TIMEOUT`            | int  | 30     | 工具执行超时时间（秒）         |
| `TOOL_MAX_RETRIES`        | int  | 3      | 工具执行最大重试次数           |
| `TOOL_MAX_OUTPUT_LENGTH`  | int  | 100000 | 工具输出截断长度（字符数）     |
| `TOOL_MAX_CONTENT_LENGTH` | int  | 50000  | 网页抓取最大内容长度（字符数） |

**使用场景：**

- `TOOL_TIMEOUT`：单个工具执行不能超过此时间，通过 `asyncio.wait_for` 实现
- `TOOL_MAX_RETRIES`：失败后自动重试次数，配合渐进式退避策略（1s, 2s, 4s...）
- `TOOL_MAX_OUTPUT_LENGTH`：影响 `readFile` 和 `code_exec` 的输出截断
- `TOOL_MAX_CONTENT_LENGTH`：影响 `web_browse` 的网页内容截断

**聚合属性 `tool_config` 返回：**

```python
{
    "timeout": 30,
    "max_retries": 3,
    "max_output_length": 100000,
    "max_content_length": 50000,
}
```

---

### 10. Tavily 配置

| 配置项                | 类型 | 默认值  | 说明                       |
| --------------------- | ---- | ------- | -------------------------- |
| `TAVILY_API_KEY`      | str  | ""      | Tavily API 密钥            |
| `TAVILY_SEARCH_DEPTH` | str  | "basic" | 搜索深度（basic/advanced） |

---

### 11. 日志配置

| 配置项       | 类型 | 默认值         | 说明                  |
| ------------ | ---- | -------------- | --------------------- |
| `LOG_LEVEL`  | str  | "INFO"         | 日志级别              |
| `LOG_FORMAT` | str  | "json"         | 日志格式（json/text） |
| `LOG_FILE`   | str  | "logs/app.log" | 日志文件路径          |

---

### 12. 监控配置

| 配置项            | 类型 | 默认值 | 说明                |
| ----------------- | ---- | ------ | ------------------- |
| `METRICS_ENABLED` | bool | false  | 是否启用监控        |
| `METRICS_PORT`    | int  | 9090   | Prometheus 指标端口 |

---

### 13. 安全配置

| 配置项               | 类型 | 默认值                                 | 说明                 |
| -------------------- | ---- | -------------------------------------- | -------------------- |
| `JWT_SECRET_KEY`     | str  | "your-secret-key-change-in-production" | JWT 密钥             |
| `JWT_ALGORITHM`      | str  | "HS256"                                | JWT 算法             |
| `JWT_EXPIRE_MINUTES` | int  | 1440                                   | JWT 过期时间（分钟） |

**安全提示：**

- 生产环境必须修改 `JWT_SECRET_KEY`
- 使用强密钥（至少 32 字符随机字符串）

---

## 使用示例

### 1. 基本使用

```python
from app.config import settings

# 访问单个配置项
api_key = settings.llm_api_key
model_id = settings.llm_model_id

# 判断环境
if settings.is_production:
    # 生产环境逻辑
    pass
else:
    # 开发环境逻辑
    pass
```

### 2. 使用聚合配置

```python
from app.config import settings

# 获取 LLM 配置字典
llm_config = settings.llm_config
# {'api_key': 'sk-xxx', 'base_url': '...', 'model': 'gpt-4', ...}

# 获取 Agent 配置字典
agent_config = settings.agent_config

# 获取并发控制配置
concurrency_config = settings.concurrency_config

# 传递给服务初始化
llm_service = LLMService(**settings.llm_config)
```

### 3. 多模型切换

```python
from app.config import settings

# 主模型 - 用于对话
main_llm = LLMService(**settings.llm_config)

# 推理模型 - 用于复杂推理
reasoning_llm = LLMService(**settings.llm_reasoning_config)

# 快速模型 - 用于简单任务
fast_llm = LLMService(**settings.llm_fast_config)

# 根据任务类型选择模型
def get_llm_for_task(task_type: str):
    if task_type == "reasoning":
        return reasoning_llm
    elif task_type == "simple":
        return fast_llm
    else:
        return main_llm
```

### 4. 优先级判断

```python
from app.config import settings

def get_timeout_by_priority(priority: str) -> int:
    """根据优先级返回超时时间"""
    if priority == "high":
        return settings.agent_high_priority_timeout
    elif priority == "low":
        return settings.agent_low_priority_timeout
    else:
        return settings.agent_timeout
```

---

## 环境变量配置

### 配置优先级

```
系统环境变量 > .env 文件 > 代码默认值
```

### .env 文件示例

```bash
# ===== LLM 主模型配置 =====
LLM_API_KEY="sk-xxx"
LLM_BASE_URL="https://api.deepseek.com"
LLM_MODEL_ID="deepseek-v4-pro"
LLM_TEMPERATURE=0.2

# ===== LLM 推理模型配置 =====
LLM_REASONING_MODEL_ID="deepseek-reasoner"
LLM_REASONING_TEMPERATURE=0.7

# ===== Agent 并发配置 =====
AGENT_MAX_CONCURRENT_TASKS=20
AGENT_MAX_CONCURRENT_TOOLS=5

# ===== 数据库配置 =====
DATABASE_URL="postgresql+asyncpg://user:pass@localhost/db"
REDIS_URL="redis://localhost:6379/0"
```

### 环境区分

```bash
# 开发环境 (.env.development)
DEBUG=true
LOG_LEVEL="DEBUG"
DATABASE_URL="postgresql+asyncpg://dev:dev@localhost/dev_db"

# 生产环境 (.env.production)
DEBUG=false
LOG_LEVEL="INFO"
DATABASE_URL="postgresql+asyncpg://prod:prod@prod-server/prod_db"
```

---

## 最佳实践

### 1. 敏感信息管理

❌ **错误做法：**

```python
# 不要在代码中硬编码敏感信息
api_key = "sk-xxx"
```

✅ **正确做法：**

```python
# 从环境变量读取
from app.config import settings
api_key = settings.llm_api_key
```

### 2. 配置验证

```python
# 在应用启动时验证关键配置
from app.config import settings

def validate_config():
    if not settings.llm_api_key:
        raise ValueError("LLM_API_KEY 未配置")

    if settings.agent_max_iterations > 50:
        print("警告：AGENT_MAX_ITERATIONS 过大，可能导致性能问题")

# 在 main.py 中调用
async def lifespan(app: FastAPI):
    validate_config()
    # ... 其他初始化
```

### 3. 配置变更通知

```python
# 使用属性方法封装逻辑
class Settings(BaseSettings):
    @property
    def effective_timeout(self) -> int:
        """根据环境返回有效超时时间"""
        if self.debug:
            return 0  # 开发环境不超时
        return self.agent_timeout
```

### 4. 配置文档化

```python
class Settings(BaseSettings):
    """应用配置类

    所有配置项从环境变量加载，支持 .env 文件。

    环境变量命名规则：
    - 全大写
    - 下划线分隔
    - 如：LLM_API_KEY, AGENT_MAX_ITERATIONS

    示例：
        >>> from app.config import settings
        >>> settings.llm_model_id
        'gpt-4'
    """
```

---

## 常见问题

### Q1: 如何动态修改配置？

**A:** 配置是单例，不建议运行时修改。如果需要动态配置：

```python
# 方案1：重新加载配置（不推荐）
settings = Settings()  # 重新实例化

# 方案2：使用 Redis 存储动态配置（推荐）
dynamic_config = await redis.get("dynamic_config")
```

### Q2: 配置项验证失败怎么办？

**A:** Pydantic 会在启动时抛出 ValidationError：

```python
from pydantic import ValidationError

try:
    settings = Settings()
except ValidationError as e:
    print(f"配置验证失败: {e}")
    # 检查类型、必填项等
```

### Q3: 如何支持多环境配置？

**A:** 推荐方案：

```bash
# 使用不同的 .env 文件
.env.development
.env.staging
.env.production

# 启动时指定
cp .env.production .env
python main.py
```

### Q4: 配置项命名规范？

**A:** 环境变量命名规范：

- ✅ 全大写：`LLM_API_KEY`
- ✅ 下划线分隔：`AGENT_MAX_ITERATIONS`
- ❌ 避免：`llmApiKey`、`agent-max-iterations`

### Q5: 如何调试配置加载？

**A:** 启用 DEBUG 模式：

```python
import logging
logging.basicConfig(level=logging.DEBUG)

from app.config import settings
# 会打印配置加载过程
```

---

## 总结

配置管理模块是整个系统的基础设施，正确使用可以：

1. **集中管理**：所有配置统一维护
2. **类型安全**：启动时发现配置错误
3. **环境隔离**：轻松切换不同环境
4. **易于维护**：配置变更影响范围明确

---

**相关文档：**

- [架构设计文档](architecture.md)
- [工具模块说明](tools.md)
- [API 文档](api.md)
- [部署文档](deployment.md)

## 后续优化建议

以下为当前版本中识别出的可优化项，根据实际需求选择性实施：

### 🔴 缺失配置项

| 类别       | 建议增加配置项                        | 原因                     |
| ---------- | ------------------------------------- | ------------------------ |
| LLM 配置   | `LLM_RETRY_TIMES` / `LLM_RETRY_DELAY` | API 调用失败的重试机制   |
| Agent 配置 | `AGENT_ENABLE_REFLECTION`             | 是否启用反思机制         |
|            | `AGENT_MAX_TOOL_CALLS`                | 单次任务最大工具调用次数 |
| 数据库配置 | `DATABASE_SSL_MODE`                   | SSL 连接模式             |
|            | `DATABASE_CONNECT_TIMEOUT`            | 连接超时时间             |
| Redis 配置 | `REDIS_MAX_CONNECTIONS`               | 最大连接数               |
| 日志配置   | `LOG_MAX_FILE_SIZE`                   | 单个日志文件最大大小     |
|            | `LOG_BACKUP_COUNT`                    | 日志文件备份数量         |
| 安全配置   | `RATE_LIMIT_REQUESTS`                 | API 限流请求数           |
|            | `RATE_LIMIT_PERIOD`                   | 限流时间窗口（秒）       |

### 🟡 默认值优化建议

| 配置项                       | 当前默认值    | 建议值           | 原因               |
| ---------------------------- | ------------- | ---------------- | ------------------ |
| `AGENT_TIMEOUT`              | 300s (5分钟)  | 600s (10分钟)    | 复杂任务可能超时   |
| `DATABASE_POOL_SIZE`         | 20            | 50-100           | 生产环境高并发需要 |
| `AGENT_MAX_CONCURRENT_TASKS` | 10            | 20-50            | 提升吞吐量         |
| `JWT_EXPIRE_MINUTES`         | 1440 (24小时) | 60-480 (1-8小时) | 安全最佳实践       |

### 🟢 架构优化

1. **环境变量命名统一**
   - 当前 `MAX_CONTEXT_TOKENS` 缺少 `LLM_` 前缀
   - 建议统一为 `LLM_MAX_CONTEXT_TOKENS`

2. **配置热更新**
   - 当前配置在启动时加载，运行中不可变
   - 可增加 Redis 存储的动态配置，支持运行时修改

3. **配置验证增强**
   - 可增加 `@model_validator` 在启动时检查配置项的依赖关系
   - 如：启用记忆时必须配置嵌入模型

4. **多环境配置**
   - 可支持 `env_file=".env.{environment}"` 模式
   - 如 `.env.development`、`.env.production`

5. **配置导出**
   - 可增加 `export_yaml()` / `export_json()` 方法
   - 便于运维系统读取配置
