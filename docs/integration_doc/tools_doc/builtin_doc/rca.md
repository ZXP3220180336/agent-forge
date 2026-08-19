# 良率 RCA 工具子模块说明

> **更新日期**：2026-08-17
> **模块**：`app/integration/tools/builtin/rca/`
> **文档定位**：良率根因分析（Yield RCA）**场景工具**——支撑产品主链路「批次异常 → 并行排查 → 带证据链的根因报告」（见 [product.md](../../../product.md) P0）。
> **实现状态**：5 工具全部 ✅（模拟数据源）

---

## 设计目标

1. **落地产品主链路场景工具**：良率工程师可经 Agent 完成「查良率 → 查告警 → 查 FDC → 查缺陷 → 查历史」的 RCA 排查链
2. **模拟数据固定可复现**：围绕 LOT-A123 根因故事（非随机），演示与测试稳定；未来接真实数据源仅替换 `data.py`
3. **证据链 metadata**：每个工具结果带 `source` / 查询键 / `timestamp`（产品「证据链」亮点的载体）

## 工具契约（全部 L0 只读）

| 工具 | 参数 | 返回内容 | category | source |
| --- | --- | --- | --- | --- |
| `query_batch_yield` | `batch_id` 必填、`time_range` 可选 | 批次良率记录（按 step，含骤降标记） | yield | mock_yms |
| `query_equipment_alerts` | `equipment_id`/`alert_type`/`time_range` 可选 | 告警 / PM 记录 | equipment | mock_mes |
| `query_fdc_params` | `equipment_id` 必填、`process_step`/`time_range` 可选 | FDC 参数偏离（value/baseline/deviation/status） | fdc | mock_fdc |
| `query_defect_map` | `batch_id` 必填、`wafer_id` 可选 | wafer 缺陷分布（模式/数量/主导类型） | defect | mock_defect |
| `search_historical_rca` | `query` 必填、`top_k` 可选(默认3) | 匹配历史案例（症状/根因/证据/解决） | history | mock_history |

> `time_range` 格式 `'start~end'`（如 `2026-08-12 08:00~2026-08-12 20:00`），start / end 可单侧缺省——支撑「对比异常前后」的 RCA 核心动作。

## 排查链示例（LOT-A123）

模拟数据内置一个可演示根因 case，Agent 可沿证据链收敛到根因：

```text
query_batch_yield("LOT-A123")      → ETCH step 良率骤降 82%（涉及 ETCH-01）
query_equipment_alerts("ETCH-01")  → chamber pressure ALARM + 一次 PM
query_fdc_params("ETCH-01", "2026-08-12 08:00~14:30") → 偏离随时间发展（08:00 正常 → 14:00 +12%）
query_defect_map("LOT-A123")       → center_cluster 模式，主导类型 particle
search_historical_rca("etch 偏离 良率 骤降") → RCA-001 佐证
结论 → 根因：chamber 内部污染致粒子聚集，需 chamber 清洁 / PM
```

其余批次 / 机台 / 参数为「正常」对照组，供 Agent 对比排除（定位异常而非泛化归因）。

## 模拟数据（data.py）

固定数据（无随机）——`BATCHES` / `ALERTS` / `FDC_PARAMS` / `DEFECTS` / `HISTORY` + 查询辅助函数。两次查询结果恒等，演示 / 测试可复现。数据源标识经工具 metadata 暴露（`source=mock_yms` 等）。

**时间序列**：`FDC_PARAMS` 含同机台多时间点样本（ETCH-01 的 `chamber_pressure` 偏离随时间发展：08:00 正常 → 12:00 +4.9% → 14:00 +12%）——配合 `time_range` 窗口过滤可看「异常如何发展」，支撑异常前后对比。

## search_historical_rca 简化

当前用**预置案例 + 关键词匹配**（query 与案例文本子串打分，返回 top_k）跑通主链路；**RAG（embedding 召回）为后续增强**——产品审核标注的升级路径，不阻塞本次落地。

**相关度 / 置信度信号**：每个案例返回 `confidence`（score / 去重 token 数 + 1，范围 (0,1]）；content 显示 `[相关度 X%]`，metadata 带 `top_confidence`——服务产品「置信度分级」亮点（Agent 可据历史佐证强度调整结论置信度）。

## 测试状态

`tests/unit/test_rca_tools.py`（15 用例）：各工具正常查询 + 证据链 metadata / 参数校验 / 业务未找到 / LOT-A123 根因故事（骤降、FDC 偏离、center_cluster、历史命中）/ 数据可复现 / init_default_tools 装配 10 工具 / 时间窗口过滤（P0-1，含单侧缺省）/ 相关度置信度（P0-2）。

## 相关文档

- [product.md](../../../product.md)（产品规划：工具清单 P0、证据链亮点）
- [内置工具详解](builtin.md)（BaseTool 基类 / 自动发现）
- [工具模块接口文档](../tools.md)（ToolResult 契约 / ErrorCode）
- [TOOLS-029 问题记录](../../../../issues/integration/tools/2026-08-20-rca-in-range-single-time.md)（_in_range 单边时间）
- [TOOLS-030 问题记录](../../../../issues/integration/tools/2026-08-20-rca-time-range-dry.md)（time_range 过滤抽取）
