# TOOLS-018 hooks logger 名 `services.tool_service` 与模块路径不符

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P3（可观测性，次要项）
> **来源**：2026-08-18 工具模块代码审核（编排核心层 · 次要项 7）
> **涉及模块**：`app/integration/tools/hooks.py`（ExecutionHooks logger）
> **关联文档**：[logging.md](../../../docs/platform_doc/observability/logging.md)

---

## 问题描述

### 现象

`hooks.py` logger 名 `services.tool_service`，与模块实际路径 `app.integration.tools.hooks` 不符，且与 `tool_service.py` 的 `tools.service` 命名空间不一致——观测日志归属混乱。

### 影响

按模块检索日志时 hooks 日志混入错误的命名空间，可观测性失真。

### 根因

logger 名硬编码错误（疑似从旧模块路径复制）。

---

## 修复方案

logger 名改为 `tools.hooks`（对齐模块路径与 `tools.*` 命名约定）；[logging.md](../../../docs/platform_doc/observability/logging.md) 各模块日志清单修正（`tools.service` 说明改「工具 on_unload 失败」+ 新增 `tools.hooks` 行「工具钩子执行失败」）。

**取舍**：无逻辑改动，仅日志命名空间归位。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/hooks.py` | `get_logger("services.tool_service")` → `get_logger("tools.hooks")` | 现有测试通过（无逻辑改动） |
| `docs/platform_doc/observability/logging.md` | 日志清单：`tools.service` 说明修正 + 新增 `tools.hooks` | — |

---

## 验证

- 相关测试（executor 组件等）通过（无逻辑改动）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **logger 名是观测契约**：必须与模块路径一致（`tools.*` / `app.*` 命名空间），硬编码错名让日志归属失真。
- **文档清单与代码对齐**：logging.md 的模块→logger 清单随代码修正，避免双处漂移。
