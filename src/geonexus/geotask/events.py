"""任务事件 —— 契约 §4。

与 ``geonexus.cafe.events`` 的关系：那是**单进程内存事件总线**，形状里缺 ``seq``
（客户端无法判断丢帧，重连也无法去重）。这里定义 GeoTask 的事件契约，并把 ``seq``
作为必需字段；``cafe.events`` 补上同一个字段，两者可共存于同一个任务的不同阶段。
"""

from __future__ import annotations

import time
from typing import Any

__all__ = [
    "EVENT_TYPES", "TASK_CREATED", "TASK_STATE_CHANGED", "TASK_PROGRESS",
    "TASK_OUTPUT", "TASK_FAILED", "TASK_CANCELLED", "build_event",
]

TASK_CREATED = "task.created"
TASK_STATE_CHANGED = "task.state_changed"
TASK_PROGRESS = "task.progress"
TASK_OUTPUT = "task.output"
TASK_FAILED = "task.failed"
TASK_CANCELLED = "task.cancelled"

EVENT_TYPES = frozenset({
    TASK_CREATED, TASK_STATE_CHANGED, TASK_PROGRESS,
    TASK_OUTPUT, TASK_FAILED, TASK_CANCELLED,
})


def build_event(task_id: str, seq: int, event: str, data: dict[str, Any],
                attempt: int = 0, *, timestamp: float | None = None) -> dict[str, Any]:
    """构造一个事件。

    ``seq`` 是**每个任务内**从 1 开始严格递增的序号：同一任务内不得乱序、不得跳号；
    跨任务不保证顺序（需要顺序就按 seq 排，别假设到达顺序）。
    """
    if event not in EVENT_TYPES:
        raise ValueError(f"未知事件类型: {event!r}；可用 {sorted(EVENT_TYPES)}")
    return {
        "task_id": task_id,
        "seq": seq,
        "event": event,
        "data": data,
        "attempt": attempt,
        "timestamp": time.time() if timestamp is None else timestamp,
    }
