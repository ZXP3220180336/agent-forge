"""良率 RCA 模拟数据（固定、可复现，无随机）。

围绕可演示根因故事：**LOT-A123 良率骤降（98%→82%）**，排查链有明确指向：

- 良率：LOT-A123 在 ETCH step 骤降，涉及 ETCH-01
- 告警：ETCH-01 有 ALARM（chamber pressure abnormal）+ 一次 PM
- FDC：ETCH-01 的 `chamber_pressure` 偏离**随时间发展**（08:00 正常 → 12:00 +4.9% → 14:00 +12%，baseline 45.0）——时间窗口过滤可看出偏离发展过程
- 缺陷：LOT-A123 wafer 为 center_cluster 模式，top type = particle
- 历史：预置案例佐证「chamber pressure 偏离 → center particle → 良率骤降」，根因 = chamber 需清洁/PM

其余批次 / 机台 / 参数为「正常」对照组，供 Agent 对比排除。
数据固定（非随机）→ 演示与测试可复现；未来接真实数据源仅替换本模块。
"""

from __future__ import annotations

# ===== 批次良率（模拟 YMS） =====
BATCHES: list[dict] = [
    {"batch_id": "LOT-A120", "step": "LITHO", "yield_rate": 97.0, "equipment": "LITHO-01", "timestamp": "2026-08-09 08:00", "drop": False},
    {"batch_id": "LOT-A120", "step": "ETCH", "yield_rate": 96.5, "equipment": "ETCH-02", "timestamp": "2026-08-09 12:00", "drop": False},
    {"batch_id": "LOT-A121", "step": "LITHO", "yield_rate": 96.8, "equipment": "LITHO-01", "timestamp": "2026-08-10 08:00", "drop": False},
    {"batch_id": "LOT-A121", "step": "ETCH", "yield_rate": 96.0, "equipment": "ETCH-02", "timestamp": "2026-08-10 12:00", "drop": False},
    {"batch_id": "LOT-A122", "step": "LITHO", "yield_rate": 97.5, "equipment": "LITHO-01", "timestamp": "2026-08-11 08:00", "drop": False},
    {"batch_id": "LOT-A122", "step": "ETCH", "yield_rate": 96.8, "equipment": "ETCH-02", "timestamp": "2026-08-11 12:00", "drop": False},
    # ---- LOT-A123：ETCH step 良率骤降 ----
    {"batch_id": "LOT-A123", "step": "LITHO", "yield_rate": 97.5, "equipment": "LITHO-01", "timestamp": "2026-08-12 08:00", "drop": False},
    {"batch_id": "LOT-A123", "step": "ETCH", "yield_rate": 82.0, "equipment": "ETCH-01", "timestamp": "2026-08-12 14:30", "drop": True},
    {"batch_id": "LOT-A123", "step": "CMP", "yield_rate": 83.1, "equipment": "CMP-01", "timestamp": "2026-08-13 08:00", "drop": True},
    {"batch_id": "LOT-B100", "step": "LITHO", "yield_rate": 97.2, "equipment": "LITHO-01", "timestamp": "2026-08-12 09:00", "drop": False},
    {"batch_id": "LOT-B100", "step": "ETCH", "yield_rate": 96.9, "equipment": "ETCH-01", "timestamp": "2026-08-12 15:00", "drop": False},
]

# ===== 设备告警 / PM（模拟 MES） =====
ALERTS: list[dict] = [
    {"alert_id": "ALM-1001", "equipment_id": "ETCH-01", "alert_type": "ALARM", "severity": "HIGH",
     "message": "chamber pressure abnormal（超出工艺窗口）", "timestamp": "2026-08-12 13:20"},
    {"alert_id": "ALM-1002", "equipment_id": "ETCH-01", "alert_type": "PM", "severity": "INFO",
     "message": "预防性维护计划（chamber 清洁），建议于 2026-08-13 执行", "timestamp": "2026-08-12 13:25"},
    {"alert_id": "ALM-1003", "equipment_id": "ETCH-02", "alert_type": "INFO", "severity": "LOW",
     "message": "例行状态上报", "timestamp": "2026-08-12 10:00"},
    {"alert_id": "ALM-1004", "equipment_id": "LITHO-01", "alert_type": "INFO", "severity": "LOW",
     "message": "routine check ok", "timestamp": "2026-08-12 09:30"},
    {"alert_id": "ALM-1005", "equipment_id": "CMP-01", "alert_type": "INFO", "severity": "LOW",
     "message": "routine check ok", "timestamp": "2026-08-13 07:00"},
]

