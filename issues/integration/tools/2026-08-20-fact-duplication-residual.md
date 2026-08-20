# TOOLS-047 工具模块文档事实重复收敛遗漏（3 处，违反「一个事实一个家」）

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（文档规范，次要项）
> **来源**：2026-08-20 工具模块文档↔代码状态审核（P1 A/B/C）
> **涉及模块**：`docs/integration_doc/tools_doc/`（tools / executor / external / builtin）
> **关联文档**：[tools.md](../../../docs/integration_doc/tools_doc/tools.md) · [executor.md](../../../docs/integration_doc/tools_doc/executor.md) · [external.md](../../../docs/integration_doc/tools_doc/external.md) · [builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md)

---

## 问题描述

### 现象

TOOLS-046 收敛后仍遗留 3 处「同一事实多文档重复描述」：

| # | 重复位置 | 内容 | 家 |
| --- | --- | --- | --- |
| A | tools.md 内置工具元数据总览表 ↔ builtin.md 各工具详解表 | 10 工具的风险级 / 分类 / 并发安全 / 默认超时整表重复 | builtin.md |
| B | tools.md ErrorCode 六码 ↔ executor.md 失败路径→错误码映射 | 未注册→NOT_REGISTERED / … / UNKNOWN 六码映射逐字重复 | tools.md |
| C | external.md 约定 1 ↔ builtin.md「开发新工具要点」 | 10 个内置工具注册名枚举重复 | builtin.md |

### 影响

同一元数据（工具超时 / 风险级 / 注册名清单）改一处忘另一处 → 文档漂移。tools.md 已声明「执行细节见子文档，本文不重复」，但内置工具元数据表仍复述组件细节，违反 module_doc.md Rule 1「不搬子文档内容」；executor.md 末尾已链接 tools.md 但仍重述六码映射。

### 根因

TOOLS-046 收敛聚焦「方法表 / 配置表 / 机制详述」三类形态，遗漏了「组件元数据表（A）/ 映射复述（B）/ 清单枚举（C）」三种同源形态。

---

## 修复方案

按「一个事实一个家」收敛——各留一处 + 其他文档只链接：

- **A**：tools.md 内置工具节收敛为「工具 ↔ 注册名 + 一句话职责」清单（接口导航），删风险级 / 分类 / 并发安全 / 默认超时列，链接 builtin.md（各工具实现细节）与 rca.md（RCA 场景契约）
- **B**：executor.md 错误码段删六码映射，保留 executor 特有语义（工具业务失败透传业务码默认 `None`）+ 链接 tools.md `ErrorCode`
- **C**：external.md 约定 1 删工具名枚举，链接 builtin.md「开发新工具要点」（注册名清单权威所在）

## 实施记录

| 文件 | 改动 |
| --- | --- |
| `docs/integration_doc/tools_doc/tools.md` | 内置工具元数据表（风险级/分类/并发安全/默认超时）收敛为清单 + 一句话职责，元数据链接 builtin.md / rca.md；顺带清理「配置关联」节前连续空行 |
| `docs/integration_doc/tools_doc/executor.md` | 错误码段删六码映射，改「六码定义见 tools.md ErrorCode」 |
| `docs/integration_doc/tools_doc/external.md` | 约定 1 删 10 工具名枚举，改链接 builtin.md「开发新工具要点」 |

## 验证

- `scripts/verify_alignment.py`：ALIGNMENT 校验通过

## 教训沉淀

- **收敛需覆盖全部复述形态**：事实重复不止「方法表 / 配置表 / 机制详述」，还有「组件元数据表（A）/ 映射复述（B）/ 清单枚举（C）」——修复时先 grep 出同一事实的全部出现点，避免收敛一处遗漏他处。
- **接口文档的工具清单只做导航**：工具注册名属对外契约可列，但风险级 / 超时等组件元数据属 builtin.md，接口文档只给「清单 + 职责 + 链接」。
