# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

工业级 AI Agent 系统：FastAPI + OpenAI API 协议（兼容 DeepSeek），实现完整 ReAct 循环 Agent（推理 ↔ 工具调用 ↔ 推理）。产品方向为多 Agent 任务执行引擎 + 半导体良率异常根因分析（Yield RCA）。架构蓝图见 [docs/architecture.md](docs/architecture.md)。

## 工作流 gate —— 模块开发全生命周期

> 开发 / 修改一个模块时，按以下顺序执行。每一步的产物（ADR / issues / 文档 / 测试）是下一步的前置。简单修改（单行修复、文档措辞）可跳过调研 / 设计 gate，直接实现。

### 1. 调研 gate（仅复杂 / 有决策权重的模块）

- 涉及架构选型 / 算法选择 / 契约设计（如限流、重试、熔断、结构化输出）时，**先联网调研工业级实现路径**（OpenAI SDK / LangChain / 主流框架 / 工业案例），确立本项目采用方案后再设计
- 调研结果定案后进 ADR 的「工业级参照」栏，不单独成文

### 2. 设计 gate —— 契约先行

- 先定模块**对外接口契约**：Facade 方法签名、参数 / 返回 / 异常、配置注入点、数据流（模块入口 → 出口）
- 非平凡任务（步骤 > 3 或涉及架构决策）：先进计划模式，输出实现方案确认后再写磁盘
- 需求模糊时先澄清，不脑补需求

### 3. 实现 gate —— 垂直切片

- **垂直切片**：用桩 / mock 子组件搭出编排结构，跑通一条最小端到端链路（Facade 入口 → 出口），测试锚定（外部依赖 mock）
- **细化子组件**：逐个替换桩为真实实现，每个子组件各带测试，编排测试保持通过（回归护栏）

### 4. 测试 gate

- 新增代码必须附带单元测试（pytest 为 `asyncio_mode = "auto"`，无需 `@pytest.mark.asyncio`）
- 细化子组件时：先跑相关测试文件，再全量

### 5. 文档同步 gate（测试之后）

- 改代码后同步：对应**模块文档** + **该层 README** + [ALIGNMENT.md](docs/ALIGNMENT.md)（新增 / 重命名 / 删除子模块、改设计或状态时）
- 跑 `uv run python -m scripts.verify_alignment` 校验
- 结构 / 依赖图代码块一律指定 `text` 语言；以实际代码为准（旧文档与代码不符时以代码为准并标注）

### 6. 问题记录 gate

- 审查 / 审核发现问题：先调研工业级修复方案并确立本项目采用方案，再修复
- 修复后记录到 `issues/<层名>/<模块名>/<日期-问题>.md`（一个问题一个文件，生命周期：发现 → 分析 → 修复 → 验证 → 教训）
- 关联的说明文档只链接到该问题文件，不展开描述

### 7. 决策记录 gate

- 结构性 / 契约性设计决策：先调研工业级决策方案并确立本项目采用方案，实施后记录到 `adr/<层名>/<模块名>/<日期-决策>.md`（一个决策一个文件：Context → Decision → Consequences）

## 文档体系

### 文档组织硬性规则

1. **一个事实一个家**：同一进度 / 机制 / 问题只在最该出现的一处文档描述，其他文档只链接过去，禁止复述（双处维护必然漂移）
2. **写当前状态，不写历史**：文档只描述当前状态，禁止演进叙事（previously / no longer / 修复前 / 原实现）；历史演进归 git commit 与 issues 与 ADR

### 关键文档

[architecture.md](docs/architecture.md)（架构蓝图 + 演进路径）· [ALIGNMENT.md](docs/ALIGNMENT.md)（代码↔文档↔测试对齐表）· [config.md](docs/config_doc/config.md) · [deployment.md](docs/deployment.md)

### 各层模块说明

`docs/` 下按模块目录对应（domain / application / integration / infrastructure / api / shared / platform）

### 三份文档规范

| 规范 | 适用文档 |
| --- | --- |
| [层级 README 写作规范](docs/layer_readme_doc.md) | 层 README（`docs/<layer>_doc/README.md`，总览 + 导航） |
| [模块对外接口文档规范](docs/module_doc.md) | 模块对外接口文档（如 `llm.md`，Facade 契约 + 组件导航） |
| [组件级子文档设计规范](docs/component_doc.md) | 组件子文档（如 `client.md`，单组件设计） |

### 横切主题文档

[评测与评估](docs/eval_doc/evaluation.md)（Agent 质量基线）· [安全 / 可观测性](docs/platform_doc/security.md)（platform 横切域）· [可观测性](docs/platform_doc/observability.md)

### 计划与研发教训

归档于 [docs/todo.md](docs/todo.md) / [docs/lessons.md](docs/lessons.md)

## 项目通用规则（Writer、Reviewer 共用）

1. 语言规范、代码风格遵循项目配置
2. 新增代码必须附带单元测试
3. 禁止硬编码密钥、端口、配置
4. 写 / 改文件前先输出实现方案，确认后再写入磁盘；只修改需求指定模块
5. 评审标准：逻辑正确性、边界条件、异常捕获、性能、安全、可读性

## 仓库布局

```text
app/      代码（接入/应用/领域/集成/基础设施/共享/平台/配置）
docs/     文档（架构/模块说明/对齐表/规范）
adr/      决策记录（Context → Decision → Consequences）
issues/   问题记录（发现 → 分析 → 修复 → 验证 → 教训）
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
- `uv run python -m scripts.verify_alignment` 通过（改文档后）
- 新增代码必须附带单元测试

## 环境与密钥

- 配置从 `.env` 加载（`app/config/settings.py`，模板见 `.env.example`）；禁止硬编码、禁止提交密钥
