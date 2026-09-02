"""Cutting an oversized task into slices the executing model can actually hold.

Pure arithmetic — no database, no gateway, no repository. The assertion that
matters most here is that the union of the slices is *exactly* the parent's
scope: a file dropped during the split is a requirement that silently never gets
implemented, and the mission reports success anyway.
"""

from __future__ import annotations

import pytest

from daino.config.models import ModelProfileConfig
from daino.context.profiles import CapabilityEnvelope, ModelExecutionProfile
from daino.planning import measure_scope, split_task, validate_task_graph
from daino.planning.sizing import _NEW_FILE_TOKENS
from daino.schemas import ProjectMode, TaskPlan, TaskSpec, TaskStatus


def _envelope(
    *, files: int = 3, tokens: int = 6_000, name: str = "local-ollama"
) -> CapabilityEnvelope:
    """Built directly rather than resolved, so the limits under test are exact."""
    return CapabilityEnvelope(
        profile_name=name,
        compact=False,
        one_action_per_turn=False,
        max_steps=None,
        working_headroom_tokens=tokens,
        source_tokens=tokens,
        max_files_per_task=files,
        task_source_budget_tokens=tokens,
    )


def _task(
    *,
    expected: list[str] | None = None,
    allowed: list[str] | None = None,
    dependencies: list[str] | None = None,
    commands: list[str] | None = None,
) -> TaskSpec:
    return TaskSpec(
        id="task-parent",
        title="Add invoicing",
        objective="Add invoicing across the service",
        dependencies=dependencies or [],
        expected_files=expected or [],
        allowed_files=allowed or [],
        acceptance_criteria=["invoices are produced", "totals are correct"],
        verification_commands=commands or ["pytest"],
    )


def _sizes(**kwargs: int) -> dict[str, int]:
    """Sizes in bytes, as the repository index stores them."""
    return dict(kwargs)


# ------------------------------------------------------------------ measuring


def test_a_task_that_fits_is_not_split() -> None:
    task = _task(expected=["a.py", "b.py"])
    sizes = {"a.py": 4_000, "b.py": 4_000}

    slices, needs_replan = split_task(task, sizes, _envelope())

    assert slices == []
    assert needs_replan is False


def test_a_file_the_task_will_create_is_not_free() -> None:
    """Otherwise a task creating twenty new files measures as empty."""
    task = _task(expected=[f"new_{index}.py" for index in range(20)])

    measurement = measure_scope(task, {})

    assert measurement.tokens == 20 * _NEW_FILE_TOKENS
    assert not measurement.fits(_envelope())


def test_a_glob_is_charged_but_not_packed() -> None:
    """`tests/**` is a permission, not a work item — and it is not free either."""
    task = _task(expected=["service.py"], allowed=["service.py", "tests/**"])

    measurement = measure_scope(task, {"service.py": 400})

    assert measurement.paths == ["service.py"]
    assert measurement.patterns == ["tests/**"]
    assert measurement.tokens == 100 + _NEW_FILE_TOKENS


# ------------------------------------------------------------------- splitting


def test_every_slice_fits_both_limits() -> None:
    task = _task(expected=[f"mod_{index}.py" for index in range(9)])
    sizes = {f"mod_{index}.py": 8_000 for index in range(9)}
    envelope = _envelope(files=3, tokens=6_000)

    slices, _ = split_task(task, sizes, envelope)

    assert len(slices) > 1
    for spec in slices:
        assert measure_scope(spec, sizes).fits(envelope)


def test_the_union_of_the_slices_is_exactly_the_parent_scope() -> None:
    """The most important assertion in this module.

    A path dropped by the split is a requirement nobody implements and nobody
    notices: the slices all pass, the mission reports success, and the file the
    change hinged on was never opened.
    """
    task = _task(
        expected=["a.py", "b.py", "c.py"],
        allowed=["a.py", "d.py", "e.py", "f.py"],
    )
    sizes = _sizes(**{name: 20_000 for name in ("a.py", "b.py", "c.py", "d.py", "e.py", "f.py")})

    slices, _ = split_task(task, sizes, _envelope(files=2, tokens=6_000))

    covered = [path for spec in slices for path in measure_scope(spec, sizes).paths]
    assert sorted(set(covered)) == ["a.py", "b.py", "c.py", "d.py", "e.py", "f.py"]
    # And exactly once each — a file in two slices is edited twice, by two agents
    # that cannot see each other's work.
    assert len(covered) == len(set(covered))


