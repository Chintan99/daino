"""Presentation-neutral mission and conversation workflows."""

from __future__ import annotations

import asyncio
import json
import re
import shlex
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from time import monotonic
from typing import Any

from sqlalchemy import func, select

from daino.agents import ReviewerAgent, TeamLead, TeamRunner, validate_team_plan
from daino.agents.loop import ToolLoop, describe_incomplete_outcome
from daino.agents.tool_schemas import AGENT_TOOL_SPECS, CHAT_TOOL_SPECS
from daino.application.attention import TurnAttention
from daino.application.context import ProjectContext
from daino.application.view_models import ConversationItem, MissionSummary
from daino.context import ModelExecutionProfile
from daino.design import DesignService
from daino.events import (
    ApprovalResolved,
    CheckpointCreated,
    FileChanged,
    MissionFailed,
    MissionPaused,
    ModelStreamChunk,
    TeamMemberCompleted,
    TeamMemberStarted,
    TeamPlanned,
    TodoUpdated,
    ToolCompleted,
    ToolFailed,
    ToolStarted,
)
from daino.exceptions import ConfigurationError
from daino.memory import MemoryManager, MemoryScope, MemoryType, PersistentTaskStatus
from daino.missions import MissionService
from daino.model_router import ModelRole
from daino.persistence.models import (
    Approval,
    Checkpoint,
    ConversationMessage,
    ConversationSession,
    ConversationState,
    Mission,
    RequirementVersion,
    Review,
    Task,
    ToolCall,
    VerificationRun,
)
from daino.prompts import CHAT_AGENT_SYSTEM
from daino.runtimes.base import Runtime
from daino.runtimes.detect import docker_status
from daino.schemas import (
    AgentAction,
    ChatOutcome,
    ContextBundle,
    FileDiff,
    InteractionMode,
    Message,
    MissionStatus,
    ProjectMode,
    ReviewReport,
    TaskSpec,
    TaskStatus,
    TeamMember,
    TeamMemberOutcome,
    TeamOutcome,
    TeamPlan,
    TodoItem,
    ToolResult,
)
from daino.security.commands import CommandGate
from daino.tools import EditTools, RecordingActionExecutor, WebResearchTool, build_file_diff
from daino.tools.commands import ApprovalCallback, CommandRunner
from daino.tools.diffing import render as render_diff
from daino.utils.ids import new_id
from daino.verification import missing_executable


def _with_partial_changes(reason: str, diffs: list[FileDiff]) -> str:
    """Append what was already changed to a failure reason.

    A stop is not the same as nothing having happened. Naming the files makes
    the difference actionable — the user can review or revert them — instead of
    leaving a red message above edits they have to discover for themselves.
    """
    if not diffs:
        return reason
    paths = list(dict.fromkeys(diff.path for diff in diffs))
    listed = ", ".join(paths[:5]) + (f", and {len(paths) - 5} more" if len(paths) > 5 else "")
    return (
        f"{reason}\n\n{len(paths)} file{'s' if len(paths) != 1 else ''} "
        f"had already been changed and were kept: {listed}. Review or revert them before retrying."
    )


#: The placeholder a session carries until its first request names it. Every
#: session used to keep it, which made a session list three identical rows.
DEFAULT_SESSION_TITLE = "General repository questions"


