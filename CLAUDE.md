# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

工业级 AI Agent 系统：FastAPI + OpenAI API 协议（兼容 DeepSeek），实现完整 ReAct 循环 Agent（推理 ↔ 工具调用 ↔ 推理）。产品方向为多 Agent 任务执行引擎 + 半导体良率异常根因分析（Yield RCA）。架构蓝图见 [docs/architecture.md](docs/architecture.md)。

## 工作流 gate

- **改代码前**：先读 [docs/architecture.md](docs/architecture.md) 与相关模块文档
- **改代码后**：同步 [docs/](docs/) 对应模块文档，参考 [docs/ALIGNMENT.md](docs/ALIGNMENT.md) 校验（`scripts/verify_alignment.py`）
- **问题记录（Problem/Issue Log）**：审查/审核发现问题后，先调研工业级修复方案并确立本项目采用方案，再修复；修复后记录到 `docs/issues/<模块名>/<问题文件名>`（一个问题一个文件，记录生命周期：发现 → 分析 → 修复 → 验证 → 教训）。关联的说明文档只链接到该问题文件，不展开描述
- **决策记录（ADR）**：每次结构性/契约性设计决策，先调研工业级决策方案并确立本项目采用方案，实施后记录到 `docs/adr/<模块名>/<决策文件名>`（一个决策一个文件，记录为什么做这个决策：Context → Decision → Consequences）

## 项目通用规则（Writer、Reviewer 共用）

1. 语言规范、代码风格遵循项目配置
2. 新增代码必须附带单元测试
3. 禁止硬编码密钥、端口、配置
4. 写/改文件前先输出实现方案，确认后再写入磁盘；只修改需求指定模块
5. 评审标准：逻辑正确性、边界条件、异常捕获、性能、安全、可读性

## 文档组织硬性规则

1. **一个事实一个家**：同一进度/机制/问题只在最该出现的一处文档描述，其他文档只链接过去，禁止复述（双处维护必然漂移）
2. **写当前状态，不写历史**：文档只描述当前状态，禁止 "previously / no longer / 修复前 / 原实现" 这类演进叙事；历史演进归 git commit 与 Problem/Issue Log（`docs/issues/`）与 ADR（`docs/adr/`）

## 文档体系

- **关键文档**：[architecture.md](docs/architecture.md)（架构蓝图 + 演进路径）· [ALIGNMENT.md](docs/ALIGNMENT.md)（代码↔文档↔测试对齐表）· [config.md](docs/config_doc/config.md) · [deployment.md](docs/deployment.md)
- **各层模块说明**：`docs/` 下按模块目录对应（domain/application/integration/infrastructure/api/shared/utils）
- **模块级 README 写作规范**（每层 `docs/<layer>_doc/README.md` 是该层**总览 + 导航**页，范式见 domain / application / integration 三层）：
  1. 标准结构（自上而下）：`#` 层标题 → 元信息引用块（`对应代码` / `更新日期` / `文档定位` / `实现状态`）→ `## 📋 目录` → `## 模块概述`（核心功能一句话 + 模块结构树（text 语言代码块） + 设计原则 + 依赖关系）→ `## 实现状态总览`（子模块 × 文件 × 状态 × 核心内容表）→ 各子系统一节（核心功能 + 组件表）→ `## 典型调用链路` → `## 配置关联` → `## 相关文档`
  2. 结构 / 依赖图代码块一律指定 `text` 语言标识，禁止裸代码块
  3. **总览 + 导航**：子系统只给「组件表 + 设计要点」，细节链接子文档，不把子文档内容搬进 README（避免双处维护）
  4. 状态徽标对齐架构文档：✅ 已实现 ｜ 🔶 进行中 ｜ ⬜ 待规划
  5. 以实际代码为准：子模块结构以 `app/` 实况为准；旧文档与代码不符时以代码为准并标注
- **组件级子文档设计规范**：独立子文档（`docs/<layer>_doc/**/*.md`）的创建 / 优化按 [component_doc.md](docs/component_doc.md) 实施——类型分离（Reference / Explanation）、粒度分级（简单组件轻量 Reference ~80 行 / 复杂组件完整设计文档）、契约优先、状态标注、创建后同步父文档链接与 ALIGNMENT 并跑 verify_alignment
- **计划与研发教训**：归档于 [docs/todo.md](docs/todo.md) / [docs/lessons.md](docs/lessons.md)
- **硬性约定**：修改模块后必须同步对应模块文档与**该层 README**（新增/重命名/删除子模块、改设计或状态时同步更新；见上方工作流 gate）

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
