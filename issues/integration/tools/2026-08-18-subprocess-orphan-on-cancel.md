# TOOLS-001 executor 超时取消后子进程孤儿泄漏

> **状态**：✅ 已修复（2026-08-18）
> **优先级**：P1（安全 / 资源边界）
> **来源**：2026-08-18 工具模块代码审核（builtin 通用工具组 · 重要项 1）
> **涉及模块**：`app/integration/tools/builtin/code_exec.py`（`CodeExecTool.execute`）
> **关联文档**：[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

---

## 问题描述

### 现象

`CodeExecTool.execute` 直接 `await proc.communicate()`。executor 外层 `asyncio.wait_for(tool.execute(...), timeout=...)` 超时取消的是执行协程，`communicate()` 内部对管道读取的 await 被取消，**子进程本身不会终止**——每次超时泄漏一个 `cmd` / `sh` 进程。

### 影响

多 Agent 引擎下长跑命令（`ping -t`、阻塞的编译）每次超时泄漏一个子进程，积累耗尽系统资源；命令执行属 L2 危险级工具，进程回收是安全底线。

### 根因

1. 工具内部无超时兜底——`communicate()` 不带 timeout，自声明 60s 超时仅作为元数据交给 executor，工具自身无护栏；
2. 取消 / 异常路径未清理子进程——`CancelledError` 传播时进程与管道（stdout/stderr）均残留。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| asyncio 官方 subprocess 文档 | `Process.communicate()` 的 asyncio 版不支持 `timeout` 参数，需用 `asyncio.wait_for` 包裹；**取消后应 `kill()` 进程，再调用一次 `communicate()` 关闭管道**（官方推荐清理模式） |
| 标准库 `subprocess.Popen.communicate(timeout=)` | 超时后自动 `kill` 子进程并等待回收——asyncio 版缺此语义，须手动实现等价行为 |
| 异步进程管理实践（asyncssh / aiofiles 生态） | 统一「超时 / 取消 → terminate/kill → 二次 wait/communicate 回收」模式，保证进程与句柄都释放 |

**核心**：资源型工具必须在自身取消点主动回收子进程与管道，不能依赖调用方清理。

---

## 修复方案（含决策取舍）

**决策**：`communicate()` 拆分为独立 try 块，三路径均先 `kill()` 再二次 `communicate()` 回收：

| 路径 | 处理 |
| --- | --- |
| 内部超时（`asyncio.wait_for(..., timeout=self.timeout)` 兜底 60s） | `kill()` + 二次 `communicate()` → 返回 `"命令执行超时（60 秒）"` |
| 外层 executor 超时取消（注入 `CancelledError`） | `kill()` + 二次 `communicate()` → **重抛**，交由 executor 归为 `ErrorCode.TIMEOUT` |
| `communicate` 阶段其它异常 | `kill()` + 二次 `communicate()` → 归因返回 |
| 创建阶段 `FileNotFoundError` | 单独捕获（无 proc 可清理），不再被宽泛 `except Exception` 吞并 |

**取舍理由**：

1. 对齐 asyncio 官方推荐模式（kill + 二次 communicate 关闭管道），既终止进程又回收文件句柄；
2. 内部兜底（`self.timeout`）与 executor 外层护栏双层防护——任一先到均不泄漏进程；
3. `CancelledError` 继承 `BaseException`，显式捕获后重抛保证 executor 统一分类、清理代码不被 `except Exception` 跳过。

**语义边界 / 已知局限**：

- 正常执行路径行为不变（无内部超时触发时与旧实现一致）；
- `create_subprocess_shell` 启动的是 shell 进程，`kill()` 只终止直接子进程；shell 模式下孙进程（如 `cmd /c python x.py` 里的 python）终止属平台固有限制（Windows `taskkill /T` / POSIX 进程组），列为后续增强。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/code_exec.py` | `communicate()` 拆独立 try：`asyncio.wait_for(..., timeout=self.timeout)` 兜底；`TimeoutError` / `CancelledError` / 其它异常三路径均 `kill()` + 二次 `communicate()` 回收（取消路径 kill 后重抛）；创建阶段 `FileNotFoundError` 单独捕获；`asyncio.TimeoutError` 统一为内置 `TimeoutError`（对齐 executor.py:225） | `tests/integration/test_tool_execution.py` 新增 `test_code_exec_timeout_kills_subprocess`（fake 子进程验证超时取消后 `kill()` 被调用 + executor 归 `ErrorCode.TIMEOUT`） |
| 文档 | [builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md) code_exec 实现要点补「超时 / 取消清理」一行（关联本问题文件） | — |

---

## 验证

- 相关测试（`test_tool_execution.py` + `test_tool_audit.py`）**17 passed**（含新增超时清理用例）
- 全量测试 **471 passed**（51.48s），无回归
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **资源型工具必须自持超时护栏**：executor 外层超时只取消协程、不回收资源；工具自身须在取消点主动清理（kill + 关闭管道）。
- **`CancelledError` 需显式捕获后重抛**：它继承 `BaseException`，不会被 `except Exception` 吞掉，但清理代码必须放在捕获分支里，且 re-raise 保证编排层统一分类。
- **asyncio 超时取消后清理可可靠执行**：`wait_for`（经 `timeouts.timeout.__aexit__`）先 `task.cancel()` 再 `await task` 等待任务结束——被取消协程内的清理代码（kill/二次 communicate）会执行完，测试可确定性断言 `killed is True`。
