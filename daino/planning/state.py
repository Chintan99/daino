"""Strict task transition validation."""

from daino.schemas import TaskStatus

TRANSITIONS = {
    TaskStatus.PENDING: {TaskStatus.READY, TaskStatus.CANCELLED},
    TaskStatus.READY: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {
        TaskStatus.BLOCKED,
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.VERIFYING,
        TaskStatus.FAILED,
    },
    TaskStatus.AWAITING_APPROVAL: {
        TaskStatus.RUNNING,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
    },
    TaskStatus.VERIFYING: {
        TaskStatus.RUNNING,
        TaskStatus.REVIEWING,
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
    },
    TaskStatus.REVIEWING: {
        TaskStatus.COMPLETED,
        TaskStatus.RUNNING,
        TaskStatus.FAILED,
    },
    TaskStatus.BLOCKED: {TaskStatus.READY, TaskStatus.CANCELLED},
    TaskStatus.FAILED: {TaskStatus.READY, TaskStatus.CANCELLED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.CANCELLED: set(),
}


def validate_transition(current: TaskStatus, target: TaskStatus) -> None:
    if target not in TRANSITIONS[current]:
        raise ValueError(f"Invalid task transition: {current.value} -> {target.value}")