def test_the_planner_s_ordering_survives_the_split() -> None:
    """Packing by descending size would reorder the implementation sequence."""
    task = _task(expected=["first.py", "second.py", "third.py", "fourth.py"])
    sizes = _sizes(**{"first.py": 100, "second.py": 90_000, "third.py": 100, "fourth.py": 100})

    slices, _ = split_task(task, sizes, _envelope(files=2, tokens=6_000))

    order = [path for spec in slices for path in spec.expected_files]
    assert order == ["first.py", "second.py", "third.py", "fourth.py"]


def test_slices_run_one_after_another() -> None:
    task = _task(expected=[f"mod_{index}.py" for index in range(6)], dependencies=["task-earlier"])
    sizes = {f"mod_{index}.py": 20_000 for index in range(6)}

    slices, _ = split_task(task, sizes, _envelope(files=2, tokens=6_000))

    assert slices[0].dependencies == ["task-earlier"]
    for previous, current in zip(slices, slices[1:], strict=False):
        assert previous.id in current.dependencies


def test_slice_ids_sort_into_execution_order_past_nine() -> None:
    """`validate_task_graph` orders ready tasks by id, so padding is load-bearing.

    Unpadded, slice 10 sorts before slice 2 and the change is applied out of the
    order its own dependencies describe.
    """
    task = _task(expected=[f"mod_{index:02d}.py" for index in range(12)])
    sizes = {f"mod_{index:02d}.py": 20_000 for index in range(12)}

    slices, _ = split_task(task, sizes, _envelope(files=1, tokens=6_000))

    assert len(slices) == 12
    assert [spec.id for spec in slices] == sorted(spec.id for spec in slices)


def test_the_slices_form_a_valid_graph_with_a_remapped_dependent() -> None:
    """What the mission loop inserts has to survive the validator it inserts into."""
    task = _task(expected=[f"mod_{index}.py" for index in range(6)])
    sizes = {f"mod_{index}.py": 20_000 for index in range(6)}
    slices, _ = split_task(task, sizes, _envelope(files=2, tokens=6_000))
    downstream = _task(expected=["later.py"]).model_copy(
        update={"id": "task-later", "dependencies": [slices[-1].id]}
    )

    ordered = validate_task_graph(
        TaskPlan(summary="s", mode=ProjectMode.DIRECT, tasks=[*slices, downstream])
    )

    assert [spec.id for spec in ordered] == [*(spec.id for spec in slices), "task-later"]


# ---------------------------------------------------- what a slice inherits


def test_only_the_final_slice_verifies() -> None:
    """A third of a change fails the whole change's tests, correctly and uselessly."""
    task = _task(expected=[f"mod_{index}.py" for index in range(6)], commands=["pytest -q"])
    sizes = {f"mod_{index}.py": 20_000 for index in range(6)}

    slices, _ = split_task(task, sizes, _envelope(files=2, tokens=6_000))

    assert [spec.verification_commands for spec in slices[:-1]] == [[]] * (len(slices) - 1)
    assert slices[-1].verification_commands == ["pytest -q"]
    # The parent's acceptance criteria go with the verification, for the same
    # reason: they describe the finished change, not a third of it.
    assert slices[-1].acceptance_criteria == task.acceptance_criteria
    assert slices[0].acceptance_criteria != task.acceptance_criteria


def test_each_slice_is_told_it_is_only_a_part() -> None:
    """Without this every slice reads the parent objective and does the whole job."""
    task = _task(expected=["a.py", "b.py", "c.py", "d.py"])
    # 5,000 tokens each against a 6,000 budget, so no two share a slice.
    sizes = {name: 20_000 for name in ("a.py", "b.py", "c.py", "d.py")}

    slices, _ = split_task(task, sizes, _envelope(files=2, tokens=6_000))

    assert "part 1 of 4" in slices[0].objective
    assert "a.py" in slices[0].objective
    assert "do not attempt the whole change here" in slices[0].objective
    # The parent's own objective is still there — the slice needs the goal too.
    assert task.objective in slices[0].objective


def test_a_slice_never_gains_permissions_the_parent_lacked() -> None:
    """An empty `allowed_files` means "anything" to the editing tools."""
    task = _task(
        expected=["a.py", "b.py", "c.py", "d.py"],
        allowed=["a.py", "b.py", "c.py", "d.py", "tests/**"],
    )
    sizes = {name: 20_000 for name in ("a.py", "b.py", "c.py", "d.py")}

    slices, _ = split_task(task, sizes, _envelope(files=3, tokens=6_000))

    for spec in slices:
        assert spec.allowed_files, "an empty scope would hand the slice the whole repository"
        assert set(spec.allowed_files) <= set(task.allowed_files)
        # The glob rides along in every slice: a slice that lost the right to
        # write tests could not run the verification it was given.
        assert "tests/**" in spec.allowed_files


