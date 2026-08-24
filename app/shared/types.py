"""通用类型 / 标识（共享内核）。

集中定义跨模块复用的标识符与类型别名，避免各模块重复定义裸 str。
被所有层引用，无反向依赖。
"""

from __future__ import annotations

from typing import NewType, TypeAlias

# 标识符（NewType：运行时恒等返回 str，纯类型标注，区分不同用途的标识）
SessionId = NewType("SessionId", str)  # 会话标识
UserId = NewType("UserId", str)  # 用户标识

# 类型别名
Messages: TypeAlias = list[dict]  # LLM 消息列表（OpenAI messages 格式）
