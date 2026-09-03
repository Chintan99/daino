"""The unattended-run approval gate, and the table it decides from.

The table is keyed by ``AgentAction.action``, and nothing was checking that.
A third of it named tools that do not exist — ``delete_file`` where the real
action is ``delete``, ``design_create`` for ``create_design``, ``write_file``
for ``write`` — so the entries doing the most important work matched nothing,
and 28 real actions had no entry at all. Both halves are asserted here, because
a mismatch is invisible at runtime: an unclassified action just falls through to
"ask", which looks like caution rather than like a broken lookup.
"""

from __future__ import annotations

import typing

import pytest

from daino.config.models import Settings
from daino.schemas import AgentAction
from daino.workbench.approvals import _ACTION_LEVELS, ApprovalLevel, ApprovalPolicy

AGENT_ACTIONS = frozenset(typing.get_args(AgentAction.model_fields["action"].annotation))


def test_every_policy_key_is_a_real_action() -> None:
    """A key nothing emits classifies nothing."""
    assert sorted(set(_ACTION_LEVELS) - AGENT_ACTIONS) == []


def test_every_agent_action_is_classified() -> None:
    """A new tool has to be classified deliberately, not absorbed by the default."""
    assert sorted(AGENT_ACTIONS - set(_ACTION_LEVELS)) == []


def test_deleting_a_file_is_destructive() -> None:
    """The case the dead ``delete_file`` key was written for."""
    policy = ApprovalPolicy()

    assert policy.level_for("delete", {"path": "notes/draft.md"}) is ApprovalLevel.DESTRUCTIVE
    assert policy.needs_approval("delete", {"path": "notes/draft.md"})
    assert policy.describe("delete", {"path": "notes/draft.md"}) == "Delete notes/draft.md"


def test_a_design_edit_no_longer_asks() -> None:
    """Design work is the run's own output, and every version is restorable."""
    policy = ApprovalPolicy()

    for action in ("create_design", "add_design_node", "add_design_frame"):
        assert policy.level_for(action) is ApprovalLevel.WORKSPACE_WRITE, action
        assert not policy.needs_approval(action), action


def test_reading_never_asks() -> None:
    policy = ApprovalPolicy()

    for action in ("read_file", "find_references", "read_design", "skill", "memory_list"):
        assert not policy.needs_approval(action), action


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("run_command", ApprovalLevel.LOCAL_EXECUTION),
        ("workspace_code", ApprovalLevel.LOCAL_EXECUTION),
        ("call_tool", ApprovalLevel.EXTERNAL_ACTION),
        ("delegate", ApprovalLevel.EXTERNAL_ACTION),
    ],
)
def test_anything_that_can_reach_outside_still_asks(action: str, expected: ApprovalLevel) -> None:
    policy = ApprovalPolicy()

    assert policy.level_for(action) is expected
    assert policy.needs_approval(action)


def test_a_write_outside_the_workspace_folder_is_reclassified() -> None:
    """The path check that makes ``workspace_write`` mean what it says."""
    policy = ApprovalPolicy()
    inside = {"path": "research/report.md", "__workspace_folder": "research"}
    outside = {"path": "src/main.py", "__workspace_folder": "research"}

    assert policy.level_for("write", inside) is ApprovalLevel.WORKSPACE_WRITE
    assert not policy.needs_approval("write", inside)
    assert policy.level_for("write", outside) is ApprovalLevel.EXTERNAL_ACTION
    assert policy.needs_approval("write", outside)


def test_a_project_that_trusts_its_commands_is_not_asked_per_step() -> None:
    settings = Settings()
    settings.security.require_approval_for_install = False

    assert not ApprovalPolicy(settings).needs_approval("run_command", {"command": "pytest"})
    assert ApprovalPolicy().needs_approval("run_command", {"command": "pytest"})
