#!/usr/bin/env python
"""
Reflection 严格期限的预算观察脚本

用途：给定总预算 `max_execution_time`，观察 Reflection 一轮执行里每一跳实际花了多少
时间，以及预算不足时链路以什么形态结束。用于排查「时序敏感用例在全量负载下失败」这类
问题，以及评估某个预算下还剩多少余量。

背景（2026-09-21）：定位 `tests/unit/test_reflection.py` 严格期限用例在全量负载下失败时，
发现根因不在该用例的断言，而在它的预算窗口内包含了成本随环境变化的真实工具执行——
`ToolService.execute` 每次调用都会先刷新外部插件目录，观察显示**一次扫描就吃掉内部期限
的绝大部分**，工具本体耗时接近 0；余量随插件数量与机器负载浮动，争用一上来就越界，先
命中的是工具自身 deadline（抛 ToolDeadlineExceededError）。该用例已改为草稿阶段不发起
工具调用（窗口内只剩纯内存的桩调用），本脚本则保留「带真实工具调用」的场景，用于量化
扫描成本与预算余量的关系——任何走真实工具执行的策略运行都适用同一结论。

运行：
    cd 项目根目录
    uv run python -m scripts.observe_reflection_deadline            # 默认扫 0.1/0.2/0.5/1.0
    uv run python -m scripts.observe_reflection_deadline 0.1 0.1    # 指定预算，可重复

要观察预算不足时的逃逸形态，可并行跑多份本脚本制造争用（空载机器上余量通常够用）。
"""

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

# Windows 控制台默认 GBK，统一切换为 UTF-8，避免打印中文与 emoji 时崩溃
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.domain.reasoning import ReflectionStrategy
from app.domain.reasoning.execution import (
    ContextWindowLimits,
    ExecutionLimits,
    ModelOptions,
    ReasoningRunScope,
    RecoveryBudget,
    ToolExecutionOptions,
)
from app.integration.tools.base import BaseTool, ToolResult
from app.integration.tools.execution import ToolEffectClass, ToolExecutionSpec
from app.integration.tools.tool_service import ToolService
from app.platform.observability.logger import setup_logging
from app.shared.events import build_message_event

DRAFT = {
    "summary": "良率下降归因于设备 A 告警",
    "conclusions": [
        {
            "claim": "设备 A 存在多次告警",
            "supporting_evidence": ["echo: query=alerts"],
            "confidence": 0.8,
        }
    ],
    "next_steps": ["查询设备 A 详细告警记录"],
    "explicit_abstention": [],
}

_STARTED_AT = time.monotonic()


def mark(label: str) -> None:
    """按相对时间戳打印链路打点，便于对齐各跳耗时。"""
    print(f"[{time.monotonic() - _STARTED_AT:8.4f}s] {label}", flush=True)


