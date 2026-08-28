# 结构化输出约束（Final Answer 工具模式）

> 日期：2026-08-28 ｜ 层级：domain/reasoning
> 定位：本决策完整记录「结构化输出约束」的讨论过程——时机、方式、取舍与工业界依据，供后续（产物链 / 增强项）决策参考，不只记结论。

## Context

对标增强项 #15「结构化输出约束」：ReAct 循环内无 schema 约束，最终答案为自由文本。需求：让 Agent 最终产出符合 JSON Schema 的结构化结果（服务于根因报告等产物链）。

### 一、结构化输出时机：最终答案 vs 中间推理

| 维度 | 中间推理结构化 | 最终答案结构化 |
| --- | --- | --- |
| 约束对象 | ReAct **每轮**输出格式（如 SMOL `Thought + code`） | 最终产物（报告/答案）格式 |
| 前提 | 执行机制依赖可解析的中间输出（SMOL 直接执行生成的代码） | 下游需 schema 校验的产物 |
| 与工具调用关系 | 可能冲突（约束轮无法返回 tool_calls） | 互补（工具编排 + 最终结构化） |

**工业界触发时机判别**（MachineLearningMastery / OpenAI / Dev 社区）：纯提取/数据转换 → structured output（单次 schema 强制）；需外部数据的问答、多步推理 → 工具调用；校验并返回单一格式 → structured output；歧义请求 → 工具调用（模型可跳过工具直接答）。

**本项目结论**：选**最终答案结构化**——本项目用 OpenAI 工具协议，中间轮强约束与 tool_calls 冲突（SMOL 的中间约束是为其代码执行机制服务的，本项目无此前提）。

### 二、约束方式对比（讨论全过程）

| 方式 | 机制 | 优劣 |
| --- | --- | --- |
| **严格 `output_type`**（response_format 全程约束） | API 级 schema 强制 | **不可取**：会抑制中间工具调用——模型可能跳过查数据直接输出 JSON（OpenAI 官方确认的 Output_Type Conflict） |
| **Final Answer 工具** | 定义 schema 约束的 `final_answer` 工具，模型最后调用提交结构化结果 | 模型**从开始按 schema 组织输出**（原生结构化、无信息损失）；无额外 LLM 调用；兼作循环终止（SMOL `is_final_answer` / OpenAI 官方推荐）；融入控制流。代价：注入工具 + 提示词约束（侵入性） |
| **事后提取**（复用 generate_structured） | 循环结束后对完整上下文额外一次提取 | 二次加工有信息损失（主模型回答时不知道下游要什么字段，提取补不回来）；+1 次调用；非原生。但零侵入 |

### 三、决策

本项目选 **Final Answer 工具模式**：

1. 模型原生结构化——推理过程中对齐 schema 组织证据链，质量高于事后提取
2. 无额外 LLM 调用（工具调用算循环轮）
3. 兼作循环终止语义（与 SMOL/OpenAI 官方一致）
4. 当前 chat 阶段可接受注入侵入性，产物链（根因报告）受益最大
5. 失败安全：参数校验失败走回喂（VALIDATION）让模型自纠，不静默吞掉

### 四、与 generate_structured 的分工（不冗余）

`generate_structured`（StructuredOutput.extract，三级降级）已存在，**不重复实现**，定位为**通用提取能力**：

- 非 Agent 场景（分类、抽取、JSON mode 降级）
- final_answer 失败的潜在兜底（升级路径，当前未叠加）

两者不同定位：final_answer = Agent 循环内的原生终止机制；generate_structured = 独立提取能力。

## Consequences

- ✅ 增强项 #15 落地：`execute` 加 `output_schema`，注入 final_answer 工具；模型调用即终止并产出 `outcome.structured` / `AgentResult.structured`；校验失败回喂自纠
- ✅ 未配置 `output_schema` 时零侵入（不注入工具，行为不变）
- ✅ 复用 jsonschema（项目已有依赖）做本地校验；失败进证据链（VALIDATION）
- ⚠️ 模型可能不使用 final_answer（自由文本结束）→ `structured=None`，content 保留（无害降级）
- 📌 升级路径：产物链接入时经 AgentContext 配置 output_schema；可叠加 generate_structured 兜底
