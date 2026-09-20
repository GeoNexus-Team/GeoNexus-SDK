"""状态机 —— 契约 §3 的机器可读形式。

**这里是唯一的关卡**：状态只能通过 :func:`assert_transition` 迁移，而
:class:`~geonexus.geotask.store.TaskStore` 的 ``transition()`` 是唯一能改状态的
入口。这样"状态机"不是一个文档约定，而是代码里会抛异常的地方。

``TRANSITIONS`` 必须与 ``docs/GEOTASK.md`` 里 ``geotask-state-machine`` 代码块
**逐条相等**，由 ``tests/test_geotask_contract.py`` 强制。
"""

from __future__ import annotations

from .model import TaskState

__all__ = [
    "INITIAL_STATE",
    "TERMINAL_STATES",
    "TRANSITIONS",
    "IllegalTransition",
    "assert_transition",
    "can_transition",
]


class IllegalTransition(Exception):
    """试图做一个状态机不允许的迁移。"""

    def __init__(self, src: TaskState, dst: TaskState) -> None:
        super().__init__(f"非法状态迁移: {src.value} → {dst.value}")
        self.src = src
        self.dst = dst


INITIAL_STATE = TaskState.CREATED

#: 终态必须吸收：没有任何出边。
TERMINAL_STATES = frozenset(
    {
        TaskState.SUCCEEDED,
        TaskState.FAILED,
        TaskState.CANCELLED,
        TaskState.SKIPPED,
    }
)

#: 允许的迁移。顺序与契约文档一致，便于人眼对照。
TRANSITIONS = frozenset(
    {
        (TaskState.CREATED, TaskState.QUEUED),
        (TaskState.CREATED, TaskState.CANCELLED),
        (TaskState.QUEUED, TaskState.SCHEDULED),
        (TaskState.QUEUED, TaskState.BLOCKED),
        (TaskState.QUEUED, TaskState.CANCELLED),
        (TaskState.BLOCKED, TaskState.QUEUED),
        (TaskState.BLOCKED, TaskState.FAILED),
        (TaskState.BLOCKED, TaskState.SKIPPED),
        (TaskState.BLOCKED, TaskState.CANCELLED),
        (TaskState.SCHEDULED, TaskState.RUNNING),
        (TaskState.SCHEDULED, TaskState.QUEUED),
        (TaskState.SCHEDULED, TaskState.FAILED),
        (TaskState.SCHEDULED, TaskState.CANCELLED),
        (TaskState.RUNNING, TaskState.SUCCEEDED),
        (TaskState.RUNNING, TaskState.FAILED),
        (TaskState.RUNNING, TaskState.CANCELLED),
        (TaskState.RUNNING, TaskState.QUEUED),
    }
)


def can_transition(src: TaskState, dst: TaskState) -> bool:
    return (src, dst) in TRANSITIONS


def assert_transition(src: TaskState, dst: TaskState) -> None:
    """允许则返回，否则抛 :class:`IllegalTransition`。"""
    if not can_transition(src, dst):
        raise IllegalTransition(src, dst)
