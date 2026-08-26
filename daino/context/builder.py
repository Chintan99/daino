"""Central, budgeted assembly of instructions, task state, memory, and source."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from daino.config.models import Settings
from daino.context.compiler import ContextCompiler
from daino.context.profiles import ModelExecutionProfile, adapt_context_bundle
from daino.memory import InstructionResolver, MemoryManager, MemoryType
from daino.repository import RepositoryIndexer
from daino.schemas import ContextBundle, TaskPacket, TaskSpec

MEMORY_PRECEDENCE = (
    "Authority: current repository/source code > current explicit user instruction > scoped "
    "DAINO.md > active project decisions > verified project memory > episodic/session memory > "
    "global learned memory. Stale or automatically learned memory is advisory and must never "
    "override the repository or user. Surface an active conflicting decision before proposing "
    "an alternative."
)


class ContextBuilder:
    """Decide what the model needs now without dumping the memory database."""

    def __init__(
        self,
        root: Path,
        settings: Settings,
        memory: MemoryManager,
        *,
        indexer: RepositoryIndexer | None = None,
        token_budget: int | None = None,
    ) -> None:
        self.root = root.resolve()
        self.settings = settings
        self.memory = memory
        self.indexer = indexer or RepositoryIndexer(self.root)
        self.token_budget = token_budget or settings.project.context_budget_tokens
        self.instructions = InstructionResolver(self.root)

    @staticmethod
    def _tokens(value: str) -> int:
        return max(1, len(value) // 4)

    @staticmethod
    def _clip(value: str, token_limit: int) -> str:
        chars = max(256, token_limit * 4)
        if len(value) <= chars:
            return value
        notice = "\n… broader instructions clipped to the context budget …\n"
        remaining = chars - len(notice)
        # Keep the tail because closest DAINO.md and current user instructions
        # are deliberately rendered after broad global/repository rules.
        head = remaining // 3
        return value[:head] + notice + value[-(remaining - head) :]

    def build(
        self,
        task: TaskSpec,
        *,
        current_user_instruction: str = "",
        task_state_id: str | None = None,
        session_id: str | None = None,
        failure_summary: str | None = None,
        active_context_tokens: int = 0,
        recent_messages: list[dict[str, str]] | None = None,
        execution_profile: ModelExecutionProfile | None = None,
    ) -> ContextBundle:
        budget = min(
            self.token_budget,
            execution_profile.initial_context_tokens if execution_profile else self.token_budget,
        )
        instruction_tokens = (
            execution_profile.instruction_tokens if execution_profile else max(256, budget // 4)
        )
        target_paths = list(dict.fromkeys([*task.expected_files, *task.allowed_files]))
        resolved = self.instructions.resolve(
            target_paths,
            user_instruction=current_user_instruction or task.objective,
        )
        resolved = resolved.model_copy(
            update={
                "text": self._clip(
                    resolved.text,
                    instruction_tokens,
                )
            }
        )
        working: dict[str, Any] = {}
        compacted: dict[str, Any] = {}
        errors: list[str] = []
        state = None
        if task_state_id:
            try:
                state = self.memory.load_task(task_state_id)
            except ValueError:
                state = None
            if state is not None:
                working = state.model_dump(mode="json", exclude={"compacted_context"})
                working.update(
                    {
                        "plan": working.get("plan", [])[:100],
                        "files_inspected": working.get("files_inspected", [])[-50:],
                        "files_changed": working.get("files_changed", [])[-50:],
                        "commands_executed": working.get("commands_executed", [])[-20:],
                        "important_outputs": working.get("important_outputs", [])[-10:],
                        "errors": working.get("errors", [])[-10:],
                    }
                )
                if self._tokens(str(working)) > max(128, budget // 4):
                    working = {
                        "task_id": state.task_id,
                        "current_goal": state.interpreted_goal or state.original_request,
                        "plan": state.plan[:50],
                        "completed_steps": state.completed_steps[-50:],
                        "current_step": state.current_step,
                        "pending_steps": state.pending_steps[:50],
                        "files_changed": state.files_changed[-20:],
                        "commands_executed": [
                            {
                                "command": item.get("command", ""),
                                "success": item.get("success", False),
                            }
                            for item in state.commands_executed[-5:]
                        ],
                        "test_status": state.test_status,
                        "unresolved_questions": state.unresolved_questions[-10:],
                        "unresolved_problems": state.unresolved_problems[-10:],
                        "errors": state.errors[-3:],
                        "last_action": state.last_action,
                    }
                errors = state.errors
                threshold = int(budget * self.settings.memory.compaction_threshold)
                if active_context_tokens >= threshold:
                    compacted_model = self.memory.compact(
                        task_state_id,
                        messages=recent_messages,
                    )
                    compacted = compacted_model.model_dump(mode="json")
                else:
                    compacted = state.compacted_context

        self.memory.refresh_staleness(self.root)
        memories = self.memory.retrieve_for_task(
            f"{task.title}\n{task.objective}\n{failure_summary or ''}",
            task_id=task_state_id,
            session_id=session_id,
            errors=errors,
            limit=(
                execution_profile.memory_items
                if execution_profile
                else self.settings.memory.max_retrieved_items
            ),
        )
        memory_payload = [
            {
                "id": item.id,
                "type": item.type.value,
                "scope": item.scope.value,
                "content": item.content,
                "summary": item.summary,
                "confidence": item.confidence,
                "source": item.source,
                "status": item.status.value,
            }
            for item in memories
        ]
        decisions = [
            f"{item.id}: {item.content} (source: {item.source}, confidence: {item.confidence:.2f})"
            for item in memories
            if item.type == MemoryType.DECISION
        ]
        reserved = sum(
            self._tokens(value)
            for value in (
                resolved.text,
                str(working),
                str(compacted),
                str(memory_payload),
                MEMORY_PRECEDENCE,
            )
        )
        source_budget = max(512, budget - reserved)
        if execution_profile:
            source_budget = min(source_budget, execution_profile.source_tokens)
        compiled = ContextCompiler(
            self.root,
            self.indexer,
            token_budget=source_budget,
            max_files=execution_profile.max_source_files if execution_profile else None,
            per_file_tokens=execution_profile.per_file_tokens if execution_profile else None,
            prefer_symbol_slices=bool(execution_profile and execution_profile.compact),
        ).compile(task, decisions=decisions, failure_summary=failure_summary)
        final_resolved = self.instructions.resolve(
            list(dict.fromkeys([*target_paths, *compiled.included_paths])),
            user_instruction=current_user_instruction or task.objective,
        )
        final_instruction_text = self._clip(
            final_resolved.text,
            instruction_tokens,
        )
        constraints: list[str] = []
        if current_user_instruction and current_user_instruction != task.objective:
            constraints.append(current_user_instruction)
        stored_constraints = compacted.get("user_constraints", [])
        if isinstance(stored_constraints, list):
            constraints.extend(str(item) for item in stored_constraints[-6:])
        current_errors = list(errors[-3:])
        if failure_summary:
            current_errors.append(failure_summary)
        pending_steps = list(state.pending_steps[:8]) if state else []
        current_step = state.current_step if state else ""
        task_packet = TaskPacket(
            objective=state.interpreted_goal or state.original_request if state else task.objective,
            acceptance_checks=task.acceptance_criteria[:8],
            constraints=list(dict.fromkeys(constraints))[:8],
            active_decisions=decisions[:6],
            relevant_files=compiled.included_paths,
            completed_steps=list(state.completed_steps[-8:]) if state else [],
            pending_steps=pending_steps,
            current_errors=current_errors[-3:],
            verification_commands=task.verification_commands[:8],
            next_action=(
                current_step
                or (
                    pending_steps[0]
                    if pending_steps
                    else "Inspect the relevant code and take one bounded action."
                )
            ),
            retrieval_hint=(
                "Use read_file/grep when required source is omitted. Use memory_search only "
                "for a prior decision, recurring failure, or cross-session fact."
            ),
        )
        bundle = compiled.model_copy(
            update={
                "effective_instructions": final_instruction_text,
                "working_memory": working,
                "compacted_context": compacted,
                "relevant_memories": memory_payload,
                "memory_precedence": MEMORY_PRECEDENCE,
                "task_packet": task_packet,
                "token_estimate": (
                    compiled.token_estimate
                    + reserved
                    - self._tokens(resolved.text)
                    + self._tokens(final_instruction_text)
                ),
            }
        )
        if execution_profile:
            bundle = adapt_context_bundle(bundle, execution_profile)
            estimate = max(1, len(bundle.model_dump_json()) // 4)
            bundle = bundle.model_copy(update={"token_estimate": estimate})
        return bundle

    def build_question_context(
        self,
        question: str,
        *,
        paths: list[str] | None = None,
        task_state_id: str | None = None,
        session_id: str | None = None,
    ) -> str:
        resolved = self.instructions.resolve(paths or [], user_instruction=question)
        self.memory.refresh_staleness(self.root)
        memories = self.memory.retrieve_for_task(
            question,
            task_id=task_state_id,
            session_id=session_id,
        )
        rendered_memories = "\n".join(
            f"- [{item.type.value}/{item.scope.value}] {item.content} "
            f"(source: {item.source}, confidence: {item.confidence:.2f})"
            for item in memories
        )
        blocks = [MEMORY_PRECEDENCE]
        if resolved.text:
            blocks.append(resolved.text)
        if rendered_memories:
            blocks.append(f"Relevant retrieved memory (advisory):\n{rendered_memories}")
        return "\n\n".join(blocks)
