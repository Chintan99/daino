"""What a run may do on its own, and what it has to ask about.

An executing plan is a different proposition from a chat turn: nobody is
watching each step, so "the user is right there and will notice" stops being
true. But asking about everything is worse than asking about nothing — a person
who has clicked Allow eleven times for a file write clicks the twelfth without
reading it, which is exactly when the destructive one arrives.

So actions are classified rather than counted. Reading and writing inside the
workspace is what the user asked for and proceeds silently; deleting, reaching
outside the workspace, and running commands are the three things that can cost
something, and those ask.

The classification is deliberately separate from :class:`CommandGate`, which
decides whether a *shell command* is safe. This decides whether an *agent
action* needs a person. Shell commands pass through both, and the gate has the
final word — it can refuse what this would merely ask about.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from daino.config.models import Settings


class ApprovalLevel(StrEnum):
    """How much an action can cost if it was the wrong one."""

    #: Reading anything, searching, listing. Free and reversible.
    SAFE_READ = "safe_read"
    #: Creating or editing an artifact inside the workspace folder. Recorded as
    #: a revision, so it is undoable — and it is the work itself.
    WORKSPACE_WRITE = "workspace_write"
    #: Running something on this machine. Bounded by the command gate, but a
    #: person may still want to see it first.
    LOCAL_EXECUTION = "local_execution"
    #: Anything that leaves this machine, or writes outside the workspace.
    EXTERNAL_ACTION = "external_action"
    #: Deleting, overwriting history, or anything with no way back.
    DESTRUCTIVE = "destructive"


#: What each level does by default while a run is executing unattended.
#: ``LOCAL_EXECUTION`` is the configurable one: a project that has already
#: decided its commands are safe should not be asked per task.
DEFAULT_POLICY: dict[ApprovalLevel, bool] = {
    ApprovalLevel.SAFE_READ: False,
    ApprovalLevel.WORKSPACE_WRITE: False,
    ApprovalLevel.LOCAL_EXECUTION: True,
    ApprovalLevel.EXTERNAL_ACTION: True,
    ApprovalLevel.DESTRUCTIVE: True,
}

#: Action name -> level. Keys are ``AgentAction.action`` values, and
#: ``test_every_agent_action_is_classified`` holds them to that: a third of this
#: table once named tools that do not exist — ``delete_file`` for what is really
#: ``delete``, ``design_create`` for ``create_design``, ``write_file`` for
#: ``write`` — so the entries that mattered most never matched anything and the
#: actions they were meant to describe fell through to the default instead.
#:
#: Anything absent is still treated as ``EXTERNAL_ACTION``, so an unclassified
#: tool is asked about rather than waved through. That is a net, not the plan:
#: the test above requires every action to be listed, so classifying a new tool
#: is a deliberate edit here rather than something the default quietly absorbs.
_ACTION_LEVELS: dict[str, ApprovalLevel] = {
    # Reading, searching, and looking things up. Free and reversible.
    "read_file": ApprovalLevel.SAFE_READ,
    "list_directory": ApprovalLevel.SAFE_READ,
    "search_text": ApprovalLevel.SAFE_READ,
    "glob": ApprovalLevel.SAFE_READ,
    "grep": ApprovalLevel.SAFE_READ,
    "read_image": ApprovalLevel.SAFE_READ,
    "workspace_read": ApprovalLevel.SAFE_READ,
    "read_design": ApprovalLevel.SAFE_READ,
    "read_design_artifact": ApprovalLevel.SAFE_READ,
    "find_definition": ApprovalLevel.SAFE_READ,
    "find_references": ApprovalLevel.SAFE_READ,
    "diagnostics": ApprovalLevel.SAFE_READ,
    "memory_search": ApprovalLevel.SAFE_READ,
    "memory_list": ApprovalLevel.SAFE_READ,
    "memory_verify": ApprovalLevel.SAFE_READ,
    "web_search": ApprovalLevel.SAFE_READ,
    "fetch_url": ApprovalLevel.SAFE_READ,
    # Loading project instructions into the turn: text in, nothing out.
    "skill": ApprovalLevel.SAFE_READ,
    # Bookkeeping inside the run rather than actions on anything: the plan the
    # agent is tracking, a note that an already-run command covered a failure,
    # and the two ways a turn ends.
    "todo": ApprovalLevel.SAFE_READ,
    "resolve_command_failure": ApprovalLevel.SAFE_READ,
    "respond": ApprovalLevel.SAFE_READ,
    "finish": ApprovalLevel.SAFE_READ,
    # Writing an artifact. Recorded as a revision, so it is undoable — and
    # `level_for` re-reads a file write's path, so one aimed outside the
    # workspace folder is reclassified as an external action and asks.
    "write": ApprovalLevel.WORKSPACE_WRITE,
    "replace": ApprovalLevel.WORKSPACE_WRITE,
    "multi_edit": ApprovalLevel.WORKSPACE_WRITE,
    "workspace_plan": ApprovalLevel.WORKSPACE_WRITE,
    "workspace_task": ApprovalLevel.WORKSPACE_WRITE,
    "workspace_deliverable": ApprovalLevel.WORKSPACE_WRITE,
    "workspace_link": ApprovalLevel.WORKSPACE_WRITE,
    "memory_save": ApprovalLevel.WORKSPACE_WRITE,
    "memory_update": ApprovalLevel.WORKSPACE_WRITE,
    "memory_forget": ApprovalLevel.WORKSPACE_WRITE,
    # Design edits, including the deletions: a design keeps every version and
    # can be restored, so removing a node or a frame is undoable in exactly the
    # sense this level means.
    "create_design": ApprovalLevel.WORKSPACE_WRITE,
    "update_design": ApprovalLevel.WORKSPACE_WRITE,
    "add_design_node": ApprovalLevel.WORKSPACE_WRITE,
    "update_design_node": ApprovalLevel.WORKSPACE_WRITE,
    "delete_design_node": ApprovalLevel.WORKSPACE_WRITE,
    "connect_design_nodes": ApprovalLevel.WORKSPACE_WRITE,
    "disconnect_design_nodes": ApprovalLevel.WORKSPACE_WRITE,
    "add_design_frame": ApprovalLevel.WORKSPACE_WRITE,
    "update_design_frame": ApprovalLevel.WORKSPACE_WRITE,
    "delete_design_frame": ApprovalLevel.WORKSPACE_WRITE,
    # Running something on this machine.
    "run_command": ApprovalLevel.LOCAL_EXECUTION,
    "workspace_code": ApprovalLevel.LOCAL_EXECUTION,
    # An MCP server and a subagent can both do anything at all, and neither is
    # something this can inspect beforehand.
    "call_tool": ApprovalLevel.EXTERNAL_ACTION,
    "delegate": ApprovalLevel.EXTERNAL_ACTION,
    # Deleting a file. The one action with no way back.
    "delete": ApprovalLevel.DESTRUCTIVE,
}


class ApprovalPolicy:
    """Decides whether one action needs a person before it happens.

    Built per run from the project's settings so a change to
    ``security.require_approval_for_*`` takes effect on the next run rather than
    needing a restart, and so a future tool — email, a browser, an external
    app — only has to name its level to be gated correctly.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.policy = dict(DEFAULT_POLICY)
        if settings is not None:
            security = settings.security
            # Network access is already gated inside the web tool by this same
            # setting; mirroring it here keeps one answer for one question.
            if not security.require_approval_for_network:
                self.policy[ApprovalLevel.EXTERNAL_ACTION] = False
            if not security.require_approval_for_install:
                self.policy[ApprovalLevel.LOCAL_EXECUTION] = False

    def level_for(self, action: str, arguments: dict[str, Any] | None = None) -> ApprovalLevel:
        """Classify one action, sharpening the answer with its arguments.

        A write is a workspace write only while it lands inside the workspace.
        The same tool aimed at ``src/main.py`` is an edit to the user's source
        tree, which a knowledge-work run has no business making unasked.
        """
        level = _ACTION_LEVELS.get(action, ApprovalLevel.EXTERNAL_ACTION)
        if level is not ApprovalLevel.WORKSPACE_WRITE:
            return level
        folder = str((arguments or {}).get("__workspace_folder", "")).strip("/")
        path = str((arguments or {}).get("path", "")).strip().lstrip("/")
        if not folder or not path:
            return level
        return level if path.startswith(f"{folder}/") else ApprovalLevel.EXTERNAL_ACTION

    def needs_approval(self, action: str, arguments: dict[str, Any] | None = None) -> bool:
        return self.policy.get(self.level_for(action, arguments), True)

    def describe(self, action: str, arguments: dict[str, Any] | None = None) -> str:
        """A sentence a non-developer can decide on."""
        arguments = arguments or {}
        path = str(arguments.get("path", "")).strip()
        if action == "delete":
            return f"Delete {path or 'a file'}"
        if action == "run_command":
            return f"Run: {str(arguments.get('command', '')).strip()[:200]}"
        if action == "fetch_url":
            return f"Read {str(arguments.get('url', '')).strip()[:200]}"
        if path:
            return f"{action.replace('_', ' ').capitalize()} {path}"
        return action.replace("_", " ").capitalize()

    def reason(self, action: str, arguments: dict[str, Any] | None = None) -> str:
        """Why this one is being asked about, in the user's terms."""
        level = self.level_for(action, arguments)
        return {
            ApprovalLevel.DESTRUCTIVE: "This cannot be undone from the workspace history.",
            ApprovalLevel.LOCAL_EXECUTION: "This runs a command on your machine.",
            ApprovalLevel.EXTERNAL_ACTION: ("This reaches outside the workspace folder."),
        }.get(level, "This needs your confirmation.")
