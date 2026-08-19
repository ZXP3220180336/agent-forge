# TOOLS-023 web_browse 连接层超时 / 重定向硬编码，未走 register_config

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P3（配置一致性，次要项）
> **来源**：2026-08-18 工具模块代码审核（builtin 通用工具组 · 次要项 12）
> **涉及模块**：`app/integration/tools/builtin/web_browse.py`（WebBrowseTool）
> **关联文档**：[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

---

## 问题描述

### 现象

`web_browse` 的 httpx 连接层 `timeout=15.0`、`max_redirects=5` 硬编码，未走 `register_config` 注入，与「URL / 超时配置注入」约定不一致（search / code_exec / http_api 均已注入）。

### 影响

配置无法统一管理；工具默认超时（15s）与内部连接层超时绑定在代码里。

### 根因

register_config 仅支持 `max_content_length`，连接层参数硬编码。

---

## 修复方案

`WebBrowseTool` 增加类属性 `_timeout` / `_max_redirects`，`register_config` 支持 `timeout` / `max_redirects` 注入；`_get_http_client` 构建 client 用类值（默认 15.0 / 5，可注入覆盖）。

**取舍**：默认值保留（合理默认非密钥），装配根 / 调用方可注入覆盖；client 为全局单例，启动注入早于首次 execute → 首次构建用注入值。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/web_browse.py` | `_timeout` / `_max_redirects` 类属性 + register_config 参数 + `_get_http_client` 用类值 | `tests/integration/test_tool_execution.py` 新增 `test_web_browse_config_injects_timeout`（注入超时进 client） |
| 文档 | [builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md) web_browse 实现要点补注入说明 | — |

---

## 验证

- 相关测试 **3 passed**（web_browse）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **连接层参数也走注入**：不只内容截断，超时 / 重定向等连接层配置统一 register_config 注入，避免硬编码漂移。
- **单例 client 注入时序**：装配根启动注入早于首次 execute，client 首次构建即用注入值；重置需先关闭单例。
