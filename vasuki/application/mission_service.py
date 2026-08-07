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

from sqlalchemy import select

from vasuki.agents import ReviewerAgent, TeamLead, TeamRunner, validate_team_plan
from vasuki.agents.loop import ToolLoop
from vasuki.agents.tool_schemas import CHAT_TOOL_SPECS
from vasuki.application.context import ProjectContext
from vasuki.application.view_models import ConversationItem, MissionSummary
from vasuki.events import (
    ApprovalResolved,
    CheckpointCreated,
    FileChanged,
    MissionFailed,
    MissionPaused,
    ModelStreamChunk,
    TeamMemberCompleted,
    TeamMemberStarted,
    TeamPlanned,
    ToolCompleted,
    ToolFailed,
)
from vasuki.exceptions import ConfigurationError
from vasuki.missions import MissionService
from vasuki.model_router import ModelRole
from vasuki.persistence.models import (
    Approval,
    Checkpoint,
    ConversationMessage,
    ConversationSession,
    Mission,
    RequirementVersion,
    Review,
    Task,
    ToolCall,
    VerificationRun,
)
from vasuki.prompts import CHAT_AGENT_SYSTEM
from vasuki.runtimes.base import Runtime
from vasuki.runtimes.detect import docker_status
from vasuki.schemas import (
    AgentAction,
    ChatOutcome,
    ContextBundle,
    FileDiff,
    Message,
    MissionStatus,
    ProjectMode,
    ReviewReport,
    TaskSpec,
    TeamMember,
    TeamMemberOutcome,
    TeamOutcome,
    TeamPlan,
    ToolResult,
)
from vasuki.security.commands import CommandGate
from vasuki.tools import EditTools, RecordingActionExecutor, build_file_diff
from vasuki.tools.commands import ApprovalCallback, CommandRunner
from vasuki.tools.diffing import render as render_diff
from vasuki.utils.ids import new_id


