"""Measuring a task against the executing model, and cutting it down to fit.

Pure arithmetic over a task's declared file scope: no gateway, no repository
reads, no I/O of any kind. Everything here is decided from the ``TaskSpec``, a
map of path to size in bytes (which the repository index already holds), and the
``CapabilityEnvelope`` of the model that will run the task.

Splitting is deterministic on purpose. Asking a model to re-plan its own
oversized task is the obvious approach and the wrong default: it is another
round trip on the model that just demonstrated it cannot hold the work, and its
answer cannot be checked against the parent's scope without doing this
arithmetic anyway. The model is kept as a fallback for the one case arithmetic
cannot decide — a single file that exceeds the whole per-task budget, where the
split has to run *through* the file along semantic boundaries rather than
between files. See ``needs_model_replan``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from daino.context.profiles import CapabilityEnvelope
from daino.schemas import TaskSpec, TaskStatus

#: What a path not yet on disk is assumed to cost. A task creating twenty files
#: has to split even though every one of them currently measures zero, so a new
#: file is charged roughly what a small module costs rather than nothing.
_NEW_FILE_TOKENS = 1_500

#: The same estimator ``ContextCompiler._estimate_tokens`` uses. Stated as a
#: constant so the two cannot drift: a splitter that measured a task cheaper
#: than the compiler packs it would pass through tasks that then overflow.
_BYTES_PER_TOKEN = 4


@dataclass(frozen=True)
class ScopeMeasurement:
    """What a task's declared scope costs the model that has to hold it.

    ``paths`` are concrete files, which is what gets packed. ``patterns`` are
    glob entries like ``tests/**``: they are permissions rather than work items,
    so they are charged against the budget but replicated into every slice
    instead of being assigned to one — a slice that lost the right to write
    tests could not run the verification it was given.
    """

    paths: list[str] = field(default_factory=list)
    patterns: list[str] = field(default_factory=list)
    tokens: int = 0
    per_path: dict[str, int] = field(default_factory=dict)

    @property
    def entries(self) -> int:
        return len(self.paths) + len(self.patterns)

    def fits(self, envelope: CapabilityEnvelope) -> bool:
        return (
            self.entries <= envelope.max_files_per_task
            and self.tokens <= envelope.task_source_budget_tokens
        )


def measure_scope(task: TaskSpec, sizes: dict[str, int]) -> ScopeMeasurement:
    """Cost a task's file scope in tokens.

    Order is the planner's, preserved through ``dict.fromkeys``: a task's file
    list usually encodes the sequence the change is meant to happen in, and
    reordering it produces slices whose dependencies say the wrong thing.
    """
    scope = list(dict.fromkeys([*task.expected_files, *task.allowed_files]))
    paths = [path for path in scope if "*" not in path]
    patterns = [path for path in scope if "*" in path]
    per_path = {path: _cost(path, sizes) for path in paths}
    return ScopeMeasurement(
        paths=paths,
        patterns=patterns,
        tokens=sum(per_path.values()) + _NEW_FILE_TOKENS * len(patterns),
        per_path=per_path,
    )


def _cost(path: str, sizes: dict[str, int]) -> int:
    # A path absent from the index is one the task will create. Charging it zero
    # lets a task that creates twenty files measure as empty and never split.
    if path not in sizes:
        return _NEW_FILE_TOKENS
    return max(1, sizes[path] // _BYTES_PER_TOKEN)


def split_task(
    task: TaskSpec,
    sizes: dict[str, int],
    envelope: CapabilityEnvelope,
    *,
    generation: int = 1,
) -> tuple[list[TaskSpec], bool]:
    """Cut *task* into slices each of which fits *envelope*.

    Returns the slices, and whether a model re-plan is still needed. An empty
    list means the task cannot usefully be made smaller — it already fits, or it
    is a single path that no arrangement of files can shrink. Returning a
    one-element list would be a copy of the parent under a new id, which is how
    a splitter loops forever.
    """
    measurement = measure_scope(task, sizes)
    if measurement.fits(envelope) or len(measurement.paths) <= 1:
        # A single oversized path is flagged rather than split: cutting inside a
        # file is a semantic judgement, not an arithmetic one.
        needs_replan = len(measurement.paths) == 1 and not measurement.fits(envelope)
        return [], needs_replan

    groups = _pack(measurement, envelope)
    if len(groups) <= 1:
        return [], False
    root = task.slice_of or task.id
    slices = [
        _slice(
            task,
            measurement,
            group,
            index,
            len(groups),
            generation=generation,
            root=root,
            envelope=envelope,
        )
        for index, group in enumerate(groups)
    ]
    # Sequential, not parallel: this is one coherent change in one worktree, and
    # slices running concurrently would edit each other's files.
    for previous, current in zip(slices, slices[1:], strict=False):
        current.dependencies.append(previous.id)
    needs_replan = any(
        len(group) == 1 and measurement.per_path[group[0]] > envelope.task_source_budget_tokens
        for group in groups
    )
    return slices, needs_replan


def _pack(measurement: ScopeMeasurement, envelope: CapabilityEnvelope) -> list[list[str]]:
    """Greedy packing in the planner's order — not first-fit-decreasing.

    The textbook bin-packing improvement is to sort by descending size, and it
    is wrong here. These are implementation steps: reordering them so the
    largest file leads produces slices whose sequential dependencies describe a
    build order nobody intended.
    """
    # The patterns ride along in every slice, so their cost is charged to every
    # slice too. Without this a scope of `service.py` plus `tests/**` could pack
    # into groups that each overflow once the pattern's allowance is added back.
    reserved = _NEW_FILE_TOKENS * len(measurement.patterns)
    file_limit = max(1, envelope.max_files_per_task - len(measurement.patterns))
    token_limit = max(1, envelope.task_source_budget_tokens - reserved)
    groups: list[list[str]] = []
    current: list[str] = []
    used = 0
    for path in measurement.paths:
        cost = measurement.per_path[path]
        over_files = len(current) + 1 > file_limit
        over_budget = used + cost > token_limit
        if current and (over_files or over_budget):
            groups.append(current)
            current, used = [], 0
        current.append(path)
        used += cost
    if current:
        groups.append(current)
    return groups


def _slice(
    task: TaskSpec,
    measurement: ScopeMeasurement,
    group: list[str],
    index: int,
    total: int,
    *,
    generation: int,
    root: str,
    envelope: CapabilityEnvelope,
) -> TaskSpec:
    final = index == total - 1
    # Intersected with the parent's own lists so a slice is granted exactly the
    # permissions the parent had — never more. An empty `allowed_files` means
    # "anything" to the editing tools, so widening here would quietly hand a
    # slice the run of the repository.
    expected = [path for path in task.expected_files if path in group]
    allowed = [path for path in task.allowed_files if path in group]
    if allowed or task.allowed_files:
        allowed = [*allowed, *measurement.patterns]
    listing = ", ".join(group)
    return task.model_copy(
        update={
            # Zero-padded because `validate_task_graph` orders ready tasks by id:
            # unpadded, slice 10 would sort before slice 2 and run out of order.
            "id": f"{task.id}-s{generation}-{index + 1:02d}",
            "title": f"{task.title} ({index + 1}/{total})",
            # Load-bearing, not decorative. Without it every slice reads the
            # parent objective and attempts the whole job, which is the failure
            # the split was supposed to prevent, now happening N times over.
            "objective": (
                f"{task.objective}\n\n"
                f"This is part {index + 1} of {total} of a larger change and covers only "
                f"these files: {listing}. The remaining parts are handled by sibling tasks — "
                f"do not edit files outside this list, and do not attempt the whole change here."
            ),
            "expected_files": expected,
            "allowed_files": allowed,
            "dependencies": list(task.dependencies),
            # Only the last slice verifies. Running the parent's commands against
            # a third of the change asserts a failure and then reports it as one.
            "verification_commands": list(task.verification_commands) if final else [],
            "acceptance_criteria": (
                list(task.acceptance_criteria)
                if final
                else [f"The changes to {listing} are complete and syntactically valid."]
            ),
            "slice_of": root,
            # The column exists on every task and has never been written by
            # anything. A slice is the one case where the answer is known for
            # certain: it was cut to this profile's measurements.
            "assigned_model": envelope.profile_name,
            "attempt_count": 0,
            "evidence": [],
            "status": TaskStatus.PENDING,
        }
    )
