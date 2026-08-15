# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

工业级 AI Agent 系统：FastAPI + OpenAI API 协议（兼容 DeepSeek），实现完整 ReAct 循环 Agent（推理 ↔ 工具调用 ↔ 推理）。产品方向为多 Agent 任务执行引擎 + 半导体良率异常根因分析（Yield RCA）。架构蓝图见 [docs/architecture.md](docs/architecture.md)。

## 工作流 gate

- **改代码前**：先读 [docs/architecture.md](docs/architecture.md) 与相关模块文档
- **改代码后**：同步 [docs/](docs/) 对应模块文档，参考 [docs/ALIGNMENT.md](docs/ALIGNMENT.md) 校验（`scripts/verify_alignment.py`）

## 项目通用规则（Writer、Reviewer 共用）

1. 语言规范、代码风格遵循项目配置
2. 新增代码必须附带单元测试
3. 禁止硬编码密钥、端口、配置
4. 写/改文件前先输出实现方案，确认后再写入磁盘；只修改需求指定模块
5. 评审标准：逻辑正确性、边界条件、异常捕获、性能、安全、可读性

## 仓库布局

```
app/      代码（接入/应用/领域/集成/基础设施/共享/配置）
docs/     文档（架构/模块说明/对齐表）
tests/    单元 + 集成 + e2e 测试
scripts/  工具脚本（init_db / verify_alignment 等）
```

## 常用命令

```bash
uv sync                                  # 安装生产 + dev 依赖（pytest/debugpy）
uv run python -m app.main                # 启动服务（uvicorn reload，0.0.0.0:8000）
uv run pytest                            # 全量测试（testpaths=tests, asyncio_mode=auto）
uv run pytest tests/unit/test_retry.py   # 运行单个测试文件
uv run python -m scripts.test_search_tool          # 运行独立验证脚本
```

- pytest 为 `asyncio_mode = "auto"`，测试函数无需 `@pytest.mark.asyncio`
- 运行脚本一律用 `uv run python -m scripts.xxx`，**不要**用 `uv run ./scripts/xxx.py`（会把 `scripts/` 加入 sys.path 导致找不到 `app` 模块）
- 独立脚本顶部需 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`，否则打印非 ASCII 符号到 Windows 控制台会 `UnicodeEncodeError`

## 提交前本地检查

- `uv run pytest` 全量通过；仅改动相关模块时运行对应测试文件
- 新增代码必须附带单元测试

## 环境与密钥

- 配置从 `.env` 加载（`app/config/settings.py`，模板见 `.env.example`）；禁止硬编码、禁止提交密钥

## 文档体系

- **关键文档**：[architecture.md](docs/architecture.md)（架构蓝图 + 演进路径）· [ALIGNMENT.md](docs/ALIGNMENT.md)（代码↔文档↔测试对齐表）· [config.md](docs/config_doc/config.md) · [deployment.md](docs/deployment.md)
- **各层模块说明**：`docs/` 下按模块目录对应（domain/application/integration/infrastructure/api/shared/utils）
- **计划与研发教训**：归档于 [docs/todo.md](docs/todo.md) / [docs/lessons.md](docs/lessons.md)
- **硬性约定**：修改模块后必须同步对应模块文档（见上方工作流 gate）
