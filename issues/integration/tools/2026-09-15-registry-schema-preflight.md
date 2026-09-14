# TOOLS-051 参数 Schema 定义错误缺少注册期拦截

日期：2026-09-15。优先级：P2。状态：已修复。来源：S-01 Schema 方言统一改动的独立代码审查（探针复现）。
范围：`app/integration/tools/registry.py` 注册入口；症状波及 `get_openai_tools` 消费方与参数校验归因。

## 现象与根因

方言统一后，`BaseTool.to_openai_tool` / `to_openai_response` 会在导出时预检 `parameters`，非法即抛 `SchemaError`。但检测点在运行期（导出、参数校验），隔离能力却在注册期：

- `ToolRegistry.register` / `ToolService.register` 只查重名；
- `ToolAssembler.assemble` 已有逐工具兜底（单工具注册失败不影响启动）；
- `ExternalToolLoader` 已有文件级回滚（任一实例化 / `on_load` / 注册失败 → 回滚本文件）。

检测点与隔离点错位，同一根因产生两个后果：

1. **同批全部工具失效**：`get_openai_tools` 是列表推导式，一个工具定义非法即整批抛错。调用点是每次 ReAct run（`react.py` 的 `execute` 入口）与每个规划步骤的工具目录，表现为原始 jsonschema 文本进入 SSE 错误事件。外部工具是用户手写插件，声明旧方言（`draft-07` 头部）属常见写法，一个既有插件即可让全部会话的 Agent 运行失败。
2. **归因错位**：参数校验把定义非法转成 `ValidationIssue`，executor 归为 `ErrorCode.VALIDATION`（语义是模型参数错）。模型无法修正工具自身定义，每轮回喂同一条错误直到重试预算耗尽；证据链会把工具定义缺陷记成模型参数缺陷。

## 方案与实施

预检收敛到唯一注册入口，不新增逐调用方守卫——两个调用方已有的兜底正好是期望的隔离粒度：内置装配跳过单个工具，外部加载回滚整个文件。

- `registry.py`：`register` 在重名检查后调用 `create_schema_validator(tool.parameters)`，非法抛 `SchemaError`，工具不入容器。
- 文档：`registry.md` 补注册契约与预检职责、测试状态；`external.md` 补插件参数 Schema 契约与边界；`tools.md` 说明执行期 `VALIDATION` 出口的适用范围；ALIGNMENT 登记 `registry.py` 职责变化。
- 未改动：参数校验的 `ValidationIssue` 归因保持不变（ADR 既定），它现在只覆盖定义在注册后失效的情形。

## 验证与经验

探针（修复前）：注册 draft-07 插件成功 → `get_openai_tools()` 抛 `SchemaError`，同批合法工具一并失效；修复后注册即被拒，合法工具导出正常。

- `test_tool_registry_metadata.py` 新增 2 项：定义非法注册抛 `SchemaError` 且不入容器、单个非法定义不影响同批导出。
- `test_tool_validator.py` 的「非法 Schema 零执行」用例改为「注册后定义被改坏」形态，保留执行链失败关闭（`VALIDATION` + 零副作用）的覆盖。
- 全量 1166 passed；`verify_alignment` 与 `git diff --check` 通过。

可复用教训：定义 / 配置类错误的检测点必须与具备隔离能力的边界对齐。把校验放在隔离能力之外，会把单点配置错误放大成整批或整服务故障；而把校验放回唯一入口，不需要为每个调用方各加一层守卫。

正式契约见[注册中心说明](../../../docs/integration_doc/tools_doc/registry.md)与[本地 Schema 契约](../../../docs/shared_doc/json_schema.md)，方言决策见 [ADR-004](../../../adr/2026-09-14-json-schema-dialect.md)。
