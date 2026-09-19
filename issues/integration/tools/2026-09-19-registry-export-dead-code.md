# TOOLS-060：ToolRegistry 保留绕过启用过滤的 Schema 导出

日期：2026-09-19。优先级：P3。状态：已修复。来源：W-03 遗留清单第 8 项复核。
范围：`app/integration/tools/registry.py` 导出方法；不改变 Facade 的两条导出语义。

## 现象与根因

容器拆分（2026-08-15）时把 `get_openai_tools` / `get_openai_responses` 复制进 `ToolRegistry`，两者当时是
ToolService 的委托目标。TOOLS-058 为关闭未就绪能力，把 Facade 两条导出改为基于 `_enabled_tools()` 自产，
委托链断开，容器副本从此零生产者：`get_openai_responses` 无任何调用方，`get_openai_tools` 仅注册中心测试
直接使用。

遗留问题的实质不是「冗余」而是风险——它是一个不做启用过滤的全量导出路径，正是 TOOLS-058 关闭的那条；
接上它即让 writeFile / code_exec 对模型可见。六组件对齐 ADR 第 22 行「get_openai_responses 全量」记录的
是当时的委托机制，已随该修复失效，而 Facade 的全量语义未变。

## 方案与取舍

删除容器副本，Schema 导出统一由 Facade 独占。核对确认 `ToolRegistry` 未进入 `tools/__init__.py` 的
`__all__`，仓库内没有其他消费者；保留副本只是保留一条可绕过门禁的路径，不构成「保持全量转储」的额外收益。
本次不是决策反转，故在 ADR 原文下加修订标注而非改写历史条款。

## 实施与验证

删除两个方法与随之无用的 `typing.Any` 导入，模块与类说明去掉导出职责。批导出测试改用
`all_tools()` + `BaseTool.to_openai_tool()`，仍锁定「单个非法定义不影响同批导出」。

定向 `test_tool_registry_metadata.py` / `test_tool_selector.py` / `test_tool_execution.py` / `test_tool_enablement.py`
95 passed；全量 1431 passed（1 条既有 Starlette/httpx 弃用提示），与改动前一致；`scripts.verify_alignment` 与
`git diff --check` 通过。

## 边界与经验

仓库外若有调用方会暴露为 `AttributeError`；本项目不是对外发布的 SDK，不保留兼容壳。容器不导出也不等于
插件沙箱——注册与导入仍执行可信 Python 代码。

与 Facade 同名的方法只应保留一个正式位置：拆分时复制出的第二条路径会绕过后来加在 Facade 上的门禁。

正式契约见 [注册中心](../../../docs/integration_doc/tools_doc/registry.md)；启用边界见
[当前启用边界](../../../docs/integration_doc/tools_doc/execution.md#当前启用边界)。

## 关联记录

- [工具模块六组件对齐 ADR](../../../adr/integration/tools/2026-08-17-six-component-alignment.md)
- [TOOLS-058 未就绪能力启用边界](2026-09-19-unprotected-tool-enablement.md)
