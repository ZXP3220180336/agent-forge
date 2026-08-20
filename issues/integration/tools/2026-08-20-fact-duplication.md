# TOOLS-046 工具模块文档事实重复（5 处，违反「一个事实一个家」）

> **状态**：✅ 已修复（2026-08-20）
> **优先级**：P3（文档规范，次要项）
> **来源**：2026-08-20 工具模块文档↔代码状态审核（C 类 #1-#5）
> **涉及模块**：`docs/integration_doc/tools_doc/`（tools / tool_service / builtin / rca / external）
> **关联文档**：[tools.md](../../../docs/integration_doc/tools_doc/tools.md) · [tool_service.md](../../../docs/integration_doc/tools_doc/tool_service.md) · [builtin.md](../../../docs/integration_doc/tools_doc/builtin_doc/builtin.md) · [rca.md](../../../docs/integration_doc/tools_doc/builtin_doc/rca.md) · [external.md](../../../docs/integration_doc/tools_doc/external.md)

---

## 问题描述

### 现象

同一事实在多处文档重复详细描述（而非只链接），违反硬性规范「一个事实一个家」，双处维护必然漂移：

| # | 重复位置 | 内容 | 家 |
| --- | --- | --- | --- |
| C1 | tools.md:83-97 ↔ tool_service.md:69-85 | ToolService 方法签名表整表重复（13 行） | tool_service.md |
| C2 | tools.md:160-171 ↔ tool_service.md:175-183 | 配置关联表整表重复（5 项） | config.md |
| C3 | tools.md:150-158 ↔ tool_service.md:113-121 | 外部工具惰性检查机制完整详述 | external.md |
| C4 | builtin.md:414 ↔ rca.md:51 | search_historical_rca 关键词匹配/RAG 增强复述 | rca.md |
| C5 | external.md:115 | 约定 6 复述 TOOLS-014 死锁机制细节 | TOOLS-014 issue |

### 影响

同一机制改一处忘另一处 → 文档漂移（本次审核已证：C 类与 A 类并存，说明复述确实已造成不一致）。

### 根因

文档编写时对「总览文档应导航而非复述」执行不严，多处复制粘贴。

---

## 修复方案

按「一个事实一个家」收敛——各留一处 + 其他文档只链接：

- **C1**：tools.md 方法签名表删除，改为链接 `tool_service.md#toolservice-方法`（保留 ToolGateway 协议 / ToolResult / BaseTool 契约）
- **C2**：tools.md 与 tool_service.md 配置表均删，改为引导「配置项见 config 文档」（tool_service.md 保留配置消费语义说明行）
- **C3**：tools.md 外部工具节压缩为一句 + 链接 external.md；tool_service.md 保留 Facade 视角要点（execute 调用 / 自动获得横切关注点 / 手动重扫），删机制详述；**顺带修正** A 类漏网过时表述「外部工具自行读环境变量配置」→ 配置注入
- **C4**：builtin.md 删关键词匹配复述，只留「详见 rca.md」
- **C5**：external.md 约定 6 删死锁机制细节，只声明约束 + 链接 TOOLS-014

## 实施记录

| 文件 | 改动 |
| --- | --- |
| `docs/integration_doc/tools_doc/tools.md` | 删 ToolService 方法表→链接；删配置表→链接 config.md；外部工具节压缩为一句 + 链接 external.md |
| `docs/integration_doc/tools_doc/tool_service.md` | 删配置表→链接 config.md；外部工具热加载节压缩为 Facade 视角要点 |
| `docs/integration_doc/tools_doc/builtin_doc/builtin.md` | 删 search_historical_rca 关键词匹配复述 |
| `docs/integration_doc/tools_doc/external.md` | 约定 6 删死锁机制细节，只声明约束 + 链接 TOOLS-014 |

## 验证

- `scripts/verify_alignment.py`：ALIGNMENT 校验通过（含新增锚点链接 `#toolservice-方法`）；全量 **542 passed**

## 教训沉淀

- **总览文档只导航不复述**：tools.md 是接口契约 + 导航，子模块机制（方法表 / 配置 / 热加载）一律链接「家」文档。
- **删除前确认信息落点**：C2 删配置表前核实 config.md 已覆盖全部键（TOOL_TIMEOUT / TOOL_ALLOWED_DIRS / TAVILY_* 等），信息不失真。
- **复述是漂移之源**：双处维护的机制必然不一致（本次审核 A 类多处即由复述未同步造成）。
