# AGENT-001 except A, B 逗号元组语法：PEP 758 语义 + 可移植性修复

> **状态**：✅ 已修复（2026-08-17）
> **优先级**：P2（风格 / 可移植性，非运行时 bug）
> **来源**：2026-08-17 工具模块重构影响面调查（Explore 扫描发现）
> **涉及模块**：`app/domain/agent/executor.py`（`_execute_tool_calls`）
> **关联文档**：[agent.md](../../../docs/domain_doc/agent_doc/agent.md)

---

## 问题描述

### 现象

`app/domain/agent/executor.py:210` 为 `except json.JSONDecodeError, KeyError:`——这是 **Python 2 的逗号绑定语法**（捕获第一个异常并绑定为第二个名字），Python 3 早期版本（<3.14）下是 `SyntaxError`。

### 影响

- 当前项目运行于 Python 3.14.6（**PEP 758 重新允许该语法**），实际语义为「元组捕获」`except (json.JSONDecodeError, KeyError):`（经实测验证），**运行时正确**；
- 但代码非惯用、可移植性差：`<3.14` 会直接 `SyntaxError`，无法降级运行；Python 2 时期语义是「绑定变量」，行为不同，易误导维护者；
- 同型语法另有 `app/main.py:27`（`except AttributeError, ValueError, OSError:`）与 `app/integration/llm/retry.py:595`（`except TypeError, ValueError:`）。

### 根因

沿用 Python 2 的逗号绑定语法书写多异常捕获，未用标准元组语法。

---

## 工业级参照

| 参照 | 做法 |
| --- | --- |
| CPython 3.14 PEP 758 | 重新允许 `except A, B:`，语义 = `except (A, B)` 元组捕获（不绑定变量）；但 `except A as e:` 绑定语义不变 |
| 项目代码规范 | 全部异常捕获用显式元组 `except (A, B):`（本项目其余代码一致） |

**核心**：多异常捕获统一写显式元组 `except (A, B):`，任何 3.x 均合法，语义明确。

---

## 修复方案（含决策取舍）

**决策**：`except json.JSONDecodeError, KeyError:` → `except (json.JSONDecodeError, KeyError):`，**精确保留当前 PEP 758 语义**（捕获两种异常 → `tool_args = {}`）。

**取舍理由**：

1. 显式元组在任何 3.x 合法，消除对 3.14 的隐性依赖；
2. 语义与当前运行行为完全一致（已实测），无行为变更；
3. `app/main.py:27`、`app/integration/llm/retry.py:595` 同型语法**不在本次工具模块重构范围**，仅记录，后续单独处理。

---

## 实施记录

| 文件 | 改动 | 回归测试 |
| --- | --- | --- |
| `app/domain/agent/executor.py` | `except json.JSONDecodeError, KeyError:` → `except (json.JSONDecodeError, KeyError):` | `tests/unit/test_agent.py`（既有） |

---

## 验证

- **语义实测**：Python 3.14.6 下 `except ValueError, KeyError:` 能同时捕获 ValueError 与 KeyError（元组语义）——修复前后行为一致；
- `tests/unit/test_agent.py` 全过（工具重构全量测试 410+ passed）；
- `uv run python -m scripts.verify_alignment` 通过。

---

## 教训沉淀

- **多异常捕获永远写显式元组** `except (A, B):`，不要用 Python 2 逗号语法——即便当前解释器（3.14 PEP 758）能跑，可移植性与可读性都不可接受；
- **语法扫描工具**可加入对 `except .*,\s` 的检查，拦截逗号绑定语法残留；
- 调查时发现「看似 bug」需实测语义再定性：PEP 758 下该语法语义正确（元组捕获），并非运行时缺陷，但仍是必须修复的风格/可移植性问题。