# ===== FDC 工艺参数（模拟 FDC 时序，多时间点支持窗口过滤） =====
FDC_PARAMS: list[dict] = [
    # ---- ETCH-01 chamber_pressure 时间序列：偏离随时间发展 ----
    {"equipment_id": "ETCH-01", "process_step": "ETCH", "parameter": "chamber_pressure", "unit": "mTorr",
     "value": 45.1, "baseline": 45.0, "deviation_pct": 0.2, "status": "normal", "timestamp": "2026-08-12 08:00"},
    {"equipment_id": "ETCH-01", "process_step": "ETCH", "parameter": "chamber_pressure", "unit": "mTorr",
     "value": 45.3, "baseline": 45.0, "deviation_pct": 0.7, "status": "normal", "timestamp": "2026-08-12 10:00"},
    {"equipment_id": "ETCH-01", "process_step": "ETCH", "parameter": "chamber_pressure", "unit": "mTorr",
     "value": 47.2, "baseline": 45.0, "deviation_pct": 4.9, "status": "normal", "timestamp": "2026-08-12 12:00"},
    {"equipment_id": "ETCH-01", "process_step": "ETCH", "parameter": "chamber_pressure", "unit": "mTorr",
     "value": 49.0, "baseline": 45.0, "deviation_pct": 8.9, "status": "deviated", "timestamp": "2026-08-12 13:00"},
    {"equipment_id": "ETCH-01", "process_step": "ETCH", "parameter": "chamber_pressure", "unit": "mTorr",
     "value": 50.4, "baseline": 45.0, "deviation_pct": 12.0, "status": "deviated", "timestamp": "2026-08-12 14:00"},
    # ---- ETCH-01 其他参数（单点正常，对照） ----
    {"equipment_id": "ETCH-01", "process_step": "ETCH", "parameter": "rf_power", "unit": "W",
     "value": 800.0, "baseline": 800.0, "deviation_pct": 0.0, "status": "normal", "timestamp": "2026-08-12 14:00"},
    {"equipment_id": "ETCH-01", "process_step": "ETCH", "parameter": "temperature", "unit": "C",
     "value": 60.2, "baseline": 60.0, "deviation_pct": 0.3, "status": "normal", "timestamp": "2026-08-12 14:00"},
    # ---- ETCH-02 / LITHO-01 / CMP-01 正常对照 ----
    {"equipment_id": "ETCH-02", "process_step": "ETCH", "parameter": "chamber_pressure", "unit": "mTorr",
     "value": 45.2, "baseline": 45.0, "deviation_pct": 0.4, "status": "normal", "timestamp": "2026-08-12 11:00"},
    {"equipment_id": "ETCH-02", "process_step": "ETCH", "parameter": "rf_power", "unit": "W",
     "value": 801.0, "baseline": 800.0, "deviation_pct": 0.1, "status": "normal", "timestamp": "2026-08-12 11:00"},
    {"equipment_id": "LITHO-01", "process_step": "LITHO", "parameter": "focus_offset", "unit": "um",
     "value": 0.05, "baseline": 0.05, "deviation_pct": 0.0, "status": "normal", "timestamp": "2026-08-12 08:30"},
    {"equipment_id": "CMP-01", "process_step": "CMP", "parameter": "platen_pressure", "unit": "psi",
     "value": 3.5, "baseline": 3.5, "deviation_pct": 0.0, "status": "normal", "timestamp": "2026-08-13 08:10"},
]

# ===== 缺陷分布（模拟缺陷检测） =====
DEFECTS: list[dict] = [
    {"batch_id": "LOT-A123", "wafer_id": "W-001", "pattern": "center_cluster", "defect_count": 42,
     "top_type": "particle", "size_um": 1.8, "sampled_at": "2026-08-12 16:00"},
    {"batch_id": "LOT-A123", "wafer_id": "W-002", "pattern": "center_cluster", "defect_count": 35,
     "top_type": "particle", "size_um": 1.6, "sampled_at": "2026-08-12 16:30"},
    {"batch_id": "LOT-A123", "wafer_id": "W-003", "pattern": "center_cluster", "defect_count": 38,
     "top_type": "particle", "size_um": 1.9, "sampled_at": "2026-08-12 17:00"},
    {"batch_id": "LOT-A121", "wafer_id": "W-101", "pattern": "random", "defect_count": 5,
     "top_type": "scratch", "size_um": 0.6, "sampled_at": "2026-08-10 14:00"},
    {"batch_id": "LOT-A122", "wafer_id": "W-201", "pattern": "edge", "defect_count": 8,
     "top_type": "edge_exclusion", "size_um": 1.2, "sampled_at": "2026-08-11 13:00"},
    {"batch_id": "LOT-B100", "wafer_id": "W-301", "pattern": "random", "defect_count": 3,
     "top_type": "unknown", "size_um": 0.4, "sampled_at": "2026-08-12 16:30"},
]

