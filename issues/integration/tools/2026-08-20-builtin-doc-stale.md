# TOOLS-040 builtin.md 文档状态与代码不符（8 处过时）

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（文档同步，次要项）
> **来源**：2026-08-20 工具模块文档↔代码状态审核（A 类 #2 / #7 / #8 / #9 / #10 / #13 / #14 / #15）
> **涉及模块**：`docs/integration_doc/tools_doc/builtin_doc/builtin.md`
> **关联文档**：[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

---

## 问题描述

### 现象

builtin.md 存在 8 处文档描述与代码当前状态不符：

| # | 位置 | 文档写 | 代码实际 |
| --- | --- | --- | --- |
| 1 | 自动发现机制 | `builtin/__init__.py`（**62 行**） | 67 行（冲突告警逻辑新增后未同步行数） |
| 2 | 发现流程代码块 | 收录循环体仅 `_tool_classes[name] = obj` | 含**类名冲突告警分支**（`builtin/__init__.py:43-47`） |
| 3 | readFile 执行方式 | `aiofiles.open(path, encoding="utf-8")` | `"rb"` 二进制 + `decode_output` 双解码（`file_ops.py:114-121`） |
| 4 | code_exec 代码块 | 仍展示 `proc.communicate()` 全量读 | `_read_stream_capped` 流式读 + `await proc.wait()`（`code_exec.py:162-170`） |
| 5 | code_exec 超时路径 | 超时「回收后**重抛**」 | 内部超时（`TimeoutError`）直接返回失败结果，仅 `CancelledError` 重抛（`code_exec.py:171-178`） |
| 6 | web_browse 客户端代码块 | 硬编码 `max_redirects=5, timeout=15.0, _ssrf_on_request` | 类属性注入（`WebBrowseTool._max_redirects/_timeout`）+ `ssrf_on_request`（`web_browse.py:153-165`） |
| 7 | web_browse 异常分类 | 漏 SSRFError 分支 | `SSRFError → "请求被安全策略拦截（SSRF 防护）"`（`web_browse.py:327-332`） |
| 8 | RCA 证据链 | 「每个工具带 source/查询键/timestamp」 | `search_historical_rca` 无 timestamp（见 TOOLS-039） |

### 影响

文档示例代码与实现不符会误导新工具开发者照抄旧写法；超时路径描述错误影响对资源回收语义的判断。

### 根因

TOOLS-004（流式读）/ TOOLS-005（编码回退）/ TOOLS-011（SSRF 共享）/ TOOLS-023（web_browse 注入）/ TOOLS-027（冲突告警）等修复后，builtin.md 对应节未逐一同步；代码块以「示意」形式存在，未与实现 diff。

---

## 修复方案

按「以实际代码为准」逐处对齐：更新行数、补冲突告警分支与注意事项、readFile 双解码表述、code_exec 流式读代码块、超时/取消双路径区分、web_browse 客户端代码块（类属性注入 + 共享 SSRF 钩子）、异常分类补 SSRFError、RCA 证据链字段修正。

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `docs/integration_doc/tools_doc/builtin_doc/builtin.md` | 上述 8 处同步 | 无（纯文档；code_exec/readFile/web_browse 行为既有测试覆盖） |

## 验证

- 全量 **542 passed**
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

## 教训沉淀

- **文档代码块必须与实现 diff**：示例代码块（尤其 execute 主体）极易随重构过时，宜在代码块旁标注「见 `xxx.py`」并复核关键行。
- **异常分类清单要枚举全部分支**：漏列会让人误以为某异常路径不存在。
