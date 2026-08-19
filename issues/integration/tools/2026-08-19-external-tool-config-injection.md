# TOOLS-010 外部工具无配置注入通道，register_config 形同虚设

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P1（契约 / 配置一致性）
> **来源**：2026-08-18 工具模块代码审核（编排核心层 · 重要项 4 + external 组）
> **涉及模块**：`app/integration/tools/loader.py`（ExternalToolLoader 配置注入）+ `app/integration/tools/external/http_api.py`（示例工具）+ tool_service / container / settings
> **关联文档**：[external.md](../../../docs/integration_doc/tools_doc/external.md) · [config.md](../../../docs/config_doc/config.md)

---

## 问题描述

### 现象

loader 加载外部工具只做 `cls()` + `on_load()`，**从不调用 `register_config`**。内置工具惯用的配置注入风格（装配根 `register_config(**settings)`）移植到外部工具会被静默忽略——工具行为错误且难排查。示例 `http_api.py` 用模块级 `_CLIENT_TIMEOUT = 15.0` 硬编码超时，与「禁止硬编码配置」规则冲突。

### 影响

外部工具无法经配置注入获得 settings 值（超时 / 地址 / 阈值等），只能硬编码或自行读环境变量（破坏统一配置入口）；配置注入契约内外不一致。

### 根因

loader 无配置注入点：外部工具自动加载、无装配根，`register_config` 无处被调用。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| 依赖注入（装配根） | 组件配置由装配根注入，组件不直接读全局状态——外部工具需等价注入路径 |
| 热插拔插件配置（manifest） | 插件声明所需配置（键清单），宿主从配置源取值注入 |

**核心**：插件声明配置需求（`CONFIG_KEYS`），宿主注入——不要求插件自行读环境变量，对齐内置工具 `register_config` 风格。

---

## 修复方案（含决策取舍）

**决策**：loader 增加配置注入点（模块 `CONFIG_KEYS` 声明 → `config_source` 取值 → `register_config`）：

| 层 | 改动 |
| --- | --- |
| `loader.py` | 构造加 `config_source: Callable[[str], Any] | None`；新增 `_inject_config`——模块声明 `CONFIG_KEYS` 且类有 `register_config` 且 config_source 存在时，从 config_source 取各键值（非 None）调 `register_config(**config)`；`_load_file` 实例化前调用 |
| `tool_service.py` | 构造加 `external_config_source`，传给 loader |
| `container.py` | `external_config_source=lambda key: getattr(settings, key, None)`（装配根绑定 settings） |
| `settings.py` | `tool_http_timeout: float = 15.0`（示例工具超时配置） |
| `http_api.py` 示例 | 模块 `CONFIG_KEYS = ("tool_http_timeout",)` + 类 `register_config` 注入 `_client_timeout`（ClassVar）；移除硬编码 `_CLIENT_TIMEOUT`（保留默认值兜底） |

**取舍理由**：

1. **声明式配置需求**：`CONFIG_KEYS`（模块级）声明键清单，宿主注入——插件不碰 settings，符合「组件不直接读全局状态」；
2. **向后兼容**：未声明 `CONFIG_KEYS` / 不实现 `register_config` / 无 `config_source` 均跳过注入，现有外部工具不受影响；
3. **config_source 经装配根绑定**：loader 不直接 import settings（分层保持），container 传 `getattr(settings, key, None)` 读取器。

**语义边界**：`config_source` 返回 `None` 的键跳过（未配置用工具默认值）；重载时 `_load_file` 重新注入（新模块 CONFIG_KEYS 生效）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/loader.py` | 构造 `config_source` + `_inject_config` + `_load_file` 调用 | `tests/unit/test_tool_loader.py` 新增 2 用例：`test_load_without_config_source_keeps_default`（无 config_source 跳过注入）+ `test_load_injects_config_from_source`（CONFIG_KEYS → register_config 收到注入值） |
| `app/integration/tools/tool_service.py` | 构造 `external_config_source` 透传 loader | 现有构造测试覆盖（默认 None 兼容） |
| `app/container.py` | 绑定 `external_config_source=lambda key: getattr(settings, key, None)` | `tests/unit/test_container.py` |
| `app/config/settings.py` | `tool_http_timeout: float = 15.0` | `tests/unit/test_settings.py` |
| `app/integration/tools/external/http_api.py` | `CONFIG_KEYS` + `register_config` + `_client_timeout` 替换硬编码 | `tests/unit/test_http_api_tool.py` 新增 `test_register_config_injects_timeout`（注入值进连接层超时） |
| 文档 | [external.md](../../../docs/integration_doc/tools_doc/external.md)（配置注入契约 + 测试状态）；[config.md](../../../docs/config_doc/config.md)（`TOOL_HTTP_TIMEOUT`）；`.env.example` | — |

---

## 验证

- 相关测试 **74 passed**（loader + http_api + settings + container）
- 全量测试待提交前确认（增量改动：loader 注入点 + 示例工具，无回归面）
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **自动加载组件也需配置注入点**：外部工具无装配根，`register_config` 必须由 loader 兜底调用——否则「注入风格」契约内外漂移，配置被静默忽略。
- **声明式配置需求优于自行读取**：插件声明 `CONFIG_KEYS`（manifest 思想），宿主注入——插件不依赖 settings 具体结构，宿主统一配置入口。
- **向后兼容注入**：无声明 / 无源 / 无实现三分支跳过，老插件不破——注入点是增强不是破坏。