class MissionApplicationService:
    """Facade used by the TUI and suitable for thin CLI handlers."""

    def __init__(self, context: ProjectContext) -> None:
        self.context = context
        self.core = MissionService(
            context.root,
            context.settings,
            context.database,
            events=context.events,
        )
        #: Command approval memory per conversation session.
        self._command_gates: dict[str, CommandGate] = {}

    def create_session(
        self,
        title: str = "General repository questions",
        *,
        mission_id: str | None = None,
    ) -> str:
        item = ConversationSession(
            id=new_id("session"),
            project_id=self.context.database.project().id,
            mission_id=mission_id,
            title=title[:255],
        )
        with self.context.database.session() as session:
            session.add(item)
        return item.id

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
            session.add(message)
        return message

    def messages(self, session_id: str, limit: int = 500) -> list[ConversationItem]:
        with self.context.database.session() as session:
            items = session.scalars(
                select(ConversationMessage)
                .where(ConversationMessage.session_id == session_id)
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
        from vasuki.repository import RepositoryIndexer

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
            from vasuki.playbooks import PlaybookLoader

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
        from vasuki.repository import RepositoryIndexer

        return (
            RepositoryIndexer(self.context.root).summary(),
            self._supplemental_context(session_id, question),
        )

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
                    "You are Vasuki, an engineering assistant for this repository. "
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
        history = self.conversation_history(session_id)
        # Read before this turn is persisted, or the instruction would appear
        # twice: once as history and again as the task.
        history = self.conversation_history(session_id)
        self.add_message(session_id, kind="user", role="user", content=instruction)
        mission = self.core.create(instruction, ProjectMode.DIRECT)
        # Taken before the first edit so /restore always has a way back; the
        # agent writes to the real working tree, not a worktree.
        self._checkpoint_working_tree(mission.id)
        base_context = await asyncio.to_thread(self._team_context, instruction)

        editor = EditTools(
            self.context.root,
            require_read_before_write=True,
            seen_files=set(base_context.included_paths),
        )
        runtime, runner = await self._command_runner(session_id, approve)
        executor = RecordingActionExecutor(editor, runner)
        loop = ToolLoop(
            self.core.gateway.with_profile(profile_override),
            ModelRole.BUILDER,
            executor,
            system=CHAT_AGENT_SYSTEM,
            tools=CHAT_TOOL_SPECS,
        )
        diffs: list[FileDiff] = []
        try:
            result = await loop.run(
                mission.id,
                base_context,
                on_action=self._record_chat_action(mission.id, session_id, executor, diffs),
                history=history,
            )
        except Exception as exc:
            self.core._update_mission(
                mission.id, status=MissionStatus.FAILED.value, failure=str(exc)
            )
            self.context.events.publish(MissionFailed(mission_id=mission.id, error=str(exc)))
            raise
        finally:
            if runtime is not None:
                # A container started for this turn must not outlive it.
                await runtime.cleanup()

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
        # No diffs are written here: each edit already posted its own as it
        # landed, so repeating them would show every change twice.
        if outcome.summary:
            self.add_message(
                session_id,
                kind="agent",
                role=ModelRole.BUILDER.value,
                content=outcome.summary,
                mission_id=mission.id,
            )

        commands = result.implementation.verification_commands
        if result.changed and commands:
            await self._verify_chat_edit(outcome, commands, session_id, mission.id)
        self.core._update_mission(mission.id, status=MissionStatus.COMPLETED.value)
        return outcome

    def _checkpoint_working_tree(self, mission_id: str) -> None:
        """Snapshot the tree before the agent edits it, without blocking the turn."""
        from vasuki.application.checkpoint_service import CheckpointApplicationService

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
    ) -> None:
        """Run the checks the agent itself proposed and report the result."""
        from vasuki.application.verification_service import VerificationApplicationService

        try:
            report = await VerificationApplicationService(self.context).run(commands)
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
            return
        else:
            outcome.verified = bool(getattr(report, "passed", False))
            failures = getattr(report, "failures", [])
            outcome.verification_summary = (
                f"{len(commands)} check(s) passed"
                if outcome.verified
                else "; ".join(str(getattr(item, "command", item)) for item in failures)
                or "verification failed"
            )
        self.add_message(
            session_id,
            kind="test" if outcome.verified else "error",
            role="tester",
            content=outcome.verification_summary,
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

        return observe

    def _team_context(self, instruction: str) -> ContextBundle:
        """Compile repository grounding for a team. Blocking; call in a thread."""
        from vasuki.context import ContextCompiler
        from vasuki.repository import RepositoryIndexer

        spec = TaskSpec(
            id="team",
            title=instruction[:80],
            objective=instruction,
            acceptance_criteria=["Carry out the instruction."],
            verification_commands=[],
        )
        root = self.context.root
        return ContextCompiler(root, RepositoryIndexer(root)).compile(spec)

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
        gateway = self.core.gateway.with_profile(profile_override)
        base_context = await asyncio.to_thread(self._team_context, instruction)

        try:
            plan = await TeamLead(gateway).plan(
                mission.id, instruction, base_context, profile_override=profile_override
            )
            waves = validate_team_plan(plan)
        except Exception as exc:
            self.core._update_mission(
                mission.id, status=MissionStatus.FAILED.value, failure=str(exc)
            )
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
            outcome = await TeamRunner(gateway, workspace.path).run(
                mission.id,
                plan,
                base_context,
                on_action=self._record_team_action(mission.id),
                on_member=self._record_team_member(mission.id),
                on_member_start=self._announce_team_member(mission.id),
            )
        except Exception as exc:
            self.core._update_mission(
                mission.id, status=MissionStatus.FAILED.value, failure=str(exc)
            )
            self.context.events.publish(MissionFailed(mission_id=mission.id, error=str(exc)))
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

        return observe

    async def execute(
        self,
        mission_id: str,
        session_id: str,
        *,
        profile_override: str = "",
    ) -> tuple[Mission, Path | None]:
        result, evidence = await self.core.execute(
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
        from vasuki.git import GitClient

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
    if action.action == "glob":
        return f"glob {action.pattern} · {data.get('count', 0)} file(s)"
    if action.action == "grep":
        return f"grep {action.query} · {len(data.get('matches') or [])} match(es)"
    if action.action == "todo":
        marks = {"completed": "x", "in_progress": ">", "pending": " "}
        return "\n".join(f"[{marks.get(item.status, ' ')}] {item.content}" for item in action.todos)
    return ""


def _tail(text: str) -> str:
    lines = text.strip().splitlines()
    if len(lines) <= _COMMAND_OUTPUT_LINES:
        return "\n".join(lines)
    dropped = len(lines) - _COMMAND_OUTPUT_LINES
    return "\n".join([f"… {dropped} earlier line(s) …", *lines[-_COMMAND_OUTPUT_LINES:]])
