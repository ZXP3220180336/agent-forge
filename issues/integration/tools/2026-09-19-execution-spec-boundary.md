# TOOLS-059：执行声明缺少适配器边界防护

日期：2026-09-19。优先级：P2。状态：已修复。来源：用户缺陷报告。
范围：声明读取；不改变重试与交付 B 的启用条件。

## 现象与根因

三个执行检查点直接信任 describe_execution，前两处异常可逃逸；第三处非法返回覆盖闭包 spec 后，完成回调再次访问属性失败，污染 run_stop。第三处仅抛异常时赋值尚未发生，不能混同为必然触发回调错误。导出仅捕获调用异常，非法返回仍中断导出；鸭子类型对象还可误过门禁。

## 实施与边界

共享 read_execution_spec 捕获普通 Exception 并校验 isinstance，失败返回默认 ToolExecutionSpec（UNKNOWN）。三个执行入口和两种模型导出共用它；拒绝结果仍是 REJECTED，事实 NOT_STARTED/NONE/NOT_NEEDED。取消、截止时间、运行停止显式传播，不捕获 BaseException。

沿用现有执行契约，无新增状态或架构层。Python 官方说明 [CancelledError](https://docs.python.org/3/library/asyncio-exceptions.html#asyncio.CancelledError) 继承 BaseException 且通常应传播；本项目三个控制异常继承 Exception，因此另行排除。该隔离不是不可信 Python 插件沙箱。

## 验证

先 18 failed / 2 passed 复现普通异常、None、字典与伪对象；修复后覆盖三个检查点、同运行后续成功、资源释放、两种协议混合导出、错误签名及四类控制信号。最终回归见 [W-02](../../../docs/todo.md)。独立只读复审未发现阻断问题。

正式契约见 [执行声明边界](../../../docs/integration_doc/tools_doc/execution.md#当前启用边界)。
