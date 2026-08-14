"""工具执行统计。"""

import time
from dataclasses import dataclass
from typing import Any


@dataclass
class ToolStats:
    """工具执行统计（原 ToolService 内 ToolStats 拆分）。"""

    call_count: int = 0  # 调用次数
    success_count: int = 0  # 成功次数
    failed_count: int = 0  # 失败次数
    total_time: float = 0.0  # 总耗时（秒）
    last_call_time: float | None = None  # 最后调用时间戳

    @property
    def success_rate(self) -> float:
        """成功率"""
        if self.call_count == 0:
            return 0.0
        return self.success_count / self.call_count

    @property
    def avg_time(self) -> float:
        """平均耗时（秒）"""
        if self.call_count == 0:
            return 0.0
        return self.total_time / self.call_count


class ToolStatsCollector:
    """统计采集器：记录 / 初始化 / 查询 / 摘要（原 ToolService 统计职责拆分）。"""

    def __init__(self) -> None:
        self._stats: dict[str, ToolStats] = {}

    def init(self, name: str) -> None:
        """注册工具时初始化统计条目（幂等）。"""
        if name not in self._stats:
            self._stats[name] = ToolStats()

    def remove(self, name: str) -> None:
        """注销工具时删除统计条目。"""
        self._stats.pop(name, None)

    def record(self, name: str, success: bool, elapsed: float) -> None:
        """记录一次执行（未注册工具名惰性建条目）。"""
        if name not in self._stats:
            self._stats[name] = ToolStats()

        stats = self._stats[name]
        stats.call_count += 1
        stats.total_time += elapsed
        stats.last_call_time = time.time()

        if success:
            stats.success_count += 1
        else:
            stats.failed_count += 1

    def get(
        self, name: str | None = None
    ) -> dict[str, ToolStats] | ToolStats | None:
        """单工具统计（name 给定）或全量字典（name 为 None）。"""
        if name is not None:
            return self._stats.get(name)
        return dict(self._stats)

    def summary(self) -> dict[str, Any]:
        """全量统计摘要：总调用数 / 成功率 / 各工具详情。"""
        total_calls = sum(s.call_count for s in self._stats.values())
        total_success = sum(s.success_count for s in self._stats.values())
        total_failed = sum(s.failed_count for s in self._stats.values())

        return {
            "total_calls": total_calls,
            "total_success": total_success,
            "total_failed": total_failed,
            "overall_success_rate": total_success / total_calls
            if total_calls > 0
            else 0.0,
            "tools": {
                name: {
                    "call_count": s.call_count,
                    "success_rate": round(s.success_rate, 4),
                    "avg_time": round(s.avg_time, 4),
                    "last_call_time": s.last_call_time,
                }
                for name, s in self._stats.items()
            },
        }
