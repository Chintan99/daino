"""Sizing the context so the agent has room to work in it.

These guard one invariant, and it is the whole point of the module:

    the scaffolding compaction re-adds on every pass must leave enough of the
    compaction threshold free to hold a source file.

The failure it prevents is silent and looks like a model problem. Scaffolding —
instructions, retrieved memory, bundled sources — used to be sized as a fraction
of the *input budget* while compaction fires at a fraction of that same budget.
On a 32k window that came out as 14,000 tokens of scaffolding under a
15,121-token threshold: 1,121 tokens for everything the agent actually did. One
``read_file`` of a 6k-token file overshot it, compaction shed the only thing it
could — the transcript, including that read — and the agent, having lost the
file, read it again. Three repeats, and the no-progress guard ended the run with
"the model is stuck, rephrase the request". No rephrasing could have fixed it.
"""

from __future__ import annotations

import pytest

from daino.config.models import ModelProfileConfig
from daino.context.profiles import (
    _MAX_FILES_PER_TASK_CEILING,
    _MIN_WORKING_FRACTION,
    TYPICAL_SOURCE_FILE_TOKENS,
    CapabilityEnvelope,
    ExecutionMode,
    ModelExecutionProfile,
)

#: Imported rather than redeclared: a file this size is ordinary in a real
#: project — the one in the field report was 478 lines of Python — and the
#: sizing logic and the assertion guarding it must not be able to drift.
assert TYPICAL_SOURCE_FILE_TOKENS == 6_000


def _profile(
    *,
    context_window: int,
    local: bool = False,
    execution_mode: str = "auto",
    initial_context_tokens: int = 0,
    max_output_tokens: int = 0,
) -> ModelProfileConfig:
    return ModelProfileConfig(
        provider="test",
        model="test-model",
        context_window=context_window,
        local=local,
        execution_mode=execution_mode,
        initial_context_tokens=initial_context_tokens,
        max_output_tokens=max_output_tokens,
    )


def _resolve(
    model: ModelProfileConfig,
    *,
    input_budget_tokens: int,
    compaction_threshold: float = 0.80,
    project_budget_tokens: int = 24_000,
    memory_tokens: int = 2_000,
) -> ModelExecutionProfile:
    return ModelExecutionProfile.resolve(
        "test-profile",
        model,
        input_budget_tokens=input_budget_tokens,
        project_budget_tokens=project_budget_tokens,
        memory_items=8,
        memory_tokens=memory_tokens,
        compaction_threshold=compaction_threshold,
    )


def _scaffold(profile: ModelExecutionProfile) -> int:
    """What compaction re-adds every pass, and so what the agent never gets."""
    return profile.instruction_tokens + profile.source_tokens + profile.memory_tokens


def _threshold(profile: ModelExecutionProfile, fraction: float = 0.80) -> int:
    return int(profile.input_budget_tokens * fraction)


# --------------------------------------------------------------- the invariant


@pytest.mark.parametrize(
    "context_window,input_budget",
    [
        (16_384, 9_000),
        (32_768, 18_902),  # the window and budget from the field report
        (32_768, 24_000),
        (128_000, 96_000),
        (200_000, 150_000),
        (1_310_720, 400_000),
    ],
)
def test_scaffolding_always_leaves_room_to_work(context_window: int, input_budget: int) -> None:
    """The regression. Scaffolding is sized against the threshold it fits under."""
    profile = _resolve(_profile(context_window=context_window), input_budget_tokens=input_budget)

    limit = _threshold(profile)
    headroom = limit - _scaffold(profile)

    assert headroom > 0, "the scaffolding alone exceeded the compaction threshold"
    # Not merely positive: enough to hold a real file, or compaction evicts the
    # read that put it there and the agent loops.
    assert headroom >= int(limit * _MIN_WORKING_FRACTION) - 1
    if limit >= 12_000:
        assert headroom >= TYPICAL_SOURCE_FILE_TOKENS


def test_the_exact_field_case_now_fits_a_source_file() -> None:
    """The numbers from the run that failed, asserted directly.

    Before: 14,000 of scaffolding under a 15,121 threshold — 1,121 of room for a
    6,000-token file.
    """
    profile = _resolve(_profile(context_window=32_768), input_budget_tokens=18_902)

    limit = _threshold(profile)
    headroom = limit - _scaffold(profile)

    assert limit == 15_121
    assert _scaffold(profile) < 14_000
    assert headroom >= TYPICAL_SOURCE_FILE_TOKENS


