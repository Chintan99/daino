"""Model-aware execution policy and compact context adaptation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from vasuki.config.models import ModelProfileConfig
from vasuki.schemas import ContextBundle, TaskPacket


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
    max_steps: int
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
    ) -> ModelExecutionProfile:
        # Capability scores default to the neutral value 5 when a provider is
        # first configured. They therefore cannot classify a remote frontier
        # model by themselves: doing so incorrectly put every new remote model
        # into compact mode. A constrained window is objective evidence; local
        # placement plus modest scores is also a useful small-model signal.
        auto_compact = model.context_window <= 16_384 or (
            model.local
            and (
                model.coding_score <= 6
                or min(model.tool_reliability, model.structured_reliability) <= 5
            )
        )
        compact = model.execution_mode == "compact" or (
            model.execution_mode == "auto" and auto_compact
        )
        mode = ExecutionMode.COMPACT if compact else ExecutionMode.STANDARD
        derived_initial = min(input_budget_tokens, 8_192 if compact else input_budget_tokens)
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
            # Compact mode deliberately permits only one action per turn, so it
            # needs more turns than standard mode rather than fewer.
            max_steps = model.max_agent_steps or 32
        else:
            instruction_tokens = max(256, initial // 4)
            selected_memory_tokens = memory_tokens
            selected_memory_items = memory_items
            source_tokens = max(512, initial * 3 // 4)
            max_source_files = None
            per_file_tokens = None
            recent_groups = 6
            max_steps = model.max_agent_steps or 24
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
