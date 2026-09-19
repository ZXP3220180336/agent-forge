# TOOLS-056：多工具插件卸载期间仍可启动兄弟工具

日期：2026-09-19；状态：已修复；优先级：P2；来源：工作区审查探针。
范围：ExternalToolLoader 文件级卸载。

## 现象与根因

文件含 A/B 两个工具，活动检查通过后只注销 A 就等待其 `on_unload()`。手动刷新与 TTL 内调用并发时，B 仍可被取得并开始执行；A 卸载完成后 B 被直接卸载，可能关闭其在用资源。逐实例无 await 不能保证整个文件的检查与注销一致。

## 方案与实施

捕获该文件全部实例并检查活动状态，在任何 await 之前同步注销全部实例，再逐个执行卸载钩子。沿用现有单事件循环边界，无需增加锁或状态抽象。

参照 [Python Task 协作调度](https://docs.python.org/3/library/asyncio-task.html#task-object)：其他任务在 await 让出控制时运行。采用无 await 的文件级状态转换，把异步资源清理放在入口关闭之后。

## 验证与教训

`test_file_unload_blocks_sibling_calls_before_first_cleanup` 通过 Event 挂起 A 的卸载，在 TTL 内调用 B。修复前 B 执行成功导致断言失败；修复后返回 NOT_REGISTERED。完整加载器测试 29 项通过。

教训：批量生命周期操作的检查范围必须与同步提交范围相同。
契约见 [external](../../../docs/integration_doc/tools_doc/external.md)，适用 G0-2/G0-3。
