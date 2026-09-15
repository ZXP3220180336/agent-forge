"""Planner 步骤规范化、计划快照与单步审计记录的纯转换。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .react import ReActOutcome


def normalize_steps(
    steps: list[dict],
    completed: set[int],
    start_no: int = 1,
) -> list[dict]:
    """赋予单调 id，并清理不能指向已完成或更早步骤的依赖。"""
    pending: list[dict] = []
    seen: set[int] = set()
    for index, step in enumerate(steps):
        step_id = start_no + index
        dependencies = {
            dependency
            for dependency in step.get("depends_on", [])
            if dependency in completed
            or (dependency in seen and dependency >= start_no)
        }
        seen.add(step_id)
        pending.append({
            "id": step_id,
            "description": str(step.get("description", "")).strip(),
            "deps": dependencies,
        })
    return [step for step in pending if step["description"]]


def build_plan_payload(goal: str, steps: list[dict]) -> dict:
    """构造对外计划快照，只暴露步骤 id 与描述。"""
    return {
        "goal": goal,
        "steps": [
            {"id": step["id"], "description": step["description"]}
            for step in steps
        ],
    }


def build_step_record(step: dict, outcome: ReActOutcome | None) -> dict[str, Any]:
    """按 Planner 二维判据把 ReAct 结果转换为单步审计记录。"""
    if outcome is None:
        success = False
        failure_reason = "ReAct 子跑未产出结果"
    else:
        success = outcome.success and bool(outcome.content.strip())
        failure_reason = outcome.error or ("" if success else "步骤产出为空")

    return {
        "id": step["id"],
        "description": step["description"],
        "depends_on": sorted(step["deps"]),
        "success": success,
        "summary": (outcome.content if success and outcome else "")[:500],
        "error": None if success else failure_reason,
        "content": outcome.content if outcome else "",
        "tool_calls": outcome.tool_calls if outcome else [],
        "iterations": outcome.iterations if outcome else 0,
        "total_tokens": outcome.total_tokens if outcome else 0,
    }
