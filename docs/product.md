# 产品定位与方向（产品决策文档）

> **决策时间**：2026-08-03
> **文档定位**：记录产品的方向决策、候选方向评估与里程碑。方向演进时在此更新，避免遗忘。
> **核心表述**：本项目 = **多 Agent 任务执行引擎** + 具体驱动场景。引擎是能力核心，场景决定工具与演示。

---

## 📋 目录

- [一句话定位](#一句话定位)
- [方向决策记录](#方向决策记录)
- [主方向：良率根因分析](#主方向良率根因分析)
- [备选方向：工艺知识助手](#备选方向工艺知识助手)
- [已关闭方向：EDA 设计辅助](#已关闭方向eda-设计辅助)
- [里程碑与闭环](#里程碑与闭环)
- [相关文档](#相关文档)

---

## 一句话定位

**多 Agent 任务执行引擎**：用户提交目标 → 系统异步受理 → 主 Agent 拆分 → 并行子 Agent 执行 → 汇总交付。当前驱动场景为**半导体良率异常根因分析（Yield RCA）**。

> 引擎与场景的关系：引擎（队列/调度/编排/状态）是长期资产，场景（工具/数据/提示词）是短期外壳。外壳可换，引擎沉淀。

---

## 方向决策记录

| 方向 | 决策 | 原因 |
| --- | --- | --- |
| 多 Agent 任务执行引擎（核心能力） | ✅ 认同 | 已铺好全部基建（task.md 规划、并发信号量、限流结算、多模型），引擎为长期资产 |
| 良率根因分析（Yield RCA，驱动场景） | ✅ 主方向 | 行业推进最热、与引擎契合度最高、可落地（模拟数据） |
| 工艺知识助手（工业 RAG） | 🔶 第二项目备选 | 与引擎关联弱（单 Agent 检索），但工程价值独立，作为后续项目 |
| EDA 设计辅助 | ❌ 关闭 | 结构性不可落地（见下文详细原因） |
| 通用调研/报告 | ❌ 弃用 | 无行业纵深，仅作技术验证外壳 |
| 智能客服 / 知识库 RAG | ❌ 弃用 | 同质化严重，与多 Agent 编排关联弱 |

---

## 主方向：良率根因分析

### 场景定义

良率工程师提交"某批次良率骤降（如 95%→82%），排查根因"，系统异步受理 → 主 Agent 拆分 → 子 Agent 并行排查 → 汇总带**证据链**的根因报告。

### 行业背景（2025-2026 拐点，调研来源）

- 麦肯锡：半导体制造 AI/ML 价值约 **40%** 集中在良率/质量类场景
- [晶合集成](http://news.ahwang.cn/life/20260326/2986554.html)：首次将 Agentic AI 引入前道晶圆厂，缺陷根因定位 **38h → 5.4min**，获 SEMICON China 2026 良率提升奖
- [智现未来 FabSyn-YES](https://www.semi.org.cn/site/semi/article/cef674f0792b424898e0b8a2502d69ed.html)：良率溯因 **4h → 1min**，缺陷识别准确率 96%，良率预测准确率 90%+
- [MongoDB 参考架构](https://www.mongodb.com/company/blog/innovation/how-mongodb-atlas-powers-agentic-ai-for-semiconductor-yield-optimization)：LangGraph ReAct Agent + 4 工具（查告警/查 wafer/查时序参数/检索历史 RCA）
- [ThirdAI](https://www.ibselectronics.hk/resources/news/thirdai-raises-$3m-seed-to-deploy-causal-ai-for-faster-root-cause-analysis-in-semiconductor-fabs/)：因果智能层，RCA 时间降 80%
- [StackAI](https://www.stackai.com/blog/the-top-ai-agent-use-cases-for-semiconductor-manufacturing-in-2026)：跨 5+ 系统（MES/SPC/缺陷/量测/设备遥测）数据孤岛是根因分析的核心痛点
- Gartner：预计 2028 年前该领域自动化达 15%

### 为什么契合引擎

根因分析的本质 = 多源关联 + 多步推理 + 并行排查，与「主从并行子 Agent 编排」（task.md 阶段 B）完全同构。MongoDB 的参考架构正是 ReAct Agent + 多工具链逐层推理。

### 工程亮点（区别于通用 demo）

- **证据链**：每个结论附数据来源，可审计（对标 FabYield-PM 的 provenance 要求）
- **置信度分级**：区分确定性事实与模型推断
- **显式放弃**：证据不足时明确"不能下结论 + 建议补充什么数据"，而非硬编结论
- **人机协同**：Agent 提议根因与下一步，工程师验证，不做自主设备控制

### 落地方式（不依赖真实 fab）

用**可复现的模拟数据生成器**：合成 wafer map + 设备告警 + FDC 时序参数 + 历史案例库。Agent 的价值在编排与推理，数据可合成、可测试、可演示。

### 工具清单（规划）

| 工具 | 作用 | 数据源 |
| --- | --- | --- |
| `query_batch_yield` | 批次良率查询（按时间/机台/批次） | 模拟 YMS 数据 |
| `query_equipment_alerts` | 设备告警 / PM 记录 | 模拟 MES 数据 |
| `query_fdc_params` | FDC 工艺参数偏离检测 | 模拟 FDC 时序 |
| `query_defect_map` | wafer map 缺陷模式分析 | 模拟缺陷检测数据 |
| `search_historical_rca` | 历史案例检索 | RAG（激活 embedding 能力） |

### 顺带激活的空能力

- **embedding / 向量检索**：历史案例 RAG（`search_historical_rca`）
- **MemoryService**：沉淀历史排查经验（"Agent 永不遗忘过往 excursion"）

---

## 备选方向：工艺知识助手

> **状态**：第二项目备选。当前项目做良率 RCA 为主，此方向作为后续独立项目，记录以免遗忘。

- **一句话定位**：工业版 RAG——将工厂 SOP、机台维护日志、历史生产数据向量化封装为专属知识库，AI Agent 检索问答
- **行业参考**：[鼎华智能](https://digihua.com.tw/news260317/?num=21) 联合 Axe Innovation 的"工业版 RAG"——SMT 生产线 OEE 异常从数小时压缩至秒级
- **与引擎的关系**：关联弱（单 Agent 检索即可，无需多 Agent 编排），工程价值独立
- **若启动**：复用 embedding/向量能力、ContextManager、chat 路由；新增文档解析管线（PDF/手册/日志 → chunk → 向量）

---

## 已关闭方向：EDA 设计辅助

> **状态**：关闭（结构性不可落地，非"暂缓"）。记录原因，避免未来重新评估浪费精力。

四个硬约束：

1. **数据依赖**：需真实工艺 PDK（器件模型/库单元/设计规则）、lib 库、RTL/GDS/netlist —— 晶圆厂与设计公司的核心机密 IP，个人无法获取
2. **工具链依赖**：行业方案编排商业 EDA 工具（Synopsys DC/ICC、Cadence Virtuoso/Innovus、Siemens Questa/Calibre），按年授权、数十万至数百万美元、核心算法封闭；开源替代（OpenLane/Yosys）仅覆盖极小流程
3. **验证零容错**：芯片设计一个 DRC 违规/时序违例 = 流片失败。Agent 产出须经 signoff 级物理引擎验证（行业强调 self-verifying agents），无真实工具链则无法验证 Agent 价值
4. **领域知识门槛**：评估设计质量需器件物理、EDA 算法专业知识，工具跑通也无法判断结果好坏

**对比良率 RCA**：良率 RCA 绕开全部四条 —— 数据可合成、结果由工程师人工验证（幻觉被证据链+置信度约束）、零 license 依赖、领域门槛低。

---

## 里程碑与闭环

按「先出闭环、引擎后置」原则切分，每步均可演示：

| 闭环 | 内容 | 依赖 |
| --- | --- | --- |
| **闭环 1** | 任务受理骨架：`POST /api/tasks` 提交 → worker 异步执行 → `GET /api/tasks/{id}` 查进度 → SSE 进度事件。不加队列也能跑，直接用现有 TaskService 信号量 | 现状 |
| **闭环 2** | 编排：主 Agent 拆分 → 并行子 Agent → 汇总（task.md 阶段 B） | 闭环 1 |
| **闭环 3** | 优先级队列 + 背压（task.md 阶段 A 完整形态） | 闭环 2 |

### 验收标准

一个模拟的良率异常 case，系统能靠多 Agent 并行排查自动收敛到正确根因，输出带**证据链**的根因报告（每步结论可回溯到数据来源）。

---

## 相关文档

- [HANDOFF](HANDOFF.md)（项目交接，顶层计划/进度）
- [task.md](service_doc/task_doc/task.md)（TaskService 调度枢纽 + 多 Agent 编排规划）
- [agent.md](core_doc/agent_doc/agent.md)（Agent 层：单任务执行）
- [llm.md](service_doc/llm_doc/llm.md)（LLM 服务层）
