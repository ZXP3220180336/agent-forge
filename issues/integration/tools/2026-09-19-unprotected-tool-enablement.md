# TOOLS-058：交付 B 未就绪能力仍被导出和执行

日期：2026-09-19。优先级：P2。状态：已修复。来源：TOOLS-057 的 A/B 契约核查及用户确认。
范围：ToolService 模型导出、ToolExecutor 正式调用；沿既有 ADR-008 启用条件补齐，不建设 B。

## 现象与根因

注册容器、模型可用列表和正式可执行集合未区分。ADR 已将写入、未知效果或强制审计操作列为 B，并要求持久保护后启用，旧导出仍全量注册项，Executor 取得声明后直接进入容量准入，没有检查 B 前提。

## 方案与实施

沿 [ADR-008](../../../adr/integration/tools/2026-09-13-tool-execution-lifecycle.md) 已接受的分期条件，复用 ToolExecutionSpec，新增纯判断 is_execution_enabled，不引入配置绕过或新状态类。当前仅枚举 READ_ONLY 且 audit_required is False 获准。

ToolService 两种模型协议先过滤，再选择/导出；空参数下无法确定能力的工具保守隐藏，实际直接调用按实参判断。Executor 在参数校验后审批前、每次 attempt 取得 Permit 前及拿锁后复核。拒绝返回 REJECTED；未开始事实为 NOT_STARTED/NONE/NOT_NEEDED，不计新业务尝试，不丢旧 attempt 事实。等待期间已取得的锁/Permit 正常归还。

工业级依据沿原 ADR 的独立准入与尝试阶段；线程完成边界参照 [Python Future](https://docs.python.org/3/library/concurrent.futures.html#future-objects)，适配器登记修复见 [TOOLS-057](2026-09-19-write-thread-ownership.md)。审批/风险等级/健康检查不能代替能力就绪。

## 验证

先 13 failed，审批和真实执行未被阻止、模型仍导出未就绪能力；修复并覆盖参数优先级、注册后/审批中/重试时声明变化后 15 passed。追加串行锁等待期间声明变化测试先 1 failed，再补真实 invoke 前复核，门禁套件 16 passed。

内置工具集成验证注册 10 个、模型可用 8 个只读工具；writeFile/code_exec 直接点名拒绝，零 invoke、零文件创建。无副作用假工具显式声明只读，原命令/写入功能在独立适配器边界验证；聊天端到端使用真实 readFile。低层结果解释仍覆盖 PARTIAL/UNKNOWN，不因只读门禁删除事实语义测试。

全量 1398 passed、1 条既有第三方弃用警告；独立复审未发现阻断问题。最终检查见 [W-01](../../../docs/todo.md)。

## 边界与经验

注册/导入及生命周期钩子仍运行可信 Python 代码，门禁不是插件沙箱。直接调用适配器 execute 不经过此门禁，但不是获准的正式启用方式；无实参时无法判定的能力不保证模型可发现。持久账本、资源联合准入、恢复和正式副作用启用仍归 B。

正式契约仅维护于[当前启用边界](../../../docs/integration_doc/tools_doc/execution.md#当前启用边界)。规划中的未来保护不能豁免当前可达路径；未就绪能力必须由真实准入关闭。
