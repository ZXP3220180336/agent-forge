# TOOLS-045 工具模块文档演进叙事残留（4 处）

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（文档规范，次要项）
> **来源**：2026-08-20 工具模块文档↔代码状态审核（B 类 #1-#4）
> **涉及模块**：`docs/integration_doc/tools_doc/`（result_processor / builtin / external）
> **关联文档**：[result_processor.md](../../../docs/integration_doc/tools_doc/result_processor.md) · [builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md) · [external.md](../../../docs/integration_doc/tools_doc/external.md)

---

## 问题描述

### 现象

三处文档含「演进叙事」表述，违反硬性规范「写当前状态，不写历史」（禁止 previously / 不再 / 已改为 / TOOLS-XXX 前 等）：

| # | 位置 | 表述 |
| --- | --- | --- |
| B1 | result_processor.md:29 | 「内置工具**不再**各自内联截断」 |
| B2 | builtin.md:341 | 「`communicate()` 全量读**已废弃，改为** `_read_stream_capped`」 |
| B3 | external.md:111 | 「**TOOLS-010 前为**『自行读环境变量』」 |
| B4 | external.md:112 | 「**TOOLS-013 修复**『兄弟模块缓存残留』」 |

### 影响

演进叙事双处维护（历史归 git / issue / ADR，文档只写当前状态），随迭代产生「修复前」残留导致文档漂移。

### 根因

TOOLS-005/013/021 等修复后，文档以「修复前后对比」方式描述机制，未收敛为当前状态。

---

## 修复方案

B 类 4 处全部改为当前状态描述：result_processor 截断收敛单点、`_read_stream_capped` 流式读、外部工具经 `CONFIG_KEYS` 注入、兄弟模块随工具文件清理。问题历史归对应 issue，文档不承载。

## 实施记录

| 文件 | 改动 |
| --- | --- |
| `docs/integration_doc/tools_doc/result_processor.md` | 去「不再」，改「内置工具截断统一收敛到本组件单点」 |
| `docs/integration_doc/tools_doc/builtin_doc/builtin.md` | 去「已废弃，改为」，直接描述 `_read_stream_capped` 流式读 |
| `docs/integration_doc/tools_doc/external.md` | 删「TOOLS-010 前为『自行读环境变量』」与「TOOLS-013 修复『兄弟模块缓存残留』」括号 |

## 验证

- `scripts/verify_alignment.py`：ALIGNMENT 校验通过；全量 **542 passed**

## 教训沉淀

- **改代码后 grep 演进叙事词**：修完功能后对涉及文档 grep「不再 / 已废弃 / 改为 / 修复前 / 前为」等词，识别并清理（本类 4 处均为历史修复残留）。
- **文档只写当前状态**：修复的因果、前后对比归 issue / ADR，文档正文只描述现在是什么。
