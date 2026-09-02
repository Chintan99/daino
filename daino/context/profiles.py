"""Model-aware execution policy and compact context adaptation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from daino.config.models import ModelProfileConfig
from daino.schemas import ContextBundle, TaskPacket

#: The value every capability score takes before an operator rates a model.
#: Scores at or above it carry no information either way.
_NEUTRAL_SCORE = 5

#: Below this usable input budget the agent cannot hold repository grounding and
#: a working transcript at once, so compact mode is the honest choice.
_COMPACT_BUDGET_TOKENS = 12_000

#: The share of the compaction threshold that must stay free for the transcript.
#:
#: This exists because of a specific, silent stall. The scaffolding compaction
#: re-adds every pass — instructions, retrieved memory, bundled sources — was
#: sized as a fraction of the *input budget*, while compaction fires at a
#: fraction of that same budget. On a 32k window that worked out to 14,000
#: tokens of scaffolding under a 15,121-token threshold: 1,121 tokens of room
#: for everything the agent actually did. One ``read_file`` of a 6k-token source
#: file blew straight through it, compaction shed the only thing it could — the
#: transcript, including that read — and the agent, having lost the file, read it
#: again. Three repeats and the no-progress guard killed the run with "the model
#: is stuck; rephrase the request", which no rephrasing could fix.
#:
#: So the scaffolding is now sized against the threshold it has to fit under,
#: and this is the floor it must leave behind.
_MIN_WORKING_FRACTION = 0.45
#: Retrieved memory is the most expendable of the three, so it is capped at a
#: modest share of the scaffolding before the bundle is squeezed at all.
_MEMORY_SHARE_OF_SCAFFOLD = 0.25

#: A source file this size is ordinary — the one in the field report was 478
#: lines of Python — so it is the unit a task's file count is measured in.
#: Defined here rather than in the test so the sizing and the assertion that
#: guards it cannot drift apart.
TYPICAL_SOURCE_FILE_TOKENS = 6_000

#: Beyond this a task stops being a reviewable vertical slice regardless of what
#: the window affords. A product judgement, not an arithmetic one: eight files
#: is already a large diff to read in one sitting, and a model with a million
#: tokens of room does not make a twenty-file task easier to check.
_MAX_FILES_PER_TASK_CEILING = 8


class ExecutionMode(StrEnum):
    STANDARD = "standard"
    COMPACT = "compact"


@dataclass(frozen=True)
class ModelExecutionProfile:
    """Concrete prompt and loop limits derived from a routed model profile."""

    profile_name: str
    mode: ExecutionMode
    input_budget_tokens: int
    initial_context_tokens: int
    instruction_tokens: int
    memory_items: int
    memory_tokens: int
    source_tokens: int
    max_source_files: int | None
    per_file_tokens: int | None
    recent_tool_groups: int
    max_steps: int | None
    no_progress_limit: int
    staged_retrieval: bool
    one_action_per_turn: bool

    @property
    def compact(self) -> bool:
        return self.mode == ExecutionMode.COMPACT

    @classmethod
    def resolve(
        cls,
        profile_name: str,
        model: ModelProfileConfig,
        *,
        input_budget_tokens: int,
        project_budget_tokens: int,
        memory_items: int,
        memory_tokens: int,
        compaction_threshold: float = 0.80,
    ) -> ModelExecutionProfile:
        # Capability scores default to the neutral value 5 when a provider is
        # first configured, so they are not evidence about any model — remote or
        # local. Treating neutral defaults as a small-model signal forced every
        # freshly configured local model (a 27B coder included) into compact
        # mode: 8k of context and one action per turn, which made long tasks
        # loop by re-doing work that had already been compacted away. Only
        # scores explicitly set *below* neutral count as evidence of a weak
        # model; otherwise the deciding factor is the objective one, namely how
        # much context the model actually leaves for the agent to work in.
        constrained_window = (
            model.context_window <= 16_384 or input_budget_tokens < _COMPACT_BUDGET_TOKENS
        )
        weak_local_model = model.local and (
            model.coding_score < _NEUTRAL_SCORE
            or min(model.tool_reliability, model.structured_reliability) < _NEUTRAL_SCORE
        )
        auto_compact = constrained_window or weak_local_model
        compact = model.execution_mode == "compact" or (
            model.execution_mode == "auto" and auto_compact
        )
        mode = ExecutionMode.COMPACT if compact else ExecutionMode.STANDARD
        # The initial bundle must not claim the whole input budget. It used to,
        # which left nothing for the system prompt, the transcript and the
        # scaffolding compaction re-adds — so on a 32k window the prompt started
        # over the compaction threshold and stayed there, compacting every turn
        # for no gain. Reserving a share for the working transcript only binds
        # narrow windows: a roomy model is already capped by the project budget
        # well below this fraction, so its grounding is unchanged.
        derived_initial = (
            8_192 if compact else max(_COMPACT_BUDGET_TOKENS, input_budget_tokens * 3 // 5)
        )
        derived_initial = min(input_budget_tokens, derived_initial)
        initial = model.initial_context_tokens or derived_initial
        initial = max(512, min(initial, input_budget_tokens, project_budget_tokens))
        if compact:
            instruction_tokens = max(256, min(1_000, initial // 5))
            selected_memory_tokens = max(128, min(memory_tokens, 768, initial // 8))
            selected_memory_items = min(memory_items, 4)
            source_tokens = max(512, min(4_500, initial * 5 // 8))
            max_source_files: int | None = 4
            per_file_tokens: int | None = 2_000
            recent_groups = 3
        else:
            instruction_tokens = max(256, initial // 4)
            selected_memory_tokens = memory_tokens
            selected_memory_items = memory_items
            source_tokens = max(512, initial * 3 // 4)
            max_source_files = None
            per_file_tokens = None
            recent_groups = 6
        # Everything above sized the scaffolding against the input budget. What
        # it must actually fit under is the *compaction threshold*, because that
        # is the point at which the transcript starts being thrown away — and
        # the scaffolding is re-added in full on every pass, so whatever it
        # takes is permanently unavailable to the agent's working context.
        #
        # Without this the two references disagreed by 20% and the scaffolding
        # ate 93% of the threshold, leaving too little room to hold a single
        # source file. See _MIN_WORKING_FRACTION.
        compaction_budget = max(1_024, int(input_budget_tokens * compaction_threshold))
        scaffold_ceiling = max(
            1_024, int(compaction_budget * (1.0 - _MIN_WORKING_FRACTION))
        )
        selected_memory_tokens = min(
            selected_memory_tokens,
            max(256, int(scaffold_ceiling * _MEMORY_SHARE_OF_SCAFFOLD)),
        )
        bundle_ceiling = max(768, scaffold_ceiling - selected_memory_tokens)
        if instruction_tokens + source_tokens > bundle_ceiling:
            # Scaled rather than truncated, so the instruction/source ratio the
            # mode chose is preserved — a compact profile keeps its emphasis on
            # instructions, a standard one on sources.
            scale = bundle_ceiling / (instruction_tokens + source_tokens)
            instruction_tokens = max(256, int(instruction_tokens * scale))
            source_tokens = max(512, int(source_tokens * scale))
        # The initial bundle is drawn from the same allowance, so it cannot
        # exceed it either.
        initial = min(initial, max(768, scaffold_ceiling))
        # Zero means unlimited. Long, productive tasks must not be terminated
        # merely because they crossed an arbitrary turn count; operators can
        # still opt into a hard ceiling per model profile.
        max_steps = model.max_agent_steps or None
        return cls(
            profile_name=profile_name,
            mode=mode,
            input_budget_tokens=input_budget_tokens,
            initial_context_tokens=initial,
            instruction_tokens=instruction_tokens,
            memory_items=selected_memory_items,
            memory_tokens=selected_memory_tokens,
            source_tokens=source_tokens,
            max_source_files=max_source_files,
            per_file_tokens=per_file_tokens,
            recent_tool_groups=recent_groups,
            max_steps=max_steps,
            no_progress_limit=model.no_progress_limit,
            staged_retrieval=model.staged_retrieval,
            one_action_per_turn=compact,
        )


@dataclass(frozen=True)
class CapabilityEnvelope:
    """What the model that will *execute* a task can actually hold at once.

    ``ModelExecutionProfile`` shapes context packing while a task runs. This
    describes the same limits to whoever decides what a task *is* — the planner,
    and the splitter that catches a task the planner sized wrong. The two must
    come from one arithmetic, so this is derived from the profile rather than
    recomputed beside it.

    Two of the numbers deserve their reasoning stated:

    ``working_headroom_tokens`` is the compaction threshold minus the
    scaffolding, and is *exactly* the quantity ``resolve()`` sizes against (see
    ``_MIN_WORKING_FRACTION``) — the room left for the transcript once the
    instructions, memory and bundled sources compaction re-adds on every pass
    have taken their share. It is not a second notion of headroom; it names the
    existing one.

    ``task_source_budget_tokens`` is ``min(source_tokens, working_headroom)``.
    ``source_tokens`` is what the bundle can *carry*; the headroom is what the
    agent has left to *work in* after that bundle is re-added on every
    compaction pass. A task whose files fit the bundle but exceed the headroom
    is precisely the thrash case: the read succeeds, compaction sheds the
    transcript that held it, and the agent reads the same file again.
    """

    profile_name: str
    compact: bool
    one_action_per_turn: bool
    max_steps: int | None
    working_headroom_tokens: int
    source_tokens: int
    max_files_per_task: int
    task_source_budget_tokens: int

    @classmethod
    def from_profile(
        cls,
        profile: ModelExecutionProfile,
        *,
        compaction_threshold: float = 0.80,
    ) -> CapabilityEnvelope:
        scaffold = profile.instruction_tokens + profile.source_tokens + profile.memory_tokens
        threshold = int(profile.input_budget_tokens * compaction_threshold)
        headroom = max(0, threshold - scaffold)
        source_budget = max(1, min(profile.source_tokens, headroom or profile.source_tokens))
        # How many ordinary files that budget admits, floored at one: a task
        # must always be expressible, even on a window that can only hold part
        # of a single file. The cases that produces are what the splitter's
        # "one path over budget" branch exists for.
        affordable = max(1, source_budget // TYPICAL_SOURCE_FILE_TOKENS)
        ceiling = _MAX_FILES_PER_TASK_CEILING
        if profile.max_source_files:
            # A compact profile will not even pack more than this many, so
            # planning for more would be planning for files the agent is
            # guaranteed never to see.
            ceiling = min(ceiling, profile.max_source_files)
        return cls(
            profile_name=profile.profile_name,
            compact=profile.compact,
            one_action_per_turn=profile.one_action_per_turn,
            max_steps=profile.max_steps,
            working_headroom_tokens=headroom,
            source_tokens=profile.source_tokens,
            max_files_per_task=min(affordable, ceiling),
            task_source_budget_tokens=source_budget,
        )

    def describe(self) -> str:
        """Render the limits for a prompt, in numbers a model can arithmetic on.

        Deliberately free of "small", "a few" and "large": the repository
        summary already gives the planner each file's size in bytes, so stating
        the budget in tokens *and* characters lets it do the comparison instead
        of guessing what those words mean.
        """
        lines = [
            f"- Executor model profile: {self.profile_name}"
            f"{' (compact mode)' if self.compact else ''}",
            f"- Files per task: at most {self.max_files_per_task}",
            f"- Source per task: at most {self.task_source_budget_tokens} tokens"
            f" (~{self.task_source_budget_tokens * 4} characters of file content)",
            f"- Working room after that context is loaded:"
            f" {self.working_headroom_tokens} tokens",
        ]
        if self.one_action_per_turn:
            lines.append("- The executor takes exactly one action per turn.")
        if self.max_steps:
            lines.append(f"- The executor stops after {self.max_steps} steps per task.")
        return "\n".join(lines)


def adapt_context_bundle(
    context: ContextBundle,
    profile: ModelExecutionProfile,
) -> ContextBundle:
    """Apply a compact profile even when a caller supplied a generic bundle."""
    if not profile.compact:
        return context.model_copy(update={"execution_mode": "standard"})

    omitted: list[str] = list(context.omitted_context)
    selected_paths = context.included_paths[: profile.max_source_files]
    if len(context.included_paths) > len(selected_paths):
        omitted.append(
            f"{len(context.included_paths) - len(selected_paths)} source files; use read_file/grep"
        )
    files = _selected_sources(context.files, selected_paths, profile.per_file_tokens, omitted)
    tests = _selected_sources(context.tests, selected_paths, profile.per_file_tokens, omitted)
    memories = list(context.relevant_memories[: profile.memory_items])
    memories = _fit_memories(memories, profile.memory_tokens)
    if len(context.relevant_memories) > len(memories):
        omitted.append("additional memories; use memory_search")
    working = _compact_working_memory(context.working_memory)
    packet = context.task_packet or _packet_from_context(context, working, selected_paths)
    return context.model_copy(
        update={
            "files": files,
            "tests": tests,
            "included_paths": [*files, *tests],
            "relevant_memories": memories,
            "working_memory": working,
            "task_packet": packet.model_copy(
                update={"relevant_files": list(dict.fromkeys([*files, *tests]))}
            ),
            "execution_mode": "compact",
            "retrieval_stage": "initial" if profile.staged_retrieval else "expanded",
            "omitted_context": list(dict.fromkeys(omitted)),
        }
    )


def _selected_sources(
    sources: dict[str, str],
    selected_paths: list[str],
    per_file_tokens: int | None,
    omitted: list[str],
) -> dict[str, str]:
    selected: dict[str, str] = {}
    max_chars = (per_file_tokens or 0) * 4
    for path in selected_paths:
        content = sources.get(path)
        if content is None:
            continue
        if max_chars and len(content) > max_chars:
            notice = "\n… source clipped; use read_file with offset/limit …\n"
            remaining = max_chars - len(notice)
            head = remaining * 2 // 3
            content = content[:head] + notice + content[-(remaining - head) :]
            omitted.append(f"part of {path}; use read_file")
        selected[path] = content
    return selected


def _fit_memories(memories: list[dict[str, Any]], token_limit: int) -> list[dict[str, Any]]:
    fitted: list[dict[str, Any]] = []
    used = 0
    for item in memories:
        cost = max(1, len(str(item)) // 4)
        if fitted and used + cost > token_limit:
            break
        if cost > token_limit:
            item = dict(item)
            item["content"] = str(item.get("content", ""))[: token_limit * 3]
            cost = max(1, len(str(item)) // 4)
        fitted.append(item)
        used += cost
    return fitted


def _compact_working_memory(value: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "task_id",
        "current_goal",
        "interpreted_goal",
        "current_step",
        "completed_steps",
        "pending_steps",
        "files_changed",
        "test_status",
        "unresolved_questions",
        "unresolved_problems",
        "hypotheses",
        "errors",
        "last_action",
    )
    result = {
        key: value[key] for key in allowed if key in value and value[key] not in (None, "", [])
    }
    for key, limit in {
        "completed_steps": 8,
        "pending_steps": 8,
        "files_changed": 8,
        "unresolved_questions": 4,
        "unresolved_problems": 4,
        "hypotheses": 4,
        "errors": 3,
    }.items():
        if isinstance(result.get(key), list):
            result[key] = result[key][-limit:]
    return result


def _packet_from_context(
    context: ContextBundle,
    working: dict[str, Any],
    selected_paths: list[str],
) -> TaskPacket:
    errors = list(working.get("errors", []))
    if context.failure_summary:
        errors.append(context.failure_summary)
    current_step = str(working.get("current_step", ""))
    return TaskPacket(
        objective=context.task,
        acceptance_checks=context.acceptance_criteria[:8],
        active_decisions=context.architecture_decisions[:6],
        relevant_files=selected_paths,
        completed_steps=list(working.get("completed_steps", []))[-8:],
        pending_steps=list(working.get("pending_steps", []))[:8],
        current_errors=errors[-3:],
        next_action=current_step or "Inspect the most relevant source and take one bounded action.",
        retrieval_hint=(
            "If required evidence is missing, use read_file/grep. Use memory_search only for "
            "a prior decision, recurring failure, or cross-session fact."
        ),
    )