@pytest.mark.parametrize("threshold", [0.5, 0.65, 0.8, 0.95, 1.0])
def test_the_invariant_holds_at_every_configured_threshold(threshold: float) -> None:
    """A stricter compaction threshold must tighten the scaffolding with it.

    An operator lowering `memory.compaction_threshold` is asking compaction to
    fire sooner; if the scaffolding did not shrink to match, they would be
    re-creating the stall by turning a knob that looks unrelated to it.
    """
    profile = _resolve(
        _profile(context_window=32_768),
        input_budget_tokens=18_902,
        compaction_threshold=threshold,
    )

    limit = _threshold(profile, threshold)

    assert _scaffold(profile) < limit
    assert limit - _scaffold(profile) >= int(limit * _MIN_WORKING_FRACTION) - 1


def test_a_tiny_window_still_produces_a_usable_profile() -> None:
    """The floors must not collapse to zero or go negative."""
    profile = _resolve(_profile(context_window=8_192), input_budget_tokens=2_000)

    assert profile.instruction_tokens >= 256
    assert profile.source_tokens >= 512
    # Compact mode's own floor is 128, not the 256 the standard path uses.
    assert profile.memory_tokens >= 128
    assert profile.initial_context_tokens >= 512
    assert profile.mode is ExecutionMode.COMPACT


# ------------------------------------------------------------- mode selection


def test_a_constrained_window_selects_compact_mode() -> None:
    assert (
        _resolve(_profile(context_window=16_384), input_budget_tokens=9_000).mode
        is ExecutionMode.COMPACT
    )


def test_neutral_capability_scores_are_not_evidence_of_a_weak_model() -> None:
    """A freshly configured local model must not be forced into compact mode.

    Scores default to neutral before anyone rates a model, and treating that
    default as a signal put a 27B coder on an 8k budget.
    """
    roomy_local = _profile(context_window=128_000, local=True)

    profile = _resolve(roomy_local, input_budget_tokens=96_000)

    assert profile.mode is ExecutionMode.STANDARD


def test_scores_set_below_neutral_do_select_compact_mode() -> None:
    weak = _profile(context_window=128_000, local=True)
    weak = weak.model_copy(update={"coding_score": 2})

    assert _resolve(weak, input_budget_tokens=96_000).mode is ExecutionMode.COMPACT


def test_an_explicit_mode_overrides_the_heuristics() -> None:
    forced = _profile(context_window=200_000, execution_mode="compact")

    assert _resolve(forced, input_budget_tokens=150_000).mode is ExecutionMode.COMPACT


# ------------------------------------------------------------------- overrides


def test_an_operator_set_initial_budget_is_still_capped_by_the_invariant() -> None:
    """Honouring it verbatim would let a single setting re-open the stall."""
    generous = _profile(context_window=32_768, initial_context_tokens=30_000)

    profile = _resolve(generous, input_budget_tokens=18_902)

    limit = _threshold(profile)
    assert profile.initial_context_tokens <= limit
    assert limit - _scaffold(profile) >= TYPICAL_SOURCE_FILE_TOKENS


def test_the_instruction_to_source_ratio_survives_being_scaled_down() -> None:
    """Scaled, not truncated: each mode keeps the emphasis it chose.

    Standard mode weights sources over instructions 3:1. Squeezing the bundle
    must not quietly invert that.
    """
    profile = _resolve(_profile(context_window=32_768), input_budget_tokens=18_902)

    assert profile.mode is ExecutionMode.STANDARD
    assert profile.source_tokens > profile.instruction_tokens
    ratio = profile.source_tokens / profile.instruction_tokens
    assert 2.5 <= ratio <= 3.5


def test_memory_is_squeezed_before_the_bundle_is() -> None:
    """Retrieved memory is the most expendable of the three."""
    profile = _resolve(
        _profile(context_window=32_768),
        input_budget_tokens=18_902,
        memory_tokens=12_000,
    )

    limit = _threshold(profile)
    assert profile.memory_tokens < 12_000
    assert limit - _scaffold(profile) >= TYPICAL_SOURCE_FILE_TOKENS


# ------------------------------------------------------ diagnosing the stall


def _outcome(compactions: int, *, stop_reason: str = "stall") -> object:
    from daino.agents.loop import BuilderOutcome
    from daino.schemas import Implementation

    return BuilderOutcome(
        implementation=Implementation(
            summary="Stopped after 3 strategy corrections failed to make progress.",
            modifications=[],
        ),
        changed=[],
        steps=9,
        completed=False,
        stop_reason=stop_reason,
        compactions=compactions,
    )


def test_a_stall_behind_heavy_compaction_is_diagnosed_as_a_window_problem() -> None:
    """The advice has to match the cause.

    Told to "rephrase more concretely", the user rewords a prompt that was never
    the problem — the agent was losing what it read to compaction. Naming the
    real cause is the difference between a fixable report and a dead end.
    """
    from daino.agents.loop import describe_incomplete_outcome

    message = describe_incomplete_outcome(_outcome(6))

    assert "compacted 6 times" in message
    assert "window problem rather than a wording one" in message
    assert "larger context window" in message
    # And it must not also give the advice for the other cause.
    assert "Rephrasing the task" not in message