# ===== 历史 RCA 案例（模拟案例库） =====
HISTORY: list[dict] = [
    {"case_id": "RCA-001", "symptom": "批次良率骤降 98%→83%，etch step 异常",
     "root_cause": "chamber 内部污染导致粒子聚集，center particle 缺陷，需 chamber 清洁 / PM",
     "evidence": "FDC chamber_pressure 偏离 +12%，defect map 为 center_cluster / particle",
     "resolution": "执行 chamber 清洁 + 更换 consumable 后恢复", "timestamp": "2026-06-15"},
    {"case_id": "RCA-002", "symptom": "CMP 后良率下降，wafer 边缘缺陷",
     "root_cause": "platen pressure 漂移导致边缘研磨不均",
     "evidence": "FDC platen_pressure 偏离 +5%，defect map 为 edge 模式",
     "resolution": "校准 platen pressure 后恢复", "timestamp": "2026-05-20"},
    {"case_id": "RCA-003", "symptom": "光刻层良率下降，聚焦偏移",
     "root_cause": "litho focus offset 超窗口",
     "evidence": "FDC focus_offset 偏离，defect map 随机散点",
     "resolution": "重做光刻机台 focus 校正", "timestamp": "2026-07-02"},
    {"case_id": "RCA-004", "symptom": "设备告警后批次良率下降",
     "root_cause": "chamber pressure 异常 → 刻蚀不均匀",
     "evidence": "MES ALARM + FDC pressure 偏离",
     "resolution": "chamber 清洁 + 确认 PM 后恢复", "timestamp": "2026-04-18"},
]

# ===== 查询函数（供工具调用；固定数据直接过滤，可复现） =====


def _in_range(timestamp: str, time_range: str | None) -> bool:
    """ISO 格式时间范围过滤（`'start~end'`，缺省端不限；同格式字符串比较）。

    - 两端须含完整时间，**缺日期的一端自动补对端日期**
      （支持 `'2026-08-12 08:00~11:00'` → end 补为同日 11:00）
    - start / end 可整体缺省（`'~2026-08-12 11:00'` / `'2026-08-12 08:00~'`）
    """
    if not time_range:
        return True
    start, _, end = time_range.partition("~")
    start = start.strip()
    end = end.strip()
    if start and end:
        if " " not in start and " " in end:  # start 仅时间 → 补 end 日期
            start = end[:10] + " " + start
        elif " " not in end and " " in start:  # end 仅时间 → 补 start 日期
            end = start[:10] + " " + end
    if start and timestamp < start:
        return False
    if end and timestamp > end:
        return False
    return True


def query_yield(batch_id: str, time_range: str | None = None) -> list[dict]:
    """按批次查良率记录（可选时间窗口，保持时间顺序）。"""
    return [
        r
        for r in BATCHES
        if r["batch_id"] == batch_id and _in_range(r["timestamp"], time_range)
    ]


def query_alerts(
    equipment_id: str | None = None,
    alert_type: str | None = None,
    time_range: str | None = None,
) -> list[dict]:
    """按机台 / 告警类型 / 时间窗口过滤。"""
    result = ALERTS
    if equipment_id:
        result = [r for r in result if r["equipment_id"] == equipment_id]
    if alert_type:
        result = [r for r in result if r["alert_type"] == alert_type]
    if time_range:
        result = [r for r in result if _in_range(r["timestamp"], time_range)]
    return result


def query_fdc(
    equipment_id: str,
    process_step: str | None = None,
    time_range: str | None = None,
) -> list[dict]:
    """按机台（及可选 step / 时间窗口）查 FDC 参数。"""
    result = [r for r in FDC_PARAMS if r["equipment_id"] == equipment_id]
    if process_step:
        result = [r for r in result if r["process_step"] == process_step]
    if time_range:
        result = [r for r in result if _in_range(r["timestamp"], time_range)]
    return result


def query_defects(batch_id: str, wafer_id: str | None = None) -> list[dict]:
    """按批次（及可选 wafer）查缺陷分布。"""
    result = [r for r in DEFECTS if r["batch_id"] == batch_id]
    if wafer_id:
        result = [r for r in result if r["wafer_id"] == wafer_id]
    return result


def search_history(query: str, top_k: int = 3) -> list[dict]:
    """关键词匹配历史案例（RAG 召回为后续增强，当前用文本子串打分）。

    返回案例列表，每个案例附带相关度信号（供 Agent 置信度分级）：
    - `score`：命中 token 数（+ 整句子串命中 +1）
    - `confidence`：`score / (去重 token 数 + 1)`，范围 (0, 1]
    """
    query_lower = query.lower()
    query_compact = query_lower.replace(" ", "")
    tokens = [t for t in query_lower.split() if t]
    max_score = max(1, len(set(tokens)) + 1)

    scored: list[tuple[int, float, dict]] = []
    for case in HISTORY:
        haystack = (
            case["symptom"] + " " + case["root_cause"] + " " + case["evidence"]
        ).lower()
        haystack_compact = haystack.replace(" ", "")
        score = sum(1 for token in set(tokens) if token in haystack)
        if query_compact and query_compact in haystack_compact:
            score += 1  # 整句子串命中（中英文混排）
        if score:
            confidence = round(score / max_score, 2)
            scored.append((score, confidence, case))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {**case, "score": score, "confidence": confidence}
        for score, confidence, case in scored[:top_k]
    ]