def _tool_call(name: str, args: dict) -> dict:
    return {
        "id": f"call_{name}",
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class _EchoTool(BaseTool):
    """即时返回的只读工具（ReAct 收集阶段执行），并在本体两侧打点。"""

    def __init__(self) -> None:
        self.body_seconds = 0.0

    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "回声工具"

    @property
    def parameters(self) -> dict:
        return {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }

    def describe_execution(self, parameters: dict) -> ToolExecutionSpec:
        """观察用替身仅操作内存，不产生外部副作用。"""
        return ToolExecutionSpec(effect_class=ToolEffectClass.READ_ONLY)

    async def execute(self, **kwargs) -> ToolResult:
        started = time.monotonic()
        mark(f"      └─ [工具本体] 开始执行 {kwargs}")
        content = f"echo:{kwargs.get('text', '')}"
        self.body_seconds = time.monotonic() - started
        mark("      └─ [工具本体] 返回")
        return ToolResult(success=True, content=content)


class _TimedToolService(ToolService):
    """在 execute 两侧打点，并打印本次调用拿到的期限余量。

    进入 → 工具本体启动之间的间隔即「外部插件刷新 + 准入」的耗时，
    也是本脚本要量化的主要成本（目录签名扫描 + 模块导入）。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.entered_at = 0.0
        self.deadline_margin = 0.0
        self.elapsed = 0.0

    async def execute(self, *args, **kwargs):
        call = kwargs.get("call")
        deadline = getattr(call, "deadline", None)
        self.entered_at = time.monotonic()
        self.deadline_margin = (deadline - self.entered_at) if deadline else 0.0
        mark(f"   ├─ [ToolService] 进入 {args[0] if args else '?'}，期限余量={self.deadline_margin:.4f}s")
        try:
            result = await super().execute(*args, **kwargs)
        except BaseException as exc:
            self.elapsed = time.monotonic() - self.entered_at
            mark(f"   ├─ [ToolService] 抛出 {type(exc).__name__}（耗时 {self.elapsed:.4f}s）")
            raise
        self.elapsed = time.monotonic() - self.entered_at
        mark(f"   ├─ [ToolService] 返回 success={result.success}（耗时 {self.elapsed:.4f}s）")
        return result


class _ScriptedLLM:
    """脚本化 LLM 替身：打印每次调用的类型与收到的期限余量。"""

    def __init__(self, react_scripts: list[dict], structured_scripts: list, usage: dict | None = None) -> None:
        self.react_scripts = react_scripts
        self.structured_scripts = structured_scripts
        self.usage_spec = usage or {}
        self.react_calls = 0
        self.structured_calls = 0

    async def async_generate(self, *args, result=None, **kwargs):
        self.react_calls += 1
        spec = self.react_scripts[min(self.react_calls - 1, len(self.react_scripts) - 1)]
        mark(f"   ├─ [LLM 流式] 第 {self.react_calls} 次，finish_reason={spec.get('finish_reason')}")
        if result is not None:
            for key, value in spec.items():
                setattr(result, key, value)
        yield build_message_event(spec.get("content", ""))

    async def generate_structured(
        self,
        messages,
        schema,
        model_key="fast",
        max_tokens=None,
        usage=None,
        cancel_event=None,
        deadline=None,
    ):
        self.structured_calls += 1
        margin = (deadline - time.monotonic()) if deadline else None
        shown = "无期限" if margin is None else f"{margin:.4f}s"
        mark(f"   ├─ [LLM 结构化] 第 {self.structured_calls} 次，期限余量={shown}")
        spec = self.structured_scripts[min(self.structured_calls - 1, len(self.structured_scripts) - 1)]
        if isinstance(spec, Exception):
            raise spec
        if usage is not None and self.usage_spec:
            usage.update(self.usage_spec)
        return spec


class _LateCritiqueLLM(_ScriptedLLM):
    """自查必然迟到：睡到自己收到的期限之后 0.01 秒再返回（复现严格期限语义）。"""

    async def generate_structured(self, *args, **kwargs):
        deadline = kwargs["deadline"]
        await asyncio.sleep(max(0.0, deadline - time.monotonic()) + 0.01)
        return await super().generate_structured(*args, **kwargs)


def _execution_args(budget: float) -> dict:
    """组装 Reflection 的语义执行参数（与生产调用口径一致）。"""
    return {
        "run": ReasoningRunScope(run_id="run-observe-reflection", run_stop=asyncio.Event()),
        "model": ModelOptions(temperature=0.2, max_tokens=1024),
        "limits": ExecutionLimits(max_iterations=5, max_execution_time=budget),
        "context_window": ContextWindowLimits(),
        "recovery": RecoveryBudget(max_refine_rounds=2),
        "tool_execution": ToolExecutionOptions(),
    }


def _react_scripts_with_draft() -> list[dict]:
    """ReAct 脚本：一次工具收集 → final_answer 提交结构化初稿。"""
    return [
        {"finish_reason": "tool_calls", "tool_calls": [_tool_call("echo", {"text": "query=alerts"})]},
        {"finish_reason": "tool_calls", "tool_calls": [_tool_call("final_answer", DRAFT)]},
    ]


async def run_once(budget: float) -> dict:
    """跑一轮并返回观察结果；不抛异常，把终止形态作为结果返回。"""
    print(f"\n{'=' * 78}\n预算 max_execution_time = {budget}s\n{'=' * 78}", flush=True)
    tool = _EchoTool()
    tools = _TimedToolService(max_concurrent_tools=10)
    tools.register(tool)
    llm = _LateCritiqueLLM(
        _react_scripts_with_draft(),
        [{"ok": True, "issues": []}],
        usage={"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
    )
    strategy = ReflectionStrategy(llm=llm, tools=tools)

    mark("[主流程] strategy.execute 开始")
    started = time.monotonic()
    outcome: dict = {"budget": budget, "escaped": None}
    try:
        events = []
        async for ev in strategy.execute(
            "分析良率下降原因",
            [{"role": "user", "content": "分析良率下降原因"}],
            **_execution_args(budget),
        ):
            events.append(ev)
    except BaseException as exc:  # noqa: BLE001 — 观察脚本：逃逸形态就是要观察的结果
        outcome["escaped"] = type(exc).__name__
        outcome["total_seconds"] = time.monotonic() - started
        mark(f"[主流程] 异常逃出 execute()：{type(exc).__name__}")
        print(f"  → 形态：未到达终态组装即逃逸；outcome={strategy.outcome!r}")
        outcome.update(_fill_marks(tools, tool, llm, events=0, outcome_obj=None))
        return outcome

    outcome["total_seconds"] = time.monotonic() - started
    mark(f"[主流程] execute() 正常结束，事件 {len(events)} 条")
    print(f"  → outcome.success={strategy.outcome.success} degraded={strategy.outcome.degraded}")
    print(f"  → error={strategy.outcome.error!r}")
    print(
        f"  → structured={'有 draft' if strategy.outcome.structured else 'None'} critique={strategy.outcome.critique}"
    )
    outcome.update(_fill_marks(tools, tool, llm, events=len(events), outcome_obj=strategy.outcome))
    return outcome


def _fill_marks(tools, tool, llm, *, events: int, outcome_obj) -> dict:
    """汇总本轮的量测结果；工具本体启动时刻用于拆出「刷新 + 准入」成本。"""
    return {
        "refresh_plus_admission": tools.elapsed - tool.body_seconds,
        "tool_body": tool.body_seconds,
        "tool_total": tools.elapsed,
        "deadline_margin": tools.deadline_margin,
        "events": events,
        "react_calls": llm.react_calls,
        "structured_calls": llm.structured_calls,
        "degraded": None if outcome_obj is None else outcome_obj.degraded,
    }


def _print_summary(rows: list[dict]) -> None:
    print(f"\n{'=' * 78}\n汇总（时间单位：秒）\n{'=' * 78}")
    header = f"{'预算':>7} {'期限余量':>10} {'刷新+准入':>10} {'工具本体':>10} {'本轮总耗时':>11}  形态"
    print(header)
    print("-" * len(header))
    for row in rows:
        shape = row["escaped"] or f"正常结束（degraded={row['degraded']}）"
        print(
            f"{row['budget']:>7.2f} {row['deadline_margin']:>10.4f} "
            f"{row['refresh_plus_admission']:>10.4f} {row['tool_body']:>10.4f} "
            f"{row['total_seconds']:>11.4f}  {shape}"
        )
    print(
        "\n读法：「刷新+准入」是外部插件目录扫描加模块导入的耗时，通常远大于工具本体；\n"
        "「期限余量」是工具调用进入时距离自身 deadline 的剩余时间——它小于「刷新+准入」时，\n"
        "本次调用必然在工具本体启动前或启动中越界。"
    )


async def main() -> None:
    setup_logging(
        level="INFO",
        log_file=str(Path(tempfile.gettempdir()) / "observe_reflection_deadline.log"),
        log_format="text",
    )
    budgets = [float(arg) for arg in sys.argv[1:]] or [0.1, 0.2, 0.5, 1.0]
    print(f"链路日志同时写入 {Path(tempfile.gettempdir()) / 'observe_reflection_deadline.log'}")
    rows = [await run_once(budget) for budget in budgets]
    _print_summary(rows)


if __name__ == "__main__":
    asyncio.run(main())
