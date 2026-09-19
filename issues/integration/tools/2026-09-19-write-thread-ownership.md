# TOOLS-057：写文件未登记线程导致资源提前释放

日期：2026-09-19。优先级：P2。状态：已修复。来源：用户复审 P2-1 及真实文件竞态复现。
范围：WriteFileTool 适配器，进程内线程所有权及资源清理；不代表交付 B 完成。

## 现象与根因

WriteFileTool 未覆写 invoke；建目录走 asyncio.to_thread，打开/写入/关闭走 aiofiles，均未登记到 AttemptHandle。真实 ToolService（容量 1）探针延迟第一次 open 后取消调用，线程仍在运行，但 completed=True、active=0、串行锁已释放、事实 cleanup=COMPLETE。第二次写同一临时文件成功写入 second，放行第一次 open(w) 后文件变空。

Supervisor 只能等待受控登记的线程。已取消的协程结束不证明默认线程池任务结束；遗漏工具线程导致误报完成并归还容量/串行锁，增加清理等待时间不能修复这一根因。

## 方案与实施

实施前核对 [Python Future.cancel](https://docs.python.org/3/library/concurrent.futures.html#concurrent.futures.Future.cancel) 与 [aiofiles 官方实现说明](https://github.com/Tinche/aiofiles)：运行中线程不能靠取消等待停止；aiofiles 通过线程池执行文件 I/O。

沿现有 ReadFileTool 方案，将 mkdir/open/write/close 合并到一个同步 _write_sync，受控 invoke 委托 execution.run_sync，显式声明 MAY_WRITE。execute 保留独立适配器入口，不作为正式网关替代。

## 验证

先失败：7 failed / 1 passed，取消/单次超时/硬取消及 mkdir/open 组合均出现提前 completed，另证实能力声明缺失。修复后扩展到 mkdir/open/write/close 四阶段及三种终止；用真实线程、文件、Supervisor 和 Permit 验证转移时不退容量、不释放串行锁、第二次同文件写等待、迟回值保留、文件关闭和最终内容。写测试是隔离适配器验证，没有把写操作伪声明只读绕过门禁。

全量 1398 passed、1 条既有第三方弃用警告；独立复审未发现阻断问题。最终检查见 [W-01 交接](../../../docs/history/completed-work.md)。

## 边界与经验

已开始同步工作仍可能在取消后写入，当前不会杀线程，不承诺撤销、原子写入、跨工具资源锁或崩溃恢复。可信适配器必须登记完整 I/O 生命周期，登记外层协程不足以证明真实资源释放。

正式契约：[执行与接管](../../../docs/integration_doc/tools_doc/execution.md)。启用边界另见 [TOOLS-058](2026-09-19-unprotected-tool-enablement.md)。
