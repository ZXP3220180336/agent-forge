# TOOLS-002 文件工具无路径范围限制，可读 .env / 覆盖源码

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P1（安全边界）
> **来源**：2026-08-18 工具模块代码审核（builtin 通用工具组 · 重要项 3）
> **涉及模块**：`app/integration/tools/builtin/file_ops.py`（ReadFileTool / WriteFileTool）+ 配置注入链（settings / container）
> **关联文档**：[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md) · [config.md](../../../docs/config_doc/config.md)

---

## 问题描述

### 现象

`readFile` / `writeFile` 接受任意绝对路径，`risk_level` 仅作标注、不拦截。`readFile(".env")` 可把 `TAVILY_API_KEY` 等密钥注入 LLM 上下文并经响应/日志外泄；`writeFile` 可覆盖 `app/config/settings.py` 或源码。工具未设 `requires_approval=True`，配合网页/搜索内容的提示注入即可被驱动。

### 影响

Agent 可通过间接输入诱导读写任意本地文件——读敏感文件泄露凭据、写关键文件破坏代码/配置。文件工具是 RCA 主链路常用工具（读良率数据、写排查报告），风险敞口直接作用于主链路。

### 根因

工具无「允许目录」概念，`file_path` 不做任何范围校验即进入 `aiofiles.open`。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| OpenAI Code Interpreter / Anthropic Sandbox | Agent 文件访问**限定在沙箱目录**，目录外操作拒绝——Agent 自主但受控 |
| Cursor / VS Code | 文件操作限于**工作区信任目录**，不受信路径被拦截 |
| 路径穿越防护实践（OWASP） | 规范化（`abspath` 解析 `..`）+ 大小写归一（Windows）+ **前缀含分隔符**（防 `/data` 误放行 `/database`） |

**核心**：白名单（sandbox/workspace 限制）优于人工审批——前者让 Agent 在受控目录内自主推进主链路，后者打断 Agent 循环（RCA 并行排查场景不适用 HITL）。

---

## 修复方案（含决策取舍）

**决策**：文件工具注入**允许目录白名单**（register_config），白名单外返回业务错误：

| 层 | 改动 |
| --- | --- |
| 工具层 `file_ops.py` | `_allowed_dirs` ClassVar + `register_config(allowed_dirs=...)`；`_is_path_allowed` 校验：`abspath + normcase` 规范化后必须等于某允许目录或是其子路径（`os.sep` 分隔前缀）；白名单外返回 `"文件路径不在允许目录内，拒绝访问"`（业务错误，`error_code=None`） |
| 配置层 `settings.py` | `tool_allowed_dirs: tuple[str, ...] = (str(Path(__file__).resolve().parents[2]),)` —— 默认项目根 |
| 装配根 `container.py` | `ReadFileTool` 注入 `allowed_dirs=settings.tool_allowed_dirs`；新增 `WriteFileTool.register_config` 同样注入 |

**取舍理由**：

1. **fail-closed 默认**：register_config 未调用时 `_allowed_dirs = ()` → 拒绝所有文件操作，杜绝「配置遗漏 = 裸奔」；生产由装配根注入 settings（默认项目根）兜底；
2. **白名单优于 HITL**：`requires_approval=True` 会打断 Agent 自主排查（产品主链路不适用），白名单让 Agent 在受控目录内自主；
3. **规范化防穿越**：`abspath` 解析 `..`、`normcase` 归一 Windows 大小写、`os.sep` 前缀防 `/data` 误放行 `/database`。

**语义边界**：

- 默认白名单 = 项目根，良率数据文件在项目外时经 `TOOL_ALLOWED_DIRS`（JSON 数组）扩展；
- 正常路径（白名单内）行为不变；`..` 穿越、前缀目录、大小写变体均被规范化拦截。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/file_ops.py` | 模块级 `_normalize_allowed_dirs` / `_is_path_allowed`；ReadFileTool 与 WriteFileTool 增加 `_allowed_dirs` + `register_config(allowed_dirs=...)`；两工具 execute 打开文件前白名单校验 | `tests/integration/test_tool_execution.py` 新增 4 用例：白名单外 read 拒 / 白名单外 write 拒 / `..` 穿越拒 / 前缀目录不误放行；3 个现有文件用例注入 `allowed_dirs=(str(tmp_path),)` |
| `app/config/settings.py` | `tool_allowed_dirs` 配置项（默认项目根，`Path` 相对 settings.py 定位） | `tests/unit/test_settings.py` 原断言不受影响 |
| `app/container.py` | ReadFileTool 注入 `allowed_dirs`；新增 WriteFileTool.register_config 注入 | `tests/unit/test_container.py` |
| 测试适配 | `tests/integration/test_chat_flow.py`（WriteFileTool 注入 tmp_path）、`scripts/test_agent.py`（注入 `settings.tool_allowed_dirs`） | — |
| 文档 | [builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)（两工具加「安全限制」行 + 实现要点）；[config.md](../../../docs/config_doc/config.md)（`TOOL_ALLOWED_DIRS` 配置项）；`.env.example`（示例注释） | — |

---

## 验证

- 全量测试 **475 passed**（60.84s，含 4 个新增白名单用例），无回归
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **fail-closed 优于 fail-open**：安全边界默认拒绝，配置显式放开——「配置遗漏」必须是拒绝而非放行。
- **路径校验必须规范化**：`abspath`（防 `..` 穿越）+ `normcase`（Windows 大小写）+ `os.sep` 前缀（防目录前缀误放行）三件套缺一不可。
- **白名单 vs HITL 是产品选择**：RCA 主链路要求 Agent 自主并行排查，白名单（受控目录内自主）优于审批（打断循环）；HITL 留给真正的不可逆高风险操作。