class MissionApplicationService:
    """Facade used by the TUI and suitable for thin CLI handlers."""

    def __init__(self, context: ProjectContext) -> None:
        self.context = context
        self.memory = getattr(context, "memory", None) or MemoryManager(
            context.database,
            context.root,
            context.settings,
        )
        self.core = MissionService(
            context.root,
            context.settings,
            context.database,
            events=context.events,
            memory=self.memory,
        )
        #: Sleep inhibition and OS notifications, so a turn the user walked
        #: away from keeps running and says how it ended. Shared by the TUI,
        #: the browser server, and the CLI.
        self.attention = TurnAttention(context.settings)
        #: Command approval memory per conversation session.
        self._command_gates: dict[str, CommandGate] = {}

    def create_session(
        self,
        title: str = DEFAULT_SESSION_TITLE,
        *,
        mission_id: str | None = None,
    ) -> str:
        item = ConversationSession(
            id=new_id("session"),
            project_id=self.context.database.project().id,
            mission_id=mission_id,
            title=title[:255],
            display_mode=(
                "compact" if self.context.settings.tui.display_mode == "compact" else "verbose"
            ),
        )
        with self.context.database.session() as session:
            session.add(item)
            session.add(
                ConversationState(
                    session_id=item.id,
                    interaction_mode=InteractionMode.ASK.value,
                    todos=[],
                )
            )
        return item.id

    def interaction_mode(self, session_id: str) -> InteractionMode:
        """Return the session's autonomy policy, repairing legacy sessions lazily."""
        with self.context.database.session() as session:
            item = session.get(ConversationState, session_id)
            return InteractionMode(item.interaction_mode) if item else InteractionMode.ASK

    def verbose_enabled(self, session_id: str) -> bool:
        with self.context.database.session() as session:
            item = session.get(ConversationSession, session_id)
            # Legacy sessions used "standard" before /verbose existed. Treat
            # them as detailed so upgrading does not silently hide progress.
            return bool(item and item.display_mode != "compact")

    def set_verbose(self, session_id: str, enabled: bool) -> None:
        with self.context.database.session() as session:
            item = session.get(ConversationSession, session_id)
            if item is None:
                raise ValueError(f"Unknown conversation session {session_id}")
            item.display_mode = "verbose" if enabled else "compact"

    def set_interaction_mode(self, session_id: str, mode: InteractionMode | str) -> None:
        selected = InteractionMode(mode)
        with self.context.database.session() as session:
            if session.get(ConversationSession, session_id) is None:
                raise ValueError(f"Unknown conversation session {session_id}")
            item = session.get(ConversationState, session_id)
            if item is None:
                item = ConversationState(session_id=session_id, todos=[])
                session.add(item)
            item.interaction_mode = selected.value

    def session_todos(self, session_id: str) -> list[TodoItem]:
        with self.context.database.session() as session:
            item = session.get(ConversationState, session_id)
            return [TodoItem.model_validate(todo) for todo in (item.todos if item else [])]

    def set_session_todos(
        self,
        session_id: str,
        todos: list[TodoItem],
        *,
        mission_id: str | None = None,
    ) -> None:
        serialized = [todo.model_dump(mode="json") for todo in todos]
        with self.context.database.session() as session:
            if session.get(ConversationSession, session_id) is None:
                raise ValueError(f"Unknown conversation session {session_id}")
            item = session.get(ConversationState, session_id)
            if item is None:
                item = ConversationState(session_id=session_id)
                session.add(item)
            item.todos = serialized
        self.context.events.publish(
            TodoUpdated(
                mission_id=mission_id,
                session_id=session_id,
                todos=serialized,
            )
        )

    def update_session_todo(
        self,
        session_id: str,
        content: str,
        status: str,
        *,
        mission_id: str | None = None,
    ) -> None:
        todos = self.session_todos(session_id)
        updated = False
        for index, todo in enumerate(todos):
            if todo.content == content:
                todos[index] = todo.model_copy(update={"status": status})
                updated = True
                break
        if updated:
            self.set_session_todos(session_id, todos, mission_id=mission_id)

    def update_verification_todo(
        self,
        session_id: str,
        status: str,
        *,
        mission_id: str | None = None,
    ) -> None:
        """Make the final test/build checklist item reflect actual verification."""
        todos = self.session_todos(session_id)
        keywords = ("test", "verify", "verification", "build", "check")
        candidates = [
            index
            for index, todo in enumerate(todos)
            if any(keyword in todo.content.casefold() for keyword in keywords)
        ]
        if not candidates:
            return
        index = candidates[-1]
        todos[index] = todos[index].model_copy(update={"status": status})
        self.set_session_todos(session_id, todos, mission_id=mission_id)

    def recent_sessions(self, limit: int = 20) -> list[ConversationSession]:
        with self.context.database.session() as session:
            items = session.scalars(
                select(ConversationSession)
                .where(
                    ConversationSession.project_id == self.context.database.project().id,
                    ConversationSession.status == "active",
                )
                .order_by(ConversationSession.updated_at.desc())
                .limit(limit)
            ).all()
            for item in items:
                session.expunge(item)
            return list(items)

    def latest_session(self) -> str:
        sessions = self.recent_sessions(1)
        return sessions[0].id if sessions else self.create_session()

    def session_for_mission(self, mission_id: str) -> str | None:
        with self.context.database.session() as session:
            item = session.scalar(
                select(ConversationSession)
                .where(
                    ConversationSession.project_id == self.context.database.project().id,
                    ConversationSession.mission_id == mission_id,
                )
                .order_by(ConversationSession.updated_at.desc())
            )
            return item.id if item else None

    def attach_session_mission(self, session_id: str, mission_id: str) -> None:
        """Make a newly created mission part of this conversation immediately.

        Model usage is recorded against missions. Waiting until the final answer
        message is saved means live token and cost counters cannot see a running
        turn, and a failed turn is never linked at all. Attach it before the first
        provider call instead.
        """
        with self.context.database.session() as session:
            stored_session = session.get(ConversationSession, session_id)
            if stored_session is None:
                raise ValueError(f"Unknown conversation session {session_id}")
            stored_session.mission_id = mission_id
            existing_link = session.scalar(
                select(ConversationMessage.id).where(
                    ConversationMessage.session_id == session_id,
                    ConversationMessage.mission_id == mission_id,
                )
            )
            if existing_link is not None:
                return
            pending_prompt = session.scalar(
                select(ConversationMessage)
                .where(
                    ConversationMessage.session_id == session_id,
                    ConversationMessage.kind == "user",
                    ConversationMessage.mission_id.is_(None),
                )
                .order_by(ConversationMessage.created_at.desc())
            )
            if pending_prompt is not None:
                pending_prompt.mission_id = mission_id
                return
            # Some callers create an auditing mission without a visible prompt.
            # Keep a hidden durable link so a later turn cannot make this
            # mission's usage disappear when ConversationSession.mission_id moves.
            session.add(
                ConversationMessage(
                    id=new_id("message"),
                    session_id=session_id,
                    mission_id=mission_id,
                    kind="mission_link",
                    role="system",
                    content="",
                    metadata_json={"hidden": True},
                )
            )

    def add_message(
        self,
        session_id: str,
        *,
        kind: str,
        role: str,
        content: str,
        mission_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ConversationMessage:
        message = ConversationMessage(
            id=new_id("message"),
            session_id=session_id,
            mission_id=mission_id,
            kind=kind,
            role=role,
            content=content,
            metadata_json=metadata or {},
        )
        with self.context.database.session() as session:
            stored_session = session.get(ConversationSession, session_id)
            if stored_session is None:
                raise ValueError(f"Unknown conversation session {session_id}")
            if mission_id:
                stored_session.mission_id = mission_id
            # The first request names the session, so a session list is legible.
            if kind == "user" and stored_session.title == DEFAULT_SESSION_TITLE:
                summary = " ".join(content.split())[:80].strip()
                if summary:
                    stored_session.title = summary
            session.add(message)
        return message

    def messages(self, session_id: str, limit: int = 500) -> list[ConversationItem]:
        with self.context.database.session() as session:
            items = session.scalars(
                select(ConversationMessage)
                .where(
                    ConversationMessage.session_id == session_id,
                    ConversationMessage.kind != "mission_link",
                )
                .order_by(ConversationMessage.created_at)
                .limit(limit)
            ).all()
            return [
                ConversationItem(
                    id=item.id,
                    kind=item.kind,
                    role=item.role,
                    content=item.content,
                    created_at=item.created_at,
                    metadata=item.metadata_json,
                )
                for item in items
            ]

    def context_files(self, session_id: str) -> list[str]:
        with self.context.database.session() as session:
            item = session.get(ConversationSession, session_id)
            if item is None:
                raise ValueError(f"Unknown conversation session {session_id}")
            return list(item.context_files)

    def toggle_context_file(self, session_id: str, relative_path: str) -> bool:
        target = (self.context.root / relative_path).resolve()
        if not target.is_relative_to(self.context.root) or not target.is_file():
            raise ValueError("Context file is outside the project or does not exist")
        with self.context.database.session() as session:
            item = session.get(ConversationSession, session_id)
            if item is None:
                raise ValueError(f"Unknown conversation session {session_id}")
            paths = list(item.context_files)
            if relative_path in paths:
                paths.remove(relative_path)
                attached = False
            else:
                paths.append(relative_path)
                attached = True
            item.context_files = paths
        return attached

    def _supplemental_context(self, session_id: str, text: str) -> str:
        """Resolve explicit file and symbol references within the project boundary."""
        from daino.repository import RepositoryIndexer

        paths = set(self.context_files(session_id))
        paths.update(match.group(1).rstrip(".,;") for match in re.finditer(r"@file:([^\s]+)", text))
        indexer = RepositoryIndexer(self.context.root)
        for match in re.finditer(r"@symbol:([A-Za-z_][\w.]*)", text):
            symbol_name = match.group(1).split(".")[-1]
            paths.update(item.path for item in indexer.find_symbol(symbol_name))
        sections: list[str] = []
        consumed = 0
        budget = self.context.settings.project.context_budget_tokens * 3
        for relative in sorted(paths):
            target = (self.context.root / relative).resolve()
            if (
                not target.is_relative_to(self.context.root)
                or not target.is_file()
                or target.stat().st_size > 1_000_000
            ):
                continue
            try:
                content = target.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            remaining = budget - consumed
            if remaining <= 0:
                break
            content = content[:remaining]
            sections.append(f"--- {relative} ---\n{content}")
            consumed += len(content)
        for match in re.finditer(r"@mission:([A-Za-z0-9_.:-]+)", text):
            try:
                details = self.mission_details(match.group(1))
            except ValueError:
                continue
            summary = json.dumps(
                {
                    "mission": details["mission"],
                    "tasks": details["tasks"],
                    "tests": details["tests"],
                    "reviews": details["reviews"],
                },
                indent=2,
                default=str,
            )
            sections.append(f"--- mission {match.group(1)} ---\n{summary[:12000]}")
        for match in re.finditer(r"@playbook:([A-Za-z0-9_.-]+)", text):
            from daino.playbooks import PlaybookLoader

            try:
                playbook = PlaybookLoader(self.context.root).get(match.group(1))
            except (KeyError, ValueError):
                continue
            sections.append(
                f"--- playbook {match.group(1)} ---\n{playbook.model_dump_json(indent=2)}"
            )
        return "\n\n".join(sections)

    async def plan(
        self,
        request: str,
        session_id: str,
        mode: ProjectMode | None = None,
        *,
        profile_override: str = "",
    ) -> tuple[Mission, Any, Any]:
        self.add_message(session_id, kind="user", role="user", content=request)
        try:
            supplemental = await asyncio.to_thread(
                self._supplemental_context,
                session_id,
                request,
            )
            mission, requirements, plan = await self.core.plan(
                request,
                mode,
                supplemental_context=supplemental,
                profile_override=profile_override,
            )
        except Exception as exc:
            self.add_message(
                session_id,
                kind="error",
                role="system",
                content=str(exc),
            )
            raise
        persistent = self.memory.task_for_mission(mission.id)
        if persistent:
            self.memory.update_task(persistent.task_id, session_id=session_id)
        self.add_message(
            session_id,
            kind="plan",
            role="architect",
            content=requirements.problem_statement,
            mission_id=mission.id,
            metadata={
                "requirements": requirements.model_dump(mode="json"),
                "tasks": [task.model_dump(mode="json") for task in plan.tasks],
            },
        )
        self.set_session_todos(
            session_id,
            [
                TodoItem(
                    content=task.title,
                    status=(
                        "completed"
                        if task.status == TaskStatus.COMPLETED
                        else "failed"
                        if task.status == TaskStatus.FAILED
                        else "in_progress"
                        if task.status == TaskStatus.RUNNING
                        else "pending"
                    ),
                )
                for task in plan.tasks
            ],
            mission_id=mission.id,
        )
        return mission, requirements, plan

    def answer_role(self, profile_override: str = "") -> ModelRole:
        """Pick the cheapest configured role able to answer a question.

        Raises a directly actionable error when nothing is connected, instead of
        letting the failure surface later as an opaque provider or routing error.
        """
        if profile_override:
            return ModelRole.SUMMARIZER
        for role in (
            ModelRole.SUMMARIZER,
            ModelRole.ARCHITECT,
            ModelRole.BUILDER,
            ModelRole.PLANNER,
        ):
            if self.core._role_available(role):
                return role
        raise ConfigurationError(
            "No model is connected yet. Open Providers (Ctrl+P → Switch provider, "
            "or /provider), fill in the form, and press Validate + save."
        )

    def _question_context(self, session_id: str, question: str) -> tuple[str, str]:
        """Build the repository grounding for a question. Blocking; call in a thread."""
        from daino.context import ContextBuilder
        from daino.repository import RepositoryIndexer

        persistent = self._latest_task_for_session(session_id)
        memory_context = ContextBuilder(
            self.context.root,
            self.context.settings,
            self.memory,
            indexer=RepositoryIndexer(self.context.root),
        ).build_question_context(
            question,
            paths=self.context_files(session_id),
            task_state_id=persistent.task_id if persistent else None,
            session_id=session_id,
        )
        return (
            RepositoryIndexer(self.context.root).summary(),
            "\n\n".join(
                value
                for value in (memory_context, self._supplemental_context(session_id, question))
                if value.strip()
            ),
        )

    def session_message_counts(self) -> dict[str, int]:
        """How many transcript entries each session holds, for the session list."""
        with self.context.database.session() as session:
            rows = session.execute(
                select(
                    ConversationMessage.session_id,
                    func.count(ConversationMessage.id),
                ).group_by(ConversationMessage.session_id)
            ).all()
        return {row[0]: int(row[1]) for row in rows}

    def conversation_history(self, session_id: str, *, turns: int = 12) -> list[Message]:
        """Return recent question/answer turns so follow-up questions have context."""
        history: list[Message] = []
        for item in self.messages(session_id)[-turns * 2 :]:
            if item.kind == "user":
                history.append(Message(role="user", content=item.content))
            elif item.kind == "agent" and item.content.strip():
                history.append(Message(role="assistant", content=item.content))
        return history

    async def ask(
        self,
        question: str,
        session_id: str,
        *,
        profile_override: str = "",
    ) -> str:
        """Stream a repository-grounded answer while preserving it in the session."""
        role = self.answer_role(profile_override)
        history = self.conversation_history(session_id)
        self.add_message(session_id, kind="user", role="user", content=question)
        mission = self.core.create(question, ProjectMode.DIRECT)
        self.attach_session_mission(session_id, mission.id)
        persistent = self.memory.task_for_mission(mission.id)
        if persistent:
            persistent = self.memory.update_task(
                persistent.task_id,
                session_id=session_id,
                interpreted_goal=question,
                status=PersistentTaskStatus.IN_PROGRESS,
            )
        summary, supplemental = await asyncio.to_thread(
            self._question_context,
            session_id,
            question,
        )
        grounding = f"{question}\n\n{summary}"
        if supplemental.strip():
            grounding += f"\n\n{supplemental}"
        messages = [
            Message(
                role="system",
                content=(
                    "You are Daino, an engineering assistant for this repository. "
                    "Answer using the supplied repository map and attached files, "
                    "and state clearly when something is not covered by them."
                ),
            ),
            *history,
            Message(role="user", content=grounding),
        ]
        chunks: list[str] = []
        try:
            async for chunk in self.core.gateway.stream(
                mission.id,
                role,
                messages,
                profile_override=profile_override or None,
            ):
                chunks.append(chunk)
                self.context.events.publish(
                    ModelStreamChunk(mission_id=mission.id, content=chunk, role=role.value)
                )
        except Exception as exc:
            self.core._update_mission(
                mission.id,
                status=MissionStatus.FAILED.value,
                failure=str(exc),
            )
            if persistent:
                self.memory.update_task(
                    persistent.task_id,
                    status=PersistentTaskStatus.FAILED,
                    errors=[*persistent.errors, str(exc)],
                )
            self.record_failure(session_id, mission.id, exc)
            raise
        answer = "".join(chunks)
        self.add_message(
            session_id,
            kind="agent",
            role=role.value,
            content=answer,
            mission_id=mission.id,
        )
        self.core._update_mission(mission.id, status=MissionStatus.COMPLETED.value)
        if persistent:
            self.memory.complete_task(
                persistent.task_id,
                summary=answer[:1_000] or "Question answered",
                outcome="completed",
                create_episode=False,
            )
        return answer

    async def run_shell(self, command: str, session_id: str) -> ToolResult:
        """Run a command the user typed themselves, and record it in the transcript.

        Deliberately different from the agent's ``run_command`` in two ways.

        It goes through a real shell, so pipes, redirects and globs work — that
        is what a user means by "run a shell command", and the agent's shell-free
        exec would hand ``|`` to the program as a literal argument.

        And it is not policy-gated. The gate exists to stop the *model* running
        something the user did not ask for; prompting someone to approve the
        command they just typed protects nobody. The workspace and runtime are
        unchanged, and the call is still audited.
        """
        command = command.strip()
        if not command:
            return ToolResult(tool="shell", success=False, error="No command given.")
        self.add_message(session_id, kind="user", role="user", content=f"! {command}")
        runtime = self.core._runtime(self.context.root)
        started = monotonic()
        try:
            await runtime.prepare()
            result = await runtime.execute(
                f"sh -lc {shlex.quote(command)}",
                timeout=self.context.settings.runtime.command_timeout_seconds,
                approved=True,
            )
        except Exception as exc:  # noqa: BLE001 - a failed command is output, not a crash
            reason = str(exc)
            if self.context.settings.runtime.default == "docker":
                usable, detail = docker_status()
                if not usable and detail:
                    reason = detail
            self.add_message(session_id, kind="error", role="", content=f"$ {command}\n{reason}")
            return ToolResult(tool="shell", success=False, error=reason)
        finally:
            await runtime.cleanup()

        output = (result.stdout + result.stderr).strip()
        body = f"$ {command}"
        if output:
            body += f"\n{output}"
        elif result.succeeded:
            body += "\n(no output)"
        else:
            body += f"\nexited with status {result.exit_code}"
        self.add_message(
            session_id,
            kind="tool" if result.succeeded else "error",
            role="",
            content=body,
        )
        with self.context.database.session() as session:
            session.add(
                ToolCall(
                    id=new_id("tool-call"),
                    mission_id=None,
                    tool="user.shell",
                    arguments={"command": command},
                    result_summary=output[:1000],
                    duration_seconds=monotonic() - started,
                    success=result.succeeded,
                )
            )
        return ToolResult(
            tool="shell",
            success=result.succeeded,
            data={"command": command, "exit_code": result.exit_code, "output": output},
            duration_seconds=monotonic() - started,
        )

    def _session_gate(self, session_id: str) -> CommandGate:
        """One approval memory per conversation, so "always" lasts the session."""
        gate = self._command_gates.get(session_id)
        if gate is None:
            gate = CommandGate(self.context.settings.security)
            self._command_gates[session_id] = gate
        return gate

    async def _command_runner(
        self, session_id: str, approve: ApprovalCallback | None
    ) -> tuple[Runtime | None, CommandRunner]:
        """Start the configured runtime and wrap it for the agent.

        A runtime that will not start is reported to the agent as one clear
        sentence rather than raised: not being able to run tests is a limitation
        to work around, not a reason to abandon the edit.
        """
        runtime_name = self.context.settings.runtime.default
        # Announced here because every approvable command passes through this
        # runner, whichever client supplied the callback.
        approve = self.attention.watching_approvals(approve)
        runtime = self.core._runtime(self.context.root)
        try:
            await runtime.prepare()
        except Exception as exc:  # noqa: BLE001 - reported to the agent, not raised
            reason = str(exc)
            if runtime_name == "docker":
                usable, detail = docker_status()
                if not usable and detail:
                    reason = detail
            return None, CommandRunner(
                runtime,
                self._session_gate(session_id),
                runtime_name=runtime_name,
                unavailable=(
                    f"Commands cannot run: the {runtime_name} runtime is unavailable. {reason} "
                    "Do not retry commands this turn. Finish the work you can do without them, "
                    "and tell the user to run /runtime local or fix the runtime."
                ),
            )
        return runtime, CommandRunner(
            runtime,
            self._session_gate(session_id),
            runtime_name=runtime_name,
            approve=approve,
        )

    async def chat(
        self,
        instruction: str,
        session_id: str,
        *,
        profile_override: str = "",
        approve: ApprovalCallback | None = None,
    ) -> ChatOutcome:
        """Run the agent on one chat turn: it answers, or it edits and reports the diff.

        There is no question/instruction classifier. The agent is given the same
        grounded tools the builder has plus ``respond``, and it decides which the
        request called for — a request to change something is carried out rather
        than described.
        """
        if not self.core._role_available(ModelRole.BUILDER, profile_override):
            raise ConfigurationError(
                "No model is connected yet. Open Providers (Ctrl+P → Switch provider, "
                "or /provider), fill in the form, and press Validate + save."
            )
        # Read before this turn is persisted, or the instruction would appear
        # twice: once as history and again as the task.
        history = self.conversation_history(session_id)
        self.add_message(session_id, kind="user", role="user", content=instruction)
        # A chat turn opens its own mission for auditing, but the work itself
        # continues across turns, so the session's existing working memory is
        # carried forward rather than replaced by an empty one.
        mission = self.core.create(instruction, ProjectMode.DIRECT, start_task=False)
        self.attach_session_mission(session_id, mission.id)
        persistent = self._continue_session_task(session_id, mission.id, instruction)
        # Taken before the first edit so /restore always has a way back; the
        # agent writes to the real working tree, not a worktree.
        self._checkpoint_working_tree(mission.id)
        gateway = self.core.gateway.with_profile(profile_override)
        budgeter = getattr(gateway, "context_budget", None)
        model_budget = (
            budgeter(ModelRole.BUILDER, tools=CHAT_TOOL_SPECS)
            if callable(budgeter)
            else self.context.settings.project.context_budget_tokens
        )
        profile_resolver = getattr(gateway, "execution_profile", None)
        execution_profile = (
            profile_resolver(ModelRole.BUILDER, tools=CHAT_TOOL_SPECS)
            if callable(profile_resolver)
            else None
        )
        context_reserve = min(2_048, max(512, model_budget // 4))
        context_budget = min(
            self.context.settings.project.context_budget_tokens,
            max(512, model_budget - context_reserve),
        )
        base_context = await asyncio.to_thread(
            self._team_context,
            instruction,
            context_budget,
            persistent.task_id if persistent else None,
            session_id,
            execution_profile,
        )

        editor = EditTools(
            self.context.root,
            require_read_before_write=True,
            seen_files=set(base_context.included_paths),
        )
        runtime, runner = await self._command_runner(session_id, approve)
        if runner.unavailable:
            # Learned at the top of the turn, not on the first command near the
            # end of it. A 50-minute build that only discovers at the finish
            # that nothing could ever be verified has wasted the whole run and
            # reports itself as failed for a reason unrelated to the code.
            base_context = base_context.model_copy(
                update={
                    "effective_instructions": "\n\n".join(
                        item
                        for item in (
                            base_context.effective_instructions,
                            f"RUNTIME UNAVAILABLE: {runner.unavailable} Plan for this from the "
                            "start: do the work that needs no commands, do not promise "
                            "verification you cannot perform, and state plainly in your summary "
                            "which checks the user must run by hand.",
                        )
                        if item
                    )
                }
            )
            self.add_message(
                session_id,
                kind="status",
                role="",
                content=(
                    f"Commands cannot run this turn — the "
                    f"{self.context.settings.runtime.default} runtime is unavailable. "
                    "The agent will edit files but cannot verify them. "
                    "Run /runtime local to execute checks on this machine."
                ),
                mission_id=mission.id,
            )
        executor = RecordingActionExecutor(
            editor,
            runner,
            web=WebResearchTool(
                approve=approve,
                require_approval=self.context.settings.security.require_approval_for_network,
            ),
            memory=self.memory,
            memory_task_id=persistent.task_id if persistent else None,
            memory_session_id=session_id,
            design=DesignService(self.context.root, events=self.context.events),
        )
        loop = ToolLoop(
            gateway,
            ModelRole.BUILDER,
            executor,
            on_action_start=self._record_chat_action_started(mission.id),
            system=CHAT_AGENT_SYSTEM,
            tools=CHAT_TOOL_SPECS,
            require_verified_finish=True,
            execution_profile=execution_profile,
        )
        diffs: list[FileDiff] = []
        try:
            result = await loop.run(
                mission.id,
                base_context,
                on_action=self._record_chat_action(mission.id, session_id, executor, diffs),
                history=history,
            )
            if not result.completed:
                raise RuntimeError(
                    describe_incomplete_outcome(
                        result, role_label="coding", pinned=bool(profile_override)
                    )
                )
        except Exception as exc:
            # A turn that stops early has usually already changed files, and
            # reporting only the failure hides them: the user is told the work
            # failed while two edits sit in their working tree. Record what
            # landed, then report the stop.
            partial = ChatOutcome(mission_id=mission.id, diffs=list(diffs))
            self._record_changeset(session_id, mission.id, partial)
            self.core._update_mission(
                mission.id,
                status=MissionStatus.FAILED.value,
                failure=_with_partial_changes(str(exc), diffs),
            )
            self.record_failure(
                session_id, mission.id, RuntimeError(_with_partial_changes(str(exc), diffs))
            )
            raise
        finally:
            if runtime is not None:
                # A container started for this turn must not outlive it.
                await runtime.cleanup()

        # A verified finish means the planned work is done, but weaker models
        # routinely forget to re-emit the todo list with completed statuses,
        # leaving the checklist stuck at 0/N. When the agent actually changed the
        # tree and finished, reflect the real outcome so completion is visible and
        # each newly-ticked task surfaces a task-complete line in the UI.
        if result.changed:
            pending = self.session_todos(session_id)
            if pending and any(todo.status != "completed" for todo in pending):
                self.set_session_todos(
                    session_id,
                    [todo.model_copy(update={"status": "completed"}) for todo in pending],
                    mission_id=mission.id,
                )

        outcome = ChatOutcome(
            mission_id=mission.id,
            answer=result.answer,
            summary="" if result.answer else result.implementation.summary,
            diffs=diffs,
            changed=result.changed,
            steps=result.steps,
        )
        if result.answer:
            self.add_message(
                session_id,
                kind="agent",
                role=ModelRole.BUILDER.value,
                content=result.answer,
                mission_id=mission.id,
            )
        commands = result.implementation.verification_commands
        if result.changed:
            await self._verify_chat_edit(
                outcome,
                commands,
                session_id,
                mission.id,
                approve=approve,
            )
        # No diffs are written here: each edit already posted its own as it
        # landed, so repeating them would show every change twice. The summary
        # comes after verification so it cannot announce success above a later
        # red tester card.
        if outcome.summary:
            summary = outcome.summary
            kind = "agent"
            if outcome.verified is False:
                kind = "error"
                summary = (
                    "Changes were applied, but the task is not complete because verification "
                    f"failed.\nBuilder report: {summary}"
                )
            elif outcome.verified is None and outcome.changed:
                kind = "status"
                summary = f"Changes were applied but remain unverified.\nBuilder report: {summary}"
            self.add_message(
                session_id,
                kind=kind,
                role=ModelRole.BUILDER.value,
                content=summary,
                mission_id=mission.id,
            )

        self._record_changeset(session_id, mission.id, outcome)

        if outcome.verified is False:
            self.core._update_mission(
                mission.id,
                status=MissionStatus.FAILED.value,
                failure=outcome.verification_summary,
            )
        elif outcome.verified is None and outcome.changed:
            self.core._update_mission(
                mission.id,
                status=MissionStatus.BLOCKED.value,
                failure=outcome.verification_summary or "Changes were not verified",
            )
        else:
            self.core._update_mission(mission.id, status=MissionStatus.COMPLETED.value)
        if persistent:
            status = (
                "failed"
                if outcome.verified is False
                else "blocked"
                if outcome.verified is None and outcome.changed
                else "completed"
            )
            self.memory.extract(
                instruction,
                task_id=persistent.task_id,
                session_id=session_id,
                source="user request",
                source_type="user",
            )
            if status == "completed" and self._unfinished_todos(session_id):
                # The turn ended cleanly but the plan is not done — a step limit,
                # a hand-back for input, or simply more work than one turn holds.
                # Closing the task here is what made the next turn start over.
                self.memory.update_task(
                    persistent.task_id,
                    status=PersistentTaskStatus.IN_PROGRESS,
                    last_action="awaiting continuation",
                )
            else:
                self.memory.complete_task(
                    persistent.task_id,
                    summary=outcome.answer or outcome.summary or "Chat task finished",
                    outcome=status,
                )
        return outcome

    def _record_changeset(self, session_id: str, mission_id: str, outcome: ChatOutcome) -> None:
        """Persist one summary of everything the turn edited.

        Individual diffs are already streamed as they land, which is right while
        the turn is running and useless afterwards: scrolling back through six
        separate cards to answer "what did it touch?" is the wrong shape. One
        closing message carries the whole changeset, so both clients can render
        the same list of files with their line counts — and neither has to
        recompute it from the transcript.
        """
        if not outcome.diffs:
            return
        # A file edited twice in one turn appears once, with the totals summed.
        totals: dict[str, dict[str, object]] = {}
        for diff in outcome.diffs:
            entry = totals.setdefault(
                diff.path,
                {"path": diff.path, "change": diff.change, "added": 0, "removed": 0},
            )
            entry["added"] = int(entry["added"]) + diff.added  # type: ignore[call-overload]
            entry["removed"] = int(entry["removed"]) + diff.removed  # type: ignore[call-overload]
            # A file created and then modified is still, overall, created.
            if diff.change == "created":
                entry["change"] = "created"
        files = sorted(
            totals.values(),
            key=lambda item: int(item["added"]) + int(item["removed"]),  # type: ignore[call-overload]
            reverse=True,
        )
        added = sum(int(item["added"]) for item in files)  # type: ignore[call-overload]
        removed = sum(int(item["removed"]) for item in files)  # type: ignore[call-overload]
        label = f"Edited {len(files)} file{'s' if len(files) != 1 else ''}"
        lines = [f"{label}  +{added} -{removed}"]
        lines.extend(
            f"  {item['path']}  +{item['added']} -{item['removed']}" for item in files
        )
        self.add_message(
            session_id,
            kind="changeset",
            role="",
            content="\n".join(lines),
            mission_id=mission_id,
            metadata={
                "files": files,
                "added": added,
                "removed": removed,
                "verified": outcome.verified,
            },
        )

    def _unfinished_todos(self, session_id: str) -> list[TodoItem]:
        """Return the session's plan steps that are neither done nor abandoned."""
        try:
            todos = self.session_todos(session_id)
        except Exception:  # noqa: BLE001 - a todo read must not fail a finished turn
            return []
        return [item for item in todos if item.status in {"pending", "in_progress"}]

    def _continue_session_task(
        self,
        session_id: str,
        mission_id: str,
        instruction: str,
    ) -> Any | None:
        """Adopt the session's working memory for this turn, or open a new task.

        Every chat turn used to create a mission, and creating a mission created
        a blank persistent task whose ``original_request`` was the new message.
        Typing "continue" therefore produced a task whose stated goal was the
        word "continue" and whose completed steps, inspected files and changed
        files were all empty — the agent had no record of the work it had
        already done and began the request again from the start. Continuing the
        prior task keeps that record and re-points it at the new mission.
        """
        if not (self.context.settings.memory.enabled and self.context.settings.memory.auto_save):
            return None
        # An unfinished task outranks a newer finished one: a question answered
        # in the middle of a build closes its own task, and adopting that would
        # replace the build's goal with the question.
        previous = self._latest_task_for_session(session_id) or self.memory.latest_task_for_session(
            session_id
        )
        if previous is not None and self._is_continuation(previous, instruction):
            return self.memory.update_task(
                previous.task_id,
                mission_id=mission_id,
                session_id=session_id,
                interpreted_goal=instruction,
                status=PersistentTaskStatus.IN_PROGRESS,
            )
        return self.memory.start_task(
            instruction,
            mission_id=mission_id,
            session_id=session_id,
            status=PersistentTaskStatus.IN_PROGRESS,
        )

    def _is_continuation(self, previous: Any, instruction: str) -> bool:
        """Decide whether this turn carries on the session's previous task.

        Unfinished work is the signal: an open task, or a plan with steps still
        outstanding. A session whose last task finished with an empty plan is
        treated as a fresh start so an unrelated later question does not inherit
        stale files and steps.
        """
        if previous.status != PersistentTaskStatus.COMPLETED:
            return True
        return bool(self._unfinished_todos(previous.session_id or "") or previous.pending_steps)

    def _checkpoint_working_tree(self, mission_id: str) -> None:
        """Snapshot the tree before the agent edits it, without blocking the turn."""
        from daino.application.checkpoint_service import CheckpointApplicationService

        try:
            CheckpointApplicationService(self.context).create("Before chat edit")
        except Exception as exc:  # noqa: BLE001 - a missing checkpoint must not stop the work
            self.context.events.publish(
                MissionPaused(mission_id=mission_id, reason=f"Checkpoint skipped: {exc}")
            )

    async def _verify_chat_edit(
        self,
        outcome: ChatOutcome,
        commands: list[str],
        session_id: str,
        mission_id: str,
        *,
        approve: ApprovalCallback | None = None,
    ) -> None:
        """Run the checks the agent itself proposed and report the result."""
        from daino.application.verification_service import VerificationApplicationService

        self.update_verification_todo(session_id, "in_progress", mission_id=mission_id)
        try:
            report = await VerificationApplicationService(self.context).run(
                commands,
                mission_id=mission_id,
                approve=approve,
                gate=self._session_gate(session_id),
            )
        except Exception as exc:  # noqa: BLE001 - a broken check is a result, not a crash
            # An unavailable runtime means the checks never ran, which says
            # nothing about the edit. Reporting that as a failure makes a
            # successful change look broken, so it stays an unverified note.
            outcome.verified = None
            outcome.verification_summary = (
                f"Edit applied, but verification was skipped: {exc}. "
                f"Switch the runtime with /runtime local, or run: {'; '.join(commands)}"
            )
            self.add_message(
                session_id,
                kind="status",
                role="tester",
                content=outcome.verification_summary,
                mission_id=mission_id,
            )
            self.update_verification_todo(session_id, "failed", mission_id=mission_id)
            return
        else:
            outcome.verified = bool(getattr(report, "passed", False))
            failures = getattr(report, "failures", [])
            executed = [check for check in getattr(report, "checks", []) if not check.skipped]
            missing = [
                tool
                for tool in (
                    missing_executable(
                        str(getattr(item, "command", "")),
                        str(getattr(item, "output_excerpt", "")),
                    )
                    for item in failures
                )
                if tool
            ]
            if not outcome.verified and failures and len(missing) == len(failures):
                # Every check failed only because the runtime has no such
                # program — the container image ships no git or node. The edit
                # is unverified, not broken, and saying otherwise sends the user
                # looking for a bug in code that was never checked.
                outcome.verified = None
                tools = ", ".join(dict.fromkeys(missing))
                outcome.verification_summary = (
                    f"Edit applied but not verified: the "
                    f"{self.context.settings.runtime.default} runtime has no {tools}. "
                    f"Run /runtime local, or run these by hand: {'; '.join(commands)}"
                )
                self.add_message(
                    session_id,
                    kind="status",
                    role="tester",
                    content=outcome.verification_summary,
                    mission_id=mission_id,
                )
                self.update_verification_todo(session_id, "failed", mission_id=mission_id)
                return
            outcome.verification_summary = (
                f"{len(executed)} check(s) passed"
                if outcome.verified
                else "; ".join(
                    f"{getattr(item, 'command', item)}: {getattr(item, 'summary', '')}".rstrip(": ")
                    for item in failures
                )
                or "verification failed"
            )
        self.add_message(
            session_id,
            kind="test" if outcome.verified else "error",
            role="tester",
            content=outcome.verification_summary,
            mission_id=mission_id,
        )
        self.update_verification_todo(
            session_id,
            "completed" if outcome.verified else "failed",
            mission_id=mission_id,
        )

    def _record_chat_action(
        self,
        mission_id: str,
        session_id: str,
        executor: RecordingActionExecutor,
        diffs: list[FileDiff],
    ) -> Callable[..., None]:
        def observe(action: AgentAction, result: ToolResult, paths: list[str]) -> None:
            tool = f"chat.{action.action}"
            subject = _action_subject(action)
            if result.success:
                self.context.events.publish(
                    ToolCompleted(
                        mission_id=mission_id,
                        tool=tool,
                        summary=subject,
                        duration_seconds=result.duration_seconds,
                    )
                )
            else:
                self.context.events.publish(
                    ToolFailed(
                        mission_id=mission_id,
                        tool=tool,
                        error=result.error or "action failed",
                    )
                )
            if action.action == "todo" and result.success:
                self.set_session_todos(
                    session_id,
                    list(action.todos),
                    mission_id=mission_id,
                )
            # Work the agent does that is not an edit still has to be visible.
            # A command that runs invisibly leaves the user unable to tell a
            # working agent from a stuck one.
            note = _describe_action(action, result)
            if note:
                self.add_message(
                    session_id,
                    kind="tool" if result.success else "error",
                    role="",
                    content=note,
                    mission_id=mission_id,
                )
            # Each edit is shown the moment it lands. Waiting until the turn ends
            # leaves the user watching bare "Replace file" lines with no idea what
            # the agent actually did.
            edit = executor.last_edit
            diff = build_file_diff(*edit) if edit else None
            if diff is not None:
                diffs.append(diff)
                rendered = render_diff(diff)
                self.add_message(
                    session_id,
                    kind="diff",
                    role="",
                    content=rendered,
                    mission_id=mission_id,
                    metadata=diff.model_dump(mode="json"),
                )
            for path in paths:
                self.context.events.publish(
                    FileChanged(
                        mission_id=mission_id,
                        path=path,
                        action=action.action,
                        diff=render_diff(diff) if diff else "",
                        added=diff.added if diff else 0,
                        removed=diff.removed if diff else 0,
                    )
                )
            with self.context.database.session() as session:
                session.add(
                    ToolCall(
                        id=new_id("tool-call"),
                        mission_id=mission_id,
                        tool=f"chat.{action.action}",
                        arguments=action.model_dump(mode="json"),
                        result_summary=(result.error or "ok")[:1000],
                        duration_seconds=result.duration_seconds,
                        success=result.success,
                    )
                )
            persistent = self.memory.task_for_mission(mission_id)
            if persistent:
                observed_paths = paths or ([action.path] if action.path else [])
                updated = self.memory.record_action(
                    persistent.task_id,
                    action=action.action,
                    paths=observed_paths,
                    command=action.command if action.action == "run_command" else "",
                    success=result.success,
                    output=result.error or _tool_result_summary(result),
                    error=result.error or "",
                )
                if action.action == "resolve_command_failure" and result.success and updated.errors:
                    self.memory.remember_failure(
                        updated.errors[-1],
                        cause="Environment-specific command failure",
                        solution=f"Equivalent check passed: {action.evidence_command}",
                        context=updated.interpreted_goal or updated.original_request,
                        failed_attempts=[action.command],
                        task_id=updated.task_id,
                    )

        return observe

    def _record_chat_action_started(
        self, mission_id: str
    ) -> Callable[[AgentAction], None]:
        """Publish a safe action summary before a chat tool begins executing."""

        def started(action: AgentAction) -> None:
            self.context.events.publish(
                ToolStarted(
                    mission_id=mission_id,
                    tool=f"chat.{action.action}",
                    summary=_action_subject(action),
                )
            )

        return started

    def _team_context(
        self,
        instruction: str,
        token_budget: int | None = None,
        task_state_id: str | None = None,
        session_id: str | None = None,
        execution_profile: ModelExecutionProfile | None = None,
    ) -> ContextBundle:
        """Compile repository grounding for a team. Blocking; call in a thread."""
        from daino.context import ContextBuilder
        from daino.repository import RepositoryIndexer

        spec = TaskSpec(
            id="team",
            title=instruction[:80],
            objective=instruction,
            acceptance_criteria=["Carry out the instruction."],
            verification_commands=[],
        )
        root = self.context.root
        return ContextBuilder(
            root,
            self.context.settings,
            self.memory,
            indexer=RepositoryIndexer(root),
            token_budget=token_budget or self.context.settings.project.context_budget_tokens,
        ).build(
            spec,
            current_user_instruction=instruction,
            task_state_id=task_state_id,
            session_id=session_id,
            execution_profile=execution_profile,
        )

    async def team(
        self,
        instruction: str,
        session_id: str,
        *,
        profile_override: str = "",
    ) -> TeamOutcome:
        """Plan a team of sub-agents for one instruction and run it.

        Runs in an isolated workspace with a checkpoint taken first, exactly as a
        mission does: a team writes to more places at once than a single builder,
        so the way back has to exist before the first member starts.
        """
        if not self.core._role_available(ModelRole.PLANNER, profile_override):
            raise ConfigurationError(
                "No planner model is configured. Open Providers (Ctrl+P → Switch provider, "
                "or /provider) and route the planner role."
            )
        self.add_message(session_id, kind="user", role="user", content=instruction)
        mission = self.core.create(instruction, ProjectMode.DIRECT)
        self.attach_session_mission(session_id, mission.id)
        persistent = self.memory.task_for_mission(mission.id)
        if persistent:
            persistent = self.memory.update_task(
                persistent.task_id,
                session_id=session_id,
                interpreted_goal=instruction,
                status=PersistentTaskStatus.IN_PROGRESS,
            )
        gateway = self.core.gateway.with_profile(profile_override)
        budgeter = getattr(gateway, "context_budget", None)
        model_budget = (
            budgeter(ModelRole.BUILDER, tools=AGENT_TOOL_SPECS)
            if callable(budgeter)
            else self.context.settings.project.context_budget_tokens
        )
        profile_resolver = getattr(gateway, "execution_profile", None)
        execution_profile = (
            profile_resolver(ModelRole.BUILDER, tools=AGENT_TOOL_SPECS)
            if callable(profile_resolver)
            else None
        )
        context_reserve = min(2_048, max(512, model_budget // 4))
        context_budget = min(
            self.context.settings.project.context_budget_tokens,
            max(512, model_budget - context_reserve),
        )
        base_context = await asyncio.to_thread(
            self._team_context,
            instruction,
            context_budget,
            persistent.task_id if persistent else None,
            session_id,
            execution_profile,
        )

        try:
            plan = await TeamLead(gateway).plan(
                mission.id, instruction, base_context, profile_override=profile_override
            )
            waves = validate_team_plan(plan)
        except Exception as exc:
            self.core._update_mission(
                mission.id, status=MissionStatus.FAILED.value, failure=str(exc)
            )
            self.record_failure(session_id, mission.id, exc)
            raise

        self.context.events.publish(
            TeamPlanned(
                mission_id=mission.id,
                summary=plan.summary,
                members=[member.model_dump(mode="json") for member in plan.members],
            )
        )
        self.add_message(
            session_id,
            kind="summary",
            role="planner",
            content=_render_team_plan(plan, waves),
            mission_id=mission.id,
        )

        workspace = self.core.workspace_manager.create(
            mission.id, instruction, use_worktree=self.context.settings.git.use_worktrees
        )
        self.core._update_mission(
            mission.id,
            status=MissionStatus.RUNNING.value,
            workspace_path=str(workspace.path),
            branch=workspace.branch,
            initial_revision=workspace.initial_revision,
        )
        checkpoint_id, checkpoint_path = self.core.workspace_manager.checkpoint(
            workspace, "Before team changes", mission_id=mission.id
        )
        with self.context.database.session() as session:
            session.add(
                Checkpoint(
                    id=checkpoint_id,
                    mission_id=mission.id,
                    revision=workspace.initial_revision,
                    archive_path=str(checkpoint_path),
                    description="Before team changes",
                )
            )
        self.context.events.publish(
            CheckpointCreated(
                mission_id=mission.id,
                checkpoint_id=checkpoint_id,
                description="Before team changes",
            )
        )

        try:
            outcome = await TeamRunner(
                gateway,
                workspace.path,
                memory=self.memory,
                memory_task_id=persistent.task_id if persistent else None,
                memory_session_id=session_id,
            ).run(
                mission.id,
                plan,
                base_context,
                on_action=self._record_team_action(mission.id),
                on_action_start=self._record_team_action_started(mission.id),
                on_member=self._record_team_member(mission.id),
                on_member_start=self._announce_team_member(mission.id),
            )
        except Exception as exc:
            self.core._update_mission(
                mission.id, status=MissionStatus.FAILED.value, failure=str(exc)
            )
            self.record_failure(session_id, mission.id, exc)
            raise

        self.add_message(
            session_id,
            kind="summary",
            role="system",
            content=_render_team_outcome(outcome, workspace.path),
            mission_id=mission.id,
        )
        failed = [member for member in outcome.members if not member.success]
        self.core._update_mission(
            mission.id,
            status=(MissionStatus.FAILED if failed else MissionStatus.COMPLETED).value,
            failure="; ".join(f"{item.id}: {item.error}" for item in failed),
        )
        if persistent:
            self.memory.complete_task(
                persistent.task_id,
                summary=_render_team_outcome(outcome, workspace.path),
                outcome="failed" if failed else "completed",
            )
        return outcome.model_copy(update={"mission_id": mission.id})

    def _announce_team_member(self, mission_id: str) -> Callable[[TeamMember], None]:
        def started(member: TeamMember) -> None:
            self.context.events.publish(
                TeamMemberStarted(
                    mission_id=mission_id,
                    member=member.id,
                    role=member.role,
                    objective=member.objective,
                    scope=list(member.scope),
                    read_only=member.read_only,
                )
            )

        return started

    def _record_team_member(self, mission_id: str) -> Callable[[TeamMemberOutcome], None]:
        def finished(outcome: TeamMemberOutcome) -> None:
            self.context.events.publish(
                TeamMemberCompleted(
                    mission_id=mission_id,
                    member=outcome.id,
                    role=outcome.role,
                    summary=outcome.summary,
                    changed=list(outcome.changed),
                    steps=outcome.steps,
                    success=outcome.success,
                    error=outcome.error,
                )
            )

        return finished

    def _record_team_action(
        self, mission_id: str
    ) -> Callable[[TeamMember, AgentAction, ToolResult, list[str]], None]:
        """Audit a member's action on the same ledger a solo builder writes to."""

        def observe(
            member: TeamMember, action: AgentAction, result: ToolResult, paths: list[str]
        ) -> None:
            subject = action.path or action.query or action.summary or action.action
            tool = f"team.{member.id}.{action.action}"
            details = {"member": member.id, "role": member.role}
            if result.success:
                self.context.events.publish(
                    ToolCompleted(
                        mission_id=mission_id,
                        tool=tool,
                        summary=subject,
                        duration_seconds=result.duration_seconds,
                        details=details,
                    )
                )
            else:
                self.context.events.publish(
                    ToolFailed(
                        mission_id=mission_id,
                        tool=tool,
                        error=result.error or "action failed",
                        details=details,
                    )
                )
            for path in paths:
                self.context.events.publish(
                    FileChanged(
                        mission_id=mission_id, path=path, action=action.action, details=details
                    )
                )
            with self.context.database.session() as session:
                session.add(
                    ToolCall(
                        id=new_id("tool-call"),
                        mission_id=mission_id,
                        tool=tool,
                        arguments=action.model_dump(mode="json"),
                        result_summary=(result.error or "ok")[:1000],
                        duration_seconds=result.duration_seconds,
                        success=result.success,
                    )
                )
            persistent = self.memory.task_for_mission(mission_id)
            if persistent:
                observed_paths = paths or ([action.path] if action.path else [])
                self.memory.record_action(
                    persistent.task_id,
                    action=f"team.{member.id}.{action.action}",
                    paths=observed_paths,
                    command=action.command if action.action == "run_command" else "",
                    success=result.success,
                    output=result.error or _tool_result_summary(result),
                    error=result.error or "",
                )

        return observe

    def _record_team_action_started(
        self, mission_id: str
    ) -> Callable[[TeamMember, AgentAction], None]:
        """Publish member-attributed progress before a team tool starts."""

        def started(member: TeamMember, action: AgentAction) -> None:
            self.context.events.publish(
                ToolStarted(
                    mission_id=mission_id,
                    tool=f"team.{member.id}.{action.action}",
                    summary=_action_subject(action),
                    details={"member": member.id, "role": member.role},
                )
            )

        return started

    async def execute(
        self,
        mission_id: str,
        session_id: str,
        *,
        profile_override: str = "",
    ) -> tuple[Mission, Path | None]:
        mission = self.core.get(mission_id)
        if mission.status == MissionStatus.AWAITING_APPROVAL.value:
            result, evidence = await self.core.execute(
                mission_id,
                require_change_approval=True,
                profile_override=profile_override,
            )
        else:
            result, evidence = await self.core.resume(
                mission_id,
                require_change_approval=True,
                profile_override=profile_override,
            )
        if evidence:
            self.add_message(
                session_id,
                kind="summary",
                role="system",
                content=f"Mission {mission_id} completed. Evidence: {evidence}",
                mission_id=mission_id,
            )
        return result, evidence

    def approve(
        self,
        mission_id: str,
        *,
        approved: bool,
        scope: str = "once",
        category: str = "mission_execution",
    ) -> None:
        with self.context.database.session() as session:
            if session.get(Mission, mission_id) is None:
                raise ValueError(f"Unknown mission {mission_id}")
            session.add(
                Approval(
                    id=new_id("approval"),
                    mission_id=mission_id,
                    category=category,
                    subject=(
                        "Execute persisted mission plan"
                        if category == "mission_execution"
                        else "Approve reviewed mission changes"
                    ),
                    approved=approved,
                    approver=f"tui-user:{scope}",
                )
            )
            if not approved:
                mission = session.get(Mission, mission_id)
                if mission:
                    mission.status = MissionStatus.BLOCKED.value
                    mission.failure = (
                        "Plan rejected in TUI"
                        if category == "mission_execution"
                        else "Reviewed changes rejected in TUI"
                    )
        self.context.events.publish(
            ApprovalResolved(
                mission_id=mission_id,
                category=category,
                approved=approved,
                scope=scope,
            )
        )

    def approve_changes(
        self,
        mission_id: str,
        session_id: str,
        *,
        scope: str = "once",
    ) -> Path:
        self.approve(
            mission_id,
            approved=True,
            scope=scope,
            category="mission_changes",
        )
        evidence = self.core.finalize_changes(mission_id)
        self.add_message(
            session_id,
            kind="summary",
            role="system",
            content=f"Mission {mission_id} approved and committed. Evidence: {evidence}",
            mission_id=mission_id,
        )
        return evidence

    def cancel(self, mission_id: str, reason: str = "Cancelled by user") -> None:
        with self.context.database.session() as session:
            mission = session.get(Mission, mission_id)
            if mission is None:
                raise ValueError(f"Unknown mission {mission_id}")
            if mission.status == MissionStatus.COMPLETED.value:
                raise ValueError("A completed mission cannot be cancelled")
            mission.status = MissionStatus.CANCELLED.value
            mission.failure = reason
            for task in session.scalars(select(Task).where(Task.mission_id == mission_id)):
                if task.status != "completed":
                    task.status = "cancelled"
        self.context.events.publish(MissionPaused(mission_id=mission_id, reason=reason))
        persistent = self.memory.task_for_mission(mission_id)
        if persistent:
            self.memory.update_task(
                persistent.task_id,
                status=PersistentTaskStatus.CANCELLED,
                last_action="cancelled",
                unresolved_problems=[*persistent.unresolved_problems, reason],
            )

    def resumable_tasks(self) -> list[Any]:
        """Return crash-safe task snapshots for startup and ``/tasks`` flows."""
        return self.memory.resumable_tasks()

    def memory_command(self, arguments: str, session_id: str) -> str:
        """Execute the inspectable `/memory` command family for any presentation."""
        verb, _, rest = arguments.strip().partition(" ")
        verb = verb.casefold()
        rest = rest.strip()
        if verb == "search":
            if not rest:
                raise ValueError("Usage: /memory search <query>")
            items = self.memory.search(rest, include_stale=True, debug=True)
        elif verb in {"decisions", "failures", "user"}:
            selected = {
                "decisions": MemoryType.DECISION,
                "failures": MemoryType.FAILURE,
                "user": MemoryType.USER,
            }[verb]
            items = self.memory.list(memory_type=selected)
        elif verb == "project":
            items = self.memory.list(scope=MemoryScope.PROJECT)
        elif verb == "forget":
            if not rest:
                raise ValueError("Usage: /memory forget <memory-id>")
            self.memory.forget(rest)
            return f"Forgot memory {rest}."
        elif verb == "verify":
            if not rest:
                raise ValueError("Usage: /memory verify <memory-id>")
            self.memory.verify(rest)
            return f"Verified memory {rest} against its current source."
        elif verb == "clear-session":
            count = self.memory.clear(scope=MemoryScope.SESSION, session_id=session_id)
            return f"Cleared {count} session memory item(s)."
        elif verb == "clear-project":
            count = self.memory.clear(scope=MemoryScope.PROJECT)
            return f"Cleared {count} project memory item(s)."
        elif not verb:
            items = self.memory.list(limit=50)
        else:
            raise ValueError(
                "Usage: /memory [search <query>|project|decisions|failures|user|"
                "forget <id>|verify <id>|clear-session|clear-project]"
            )
        if not items:
            return "No matching memories."
        lines = ["Daino memory:"]
        for item in items:
            origin = item.source or "unknown source"
            stale = f", {item.status.value}" if item.status.value != "active" else ""
            lines.append(
                f"- `{item.id}` [{item.type.value}/{item.scope.value}{stale}] "
                f"{item.summary or item.content} — source: {origin}; "
                f"confidence: {item.confidence:.2f}"
            )
            if item.why:
                lines.append(f"  selected because: {', '.join(item.why)}")
        return "\n".join(lines)

    def task_command(self) -> str:
        items = self.resumable_tasks()
        if not items:
            return "No unfinished persistent tasks for this project."
        lines = ["Resumable tasks:"]
        for item in items:
            completed = len(item.completed_steps)
            total = completed + len(item.pending_steps) + bool(item.current_step)
            title = item.interpreted_goal or item.original_request
            lines.append(f"- `{item.task_id}` [{item.status.value}] {title}")
            lines.append(
                f"  Progress: {completed}/{total}; current: {item.current_step or 'not set'}; "
                f"last action: {item.last_action or 'none'}"
            )
        return "\n".join(lines)

    def _latest_task_for_session(self, session_id: str) -> Any | None:
        return next(
            (
                item
                for item in self.memory.resumable_tasks(limit=100)
                if item.session_id == session_id
            ),
            None,
        )

    def list_missions(self, limit: int = 100) -> list[MissionSummary]:
        with self.context.database.session() as session:
            missions = session.scalars(
                select(Mission)
                .where(Mission.project_id == self.context.database.project().id)
                .order_by(Mission.updated_at.desc())
                .limit(limit)
            ).all()
            result: list[MissionSummary] = []
            for mission in missions:
                tasks = session.scalars(select(Task).where(Task.mission_id == mission.id)).all()
                result.append(
                    MissionSummary(
                        id=mission.id,
                        title=mission.request,
                        status=mission.status,
                        mode=mission.mode,
                        updated_at=mission.updated_at,
                        branch=mission.branch or "",
                        workspace=mission.workspace_path or "",
                        task_counts=dict(Counter(task.status for task in tasks)),
                    )
                )
            return result

    def mission_details(self, mission_id: str) -> dict[str, Any]:
        with self.context.database.session() as session:
            mission = session.get(Mission, mission_id)
            if mission is None:
                raise ValueError(f"Unknown mission {mission_id}")
            requirements = session.scalars(
                select(RequirementVersion)
                .where(RequirementVersion.mission_id == mission_id)
                .order_by(RequirementVersion.version.desc())
            ).first()
            tasks = session.scalars(
                select(Task).where(Task.mission_id == mission_id).order_by(Task.created_at)
            ).all()
            tools = session.scalars(
                select(ToolCall)
                .where(ToolCall.mission_id == mission_id)
                .order_by(ToolCall.created_at)
            ).all()
            tests = session.scalars(
                select(VerificationRun)
                .where(VerificationRun.mission_id == mission_id)
                .order_by(VerificationRun.created_at)
            ).all()
            reviews = session.scalars(
                select(Review).where(Review.mission_id == mission_id).order_by(Review.created_at)
            ).all()
            approvals = session.scalars(
                select(Approval)
                .where(Approval.mission_id == mission_id)
                .order_by(Approval.created_at)
            ).all()
            checkpoints = session.scalars(
                select(Checkpoint)
                .where(Checkpoint.mission_id == mission_id)
                .order_by(Checkpoint.created_at)
            ).all()
            return {
                "mission": {
                    column.name: getattr(mission, column.name)
                    for column in Mission.__table__.columns
                },
                "requirements": requirements.content if requirements else {},
                "tasks": [
                    {
                        "id": item.id,
                        "title": item.title,
                        "status": item.status,
                        "risk_level": item.risk_level,
                        "evidence": item.evidence,
                    }
                    for item in tasks
                ],
                "tools": [
                    {
                        "tool": item.tool,
                        "summary": item.result_summary,
                        "success": item.success,
                        "duration": item.duration_seconds,
                    }
                    for item in tools
                ],
                "tests": [item.report for item in tests],
                "reviews": [item.report for item in reviews],
                "approvals": [
                    {
                        "category": item.category,
                        "subject": item.subject,
                        "approved": item.approved,
                    }
                    for item in approvals
                ],
                "checkpoints": [
                    {
                        "id": item.id,
                        "description": item.description,
                        "revision": item.revision,
                    }
                    for item in checkpoints
                ],
            }

    async def review(
        self,
        mission_id: str,
        session_id: str,
        *,
        profile_override: str = "",
    ) -> ReviewReport:
        """Run a fresh independent review of the active mission diff."""
        mission = self.core.get(mission_id)
        if not mission.workspace_path or not mission.initial_revision:
            raise RuntimeError("Mission does not have reviewable workspace changes")
        requirements, _ = self.core._load_plan(mission_id)
        from daino.git import GitClient

        diff = GitClient(Path(mission.workspace_path)).diff(mission.initial_revision)
        details = self.mission_details(mission_id)
        verification = json.dumps(details["tests"], indent=2, default=str)
        report = await ReviewerAgent(self.core._gateway(profile_override)).review(
            mission_id,
            requirements,
            requirements.acceptance_criteria,
            diff,
            verification,
        )
        with self.context.database.session() as session:
            session.add(
                Review(
                    id=new_id("review"),
                    mission_id=mission_id,
                    approved=report.approved,
                    report=report.model_dump(mode="json"),
                )
            )
        self.add_message(
            session_id,
            kind="agent" if report.approved else "error",
            role="reviewer",
            content=report.summary,
            mission_id=mission_id,
            metadata=report.model_dump(mode="json"),
        )
        return report

    def record_failure(self, session_id: str, mission_id: str | None, exc: Exception) -> None:
        self.add_message(
            session_id,
            kind="error",
            role="system",
            content=str(exc),
            mission_id=mission_id,
        )
        self.context.events.publish(MissionFailed(mission_id=mission_id, error=str(exc)))


def _render_team_plan(plan: TeamPlan, waves: list[list[TeamMember]]) -> str:
    """Show the roster as the waves it will actually run in."""
    lines = [f"Team plan: {plan.summary}", ""]
    for index, wave in enumerate(waves, start=1):
        concurrency = "runs alone" if len(wave) == 1 else f"{len(wave)} members in parallel"
        lines.append(f"Wave {index} ({concurrency}):")
        for member in wave:
            scope = ", ".join(member.scope) if member.scope else "read-only"
            lines.append(f"  {member.id} [{member.role}] {member.objective}")
            lines.append(f"      scope: {scope}")
    return "\n".join(lines)


def _render_team_outcome(outcome: TeamOutcome, workspace: Path) -> str:
    lines = [f"Team finished in {workspace}.", ""]
    for member in outcome.members:
        mark = "ok" if member.success else "failed"
        lines.append(f"[{mark}] {member.id} ({member.role}), {member.steps} steps")
        detail = member.summary if member.success else member.error
        if detail:
            lines.append(f"      {detail}")
    if outcome.changed:
        lines.extend(["", "Files changed:", *(f"  {path}" for path in outcome.changed)])
    else:
        lines.extend(["", "No files were changed."])
    return "\n".join(lines)


#: Output lines kept when echoing a command into the transcript. The agent gets
#: the full result; the reader gets enough to see what happened.
_COMMAND_OUTPUT_LINES = 12


def _describe_action(action: AgentAction, result: ToolResult) -> str:
    """Render a non-editing action for the transcript, or "" if it needs no line.

    Edits speak for themselves through their diff. Everything else — running a
    command, searching, planning — is invisible unless it is said out loud.
    """
    data = result.data or {}
    if action.action == "run_command":
        header = f"$ {action.command}"
        if not result.success:
            failure = data.get("stderr") or data.get("stdout") or result.error
            return f"{header}\n{_tail(str(failure))}"
        body = _tail(str(data.get("stdout") or data.get("stderr") or ""))
        return f"{header}\n{body}" if body else header
    if action.action == "resolve_command_failure":
        if not result.success:
            return f"could not resolve $ {action.command}\n{result.error or 'evidence rejected'}"
        return (
            f"resolved failed check $ {action.command}\n"
            f"using successful evidence $ {action.evidence_command}"
        )
    if action.action == "glob":
        return f"glob {action.pattern} · {data.get('count', 0)} file(s)"
    if action.action == "grep":
        return f"grep {action.query} · {len(data.get('matches') or [])} match(es)"
    if action.action == "web_search":
        results = data.get("results") or []
        if not result.success:
            return f"web search {action.query}\n{result.error or 'search failed'}"
        sources = "\n".join(
            f"  {item.get('title', 'Untitled')} — {item.get('url', '')}"
            for item in results[:5]
            if isinstance(item, dict)
        )
        return f"web search {action.query} · {len(results)} result(s)\n{sources}".rstrip()
    if action.action == "fetch_url":
        if not result.success:
            return f"fetch {action.url}\n{result.error or 'fetch failed'}"
        title = str(data.get("title") or data.get("url") or action.url)
        return f"fetched {title} · {len(str(data.get('content') or '')):,} characters"
    if action.action == "todo":
        marks = {"completed": "x", "in_progress": ">", "pending": " ", "failed": "!"}
        return "\n".join(f"[{marks.get(item.status, ' ')}] {item.content}" for item in action.todos)
    return ""


def _action_subject(action: AgentAction) -> str:
    """Describe an action without exposing the model's private thought field."""
    return (
        action.path
        or action.query
        or action.command
        or action.url
        or action.pattern
        or action.summary
        or action.action.replace("_", " ")
    )


def _tail(text: str) -> str:
    lines = text.strip().splitlines()
    if len(lines) <= _COMMAND_OUTPUT_LINES:
        return "\n".join(lines)
    dropped = len(lines) - _COMMAND_OUTPUT_LINES
    return "\n".join([f"… {dropped} earlier line(s) …", *lines[-_COMMAND_OUTPUT_LINES:]])


def _tool_result_summary(result: ToolResult) -> str:
    data = result.data or {}
    if not data:
        return "ok"
    return "; ".join(f"{key}: {value}" for key, value in data.items() if key != "content")[:2_000]