def test_a_stall_without_compaction_still_advises_rephrasing() -> None:
    """A genuinely stuck model is a different problem with different advice."""
    from daino.agents.loop import describe_incomplete_outcome

    message = describe_incomplete_outcome(_outcome(1))

    assert "Rephrasing the task" in message
    assert "window problem" not in message


def test_a_step_budget_stop_is_never_confused_with_either() -> None:
    from daino.agents.loop import describe_incomplete_outcome

    message = describe_incomplete_outcome(_outcome(9, stop_reason="step_budget"))

    assert "step limit" in message
    assert "window problem" not in message
    assert "Rephrasing the task" not in message


# ------------------------------------------------- describing it to a planner


def _envelope(
    *, context_window: int = 32_768, input_budget: int = 18_902, **kwargs: object
) -> CapabilityEnvelope:
    profile = _resolve(
        _profile(context_window=context_window, **kwargs),  # type: ignore[arg-type]
        input_budget_tokens=input_budget,
    )
    return CapabilityEnvelope.from_profile(profile)


@pytest.mark.parametrize(
    "context_window,input_budget",
    [
        (16_384, 9_000),
        (32_768, 18_902),
        (32_768, 24_000),
        (128_000, 96_000),
        (200_000, 150_000),
        (1_310_720, 400_000),
    ],
)
def test_the_envelope_names_the_existing_headroom_rather_than_a_second_one(
    context_window: int, input_budget: int
) -> None:
    """It must be the same arithmetic the invariant above is stated in.

    If the envelope computed headroom its own way, the planner could be told a
    task fits while the sizing that packs it disagrees — which is the original
    stall with an extra step.
    """
    profile = _resolve(_profile(context_window=context_window), input_budget_tokens=input_budget)

    envelope = CapabilityEnvelope.from_profile(profile)

    assert envelope.working_headroom_tokens == _threshold(profile) - _scaffold(profile)


def test_the_envelope_shrinks_with_the_window() -> None:
    """Monotonic, or a bigger model would be planned smaller tasks."""
    budgets = [2_000, 9_000, 18_902, 96_000, 150_000]
    envelopes = [
        _envelope(context_window=max(8_192, budget * 2), input_budget=budget) for budget in budgets
    ]

    files = [envelope.max_files_per_task for envelope in envelopes]
    sources = [envelope.task_source_budget_tokens for envelope in envelopes]

    assert files == sorted(files)
    assert sources == sorted(sources)


def test_every_envelope_admits_at_least_one_file() -> None:
    """A task that may contain no files is not a task.

    Even on a window too narrow to hold one whole file the answer is "one, and
    the splitter will deal with the overflow", not zero.
    """
    tiny = _envelope(context_window=8_192, input_budget=2_000)

    assert tiny.max_files_per_task >= 1
    assert tiny.task_source_budget_tokens >= 1


def test_a_compact_profile_is_capped_by_what_it_would_actually_pack() -> None:
    """Planning for files the packer is guaranteed to drop is planning for nothing."""
    profile = _resolve(_profile(context_window=16_384), input_budget_tokens=9_000)

    envelope = CapabilityEnvelope.from_profile(profile)

    assert profile.max_source_files is not None
    assert envelope.max_files_per_task <= profile.max_source_files
    assert envelope.compact is True


def test_a_huge_window_still_yields_a_reviewable_slice() -> None:
    """The file ceiling is a product judgement, not an arithmetic one."""
    roomy = _resolve(
        _profile(context_window=1_310_720),
        input_budget_tokens=400_000,
        project_budget_tokens=2_000_000,
    )

    envelope = CapabilityEnvelope.from_profile(roomy)

    assert envelope.max_files_per_task <= _MAX_FILES_PER_TASK_CEILING


def test_the_source_budget_never_exceeds_the_room_to_work_in() -> None:
    """The thrash case exactly: files that fit the bundle but not the headroom.

    The read succeeds, compaction sheds the transcript holding it, and the agent
    reads the same file again.
    """
    envelope = _envelope()

    assert envelope.task_source_budget_tokens <= envelope.working_headroom_tokens


def test_the_description_gives_a_planner_numbers_not_adjectives() -> None:
    """The repository summary already states file sizes; this is the other half."""
    rendered = _envelope().describe()

    assert str(_envelope().max_files_per_task) in rendered
    assert str(_envelope().task_source_budget_tokens) in rendered
    # Characters as well as tokens, because the inventory is rendered in bytes.
    assert "characters" in rendered
    for vague in ("small", "large", "a few", "several"):
        assert vague not in rendered.lower()


def test_a_one_action_per_turn_executor_says_so() -> None:
    forced = _resolve(
        _profile(context_window=200_000, execution_mode="compact"),
        input_budget_tokens=150_000,
    )

    rendered = CapabilityEnvelope.from_profile(forced).describe()

    assert "one action per turn" in rendered
