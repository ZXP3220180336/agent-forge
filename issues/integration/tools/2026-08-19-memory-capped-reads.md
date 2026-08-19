# TOOLS-004 工具整文件/整响应读入内存，截断发生在读取之后

> **状态**：✅ 已修复（2026-08-19）
> **优先级**：P1（性能 / 资源边界）
> **来源**：2026-08-18 工具模块代码审核（builtin 通用工具组 · 重要项 2）
> **涉及模块**：`app/integration/tools/builtin/`（file_ops.py / code_exec.py / web_browse.py）
> **关联文档**：[builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

---

## 问题描述

### 现象

`readFile` 全量 `file.read()`、`code_exec` 的 `communicate()` 全量读子进程输出、`web_browse` 的 `response.text` 全量载入响应体——三者都是「先全量读入内存，再交给 ResultProcessor 事后 head+tail 截断」。`max_output_length` 只限制最终持有量，不限制读取阶段的瞬态峰值。

### 影响

RCA 场景读几 MB ~ 几百 MB 数据文件 / 命令 cat 大日志 / 抓取大网页时，并发多 Agent 下产生大瞬态内存峰值，甚至 OOM。

### 根因

截断策略（head+tail）在 ResultProcessor 侧，但读取动作在工具侧且无读取量限制——「读多少」与「保留多少」脱节。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| 流式 IO 原则（Python `StreamReader`） | 分块读取、累计到上限即停；管道读满前必须持续 drain 防写端阻塞 |
| 大文件 head+tail | 文件可 seek：读头段 + seek 到尾部读尾段，避免全量 |
| HTTP 流式 body（httpx `aiter_bytes`） | Content-Length 不可信（可伪造 / chunked），流式迭代 + 解析器增量 feed，超限即停 |

**核心**：读取阶段即限制（分段 / 流式 / drain），而非读取后截断；截断标记仍由 ResultProcessor 统一生成，避免双重截断。

---

## 修复方案（含决策取舍）

| 工具 | 改动 |
| --- | --- |
| `file_ops.readFile` | `os.path.getsize` 预检；超阈值（单段 `max_output_length×3` 字节 × 2）时二进制 seek 分段读 head+tail（保留首尾），正常大小仍全读；`decode("utf-8", errors="replace")` |
| `code_exec` | 新增 `_read_stream_capped`：分块读 stdout/stderr 保留前 `max_output_length×3` 字节，超出丢弃并**继续 drain**（防子进程管道阻塞）；`communicate()` 全量读废弃，流式读后 `await proc.wait()` 填充 returncode |
| `web_browse` | `client.stream` + `aiter_bytes` 流式读 body，`_HTMLToTextParser.feed` 增量解析，累计超 `max_content_length×4` 字节即停；body 过大 content 追加提示、metadata 置 `truncated` |

**取舍理由**：

1. **分段 / 流式读保持「截断契约在 ResultProcessor」**：工具层只限制读取量，head+tail 截断标记仍统一由 ResultProcessor 生成，避免双重截断（ResultProcessor docstring 明确「内置工具不再各自内联截断」）；
2. **code_exec 丢弃尾部保留头部**：管道不可 seek，无法 head+tail；保留头部（命令输出 / 编译错误开头）并 drain 完整输出保证进程正常结束、returncode 有效；
3. **web_browse 增量 feed**：HTML 解析器支持增量 feed，无需完整 body 即可解析，超限即停并关闭流。

**语义边界**：

- 正常大小文件 / 输出 / 网页行为不变（读取量 ≤ 阈值）；
- file_ops 分段保留 head+tail；code_exec 保留 head（丢弃尾部）；web_browse 保留 head——各按介质能力取舍，均限制了读取峰值；
- web_browse 编码改为按 `response.charset_encoding`（无则 utf-8）`errors="replace"` 逐 chunk 解码（原先 `response.text` 自动解码）。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/integration/tools/builtin/file_ops.py` | `getsize` 预检 + 二进制分段 head+tail 读取 + 手动 decode | `test_tool_execution.py` 新增 `test_read_file_large_chunked_head_tail`（70 万字符大文件分段，保留首尾、中间丢弃、读取量受限） |
| `app/integration/tools/builtin/code_exec.py` | `_read_stream_capped` 流式读 + drain；`communicate()` → gather 流式读 + `proc.wait()`；清理路径 `communicate` → `wait` | 新增 `test_code_exec_output_capped`（fake 40 万字节输出，截断到 cap）；`_FakeSubprocessProc` 适配流式接口（stdout/stderr stream + wait） |
| `app/integration/tools/builtin/web_browse.py` | `client.get` → `client.stream` + `aiter_bytes` 增量 feed；body 超限标记 | 现有测试全通过（无 web_browse 内容断言测试受影响） |
| 文档 | [builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md) 三工具实现要点补读取量限制说明 | — |

---

## 验证

- 全量测试 **500 passed**（64.33s，含 2 个新增读取量限制用例），无回归
- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

---

## 教训沉淀

- **读取量限制在读取阶段，截断契约在 ResultProcessor**：工具管「读多少」（分段 / 流式 / drain），ResultProcessor 管「留多少」（head+tail + marker）——职责分离避免双重截断与内存峰值并存。
- **管道无 seek，head+tail 不适用**：子进程输出只能流式读；保留头部 + 持续 drain（防写端阻塞）是正解，丢弃尾部需文档声明。
- **HTTP body 不可信 Content-Length**：必须流式迭代读取，解析器增量 feed 天然适配「读一段解析一段」。
