"""领域工具批次的事实收集器。"""

from __future__ import annotations

import copy

from app.domain.ports.tool_execution import ToolFact


class ToolBatchCollector:
    """按当前消费调用及规范 attempt/revision 保存批次事实快照。"""

    def __init__(self) -> None:
        self._facts: dict[tuple[str, str, str, str | None], ToolFact] = {}
        self._closed = False

    def record(self, fact: ToolFact) -> bool:
        """幂等接管较新的事实；关闭后拒绝继续持有 Integration 更新。"""
        if self._closed:
            return False
        key = (
            fact.batch_id,
            fact.tool_call_id,
            fact.operation_id,
            fact.attempt_id,
        )
        current = self._facts.get(key)
        if current is None or fact.revision > current.revision:
            self._facts[key] = copy.deepcopy(fact)
        return True

    def snapshot(self) -> tuple[ToolFact, ...]:
        """返回调用方专有副本，避免其修改污染收集器中的事实。"""
        return tuple(copy.deepcopy(fact) for fact in self._facts.values())

    def close(self) -> None:
        """停止接收晚到更新；已接管快照仍可读取。"""
        self._closed = True