def test_an_unrestricted_parent_produces_unrestricted_slices() -> None:
    task = _task(expected=["a.py", "b.py", "c.py", "d.py"])
    sizes = {name: 20_000 for name in ("a.py", "b.py", "c.py", "d.py")}

    slices, _ = split_task(task, sizes, _envelope(files=2, tokens=6_000))

    assert all(spec.allowed_files == [] for spec in slices)
    assert all(spec.expected_files for spec in slices)


def test_a_slice_records_the_model_it_was_cut_for() -> None:
    task = _task(expected=["a.py", "b.py", "c.py", "d.py"])
    sizes = {name: 20_000 for name in ("a.py", "b.py", "c.py", "d.py")}

    slices, _ = split_task(task, sizes, _envelope(files=2, tokens=6_000, name="local-ollama"))

    assert all(spec.assigned_model == "local-ollama" for spec in slices)
    assert all(spec.status is TaskStatus.PENDING for spec in slices)
    assert all(spec.attempt_count == 0 for spec in slices)


# ------------------------------------------------------------- terminating


def test_a_task_that_cannot_shrink_is_not_split_into_a_copy_of_itself() -> None:
    """One group means no split. A copy under a new id is an infinite loop."""
    task = _task(expected=["huge.py"])
    sizes = {"huge.py": 4_000_000}

    slices, needs_replan = split_task(task, sizes, _envelope(files=3, tokens=6_000))

    assert slices == []
    # Arithmetic is out of moves; splitting inside a file is a semantic call.
    assert needs_replan is True


def test_an_oversized_file_among_others_is_isolated_and_flagged() -> None:
    task = _task(expected=["small.py", "enormous.py", "other.py"])
    sizes = _sizes(**{"small.py": 400, "enormous.py": 4_000_000, "other.py": 400})

    slices, needs_replan = split_task(task, sizes, _envelope(files=3, tokens=6_000))

    isolated = [spec for spec in slices if "enormous.py" in spec.expected_files]
    assert len(isolated) == 1
    assert isolated[0].expected_files == ["enormous.py"]
    assert needs_replan is True


def test_a_split_slice_remembers_the_root_it_came_from() -> None:
    """The generation cap is a dict lookup on this, not id parsing."""
    task = _task(expected=["a.py", "b.py", "c.py", "d.py"])
    sizes = {name: 20_000 for name in ("a.py", "b.py", "c.py", "d.py")}

    first, _ = split_task(task, sizes, _envelope(files=2, tokens=6_000))
    again, _ = split_task(first[0], sizes, _envelope(files=1, tokens=6_000), generation=2)

    assert all(spec.slice_of == "task-parent" for spec in first)
    # The root, not the immediate parent, so the cap counts splits of one task.
    assert all(spec.slice_of == "task-parent" for spec in again)
    assert all("-s2-" in spec.id for spec in again)


@pytest.mark.parametrize("files,tokens", [(1, 1), (1, 6_000), (8, 130_000)])
def test_any_envelope_produces_slices_that_fit_it(files: int, tokens: int) -> None:
    task = _task(expected=[f"mod_{index}.py" for index in range(7)])
    sizes = {f"mod_{index}.py": 20_000 for index in range(7)}
    envelope = _envelope(files=files, tokens=tokens)

    slices, _ = split_task(task, sizes, envelope)

    for spec in slices:
        assert len(spec.expected_files) <= envelope.max_files_per_task


def test_a_resolved_envelope_splits_the_field_case() -> None:
    """End to end against real sizing: the 32k window from the field report."""
    profile = ModelExecutionProfile.resolve(
        "local-ollama",
        ModelProfileConfig(provider="local", model="qwen3", local=True, context_window=32_768),
        input_budget_tokens=18_902,
        project_budget_tokens=24_000,
        memory_items=8,
        memory_tokens=2_000,
    )
    envelope = CapabilityEnvelope.from_profile(profile)
    # A 478-line app.py plus two collaborators — an ordinary planned task, and
    # one the builder in the field report could not hold in a single turn.
    task = _task(expected=["app.py", "models.py", "routes.py"])
    sizes = _sizes(**{"app.py": 19_000, "models.py": 12_000, "routes.py": 12_000})

    slices, needs_replan = split_task(task, sizes, envelope)

    assert [spec.expected_files for spec in slices] == [
        ["app.py"],
        ["models.py"],
        ["routes.py"],
    ]
    assert measure_scope(slices[1], sizes).fits(envelope)
    assert measure_scope(slices[2], sizes).fits(envelope)
    # app.py alone is 4,750 tokens against a 4,737 budget: this window cannot
    # hold that file whole, however the files are arranged. Arithmetic is out of
    # moves and says so rather than pretending the slice fits.
    assert not measure_scope(slices[0], sizes).fits(envelope)
    assert needs_replan is True
