"""Add workspace runs: executing a plan rather than only recording one.

Revision ID: 0007
Revises: 0006
"""

from sqlalchemy import JSON, Column, Integer, Text, inspect

from alembic import op
from daino.persistence.models import (
    WorkspaceChangeEntry,
    WorkspaceChangeSet,
    WorkspaceLink,
    WorkspaceRun,
    WorkspaceRunStep,
)

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

_TABLES = (
    WorkspaceRun.__table__,
    WorkspaceRunStep.__table__,
    WorkspaceChangeSet.__table__,
    WorkspaceChangeEntry.__table__,
    WorkspaceLink.__table__,
)

#: What an executable plan needs that a written one did not: which steps block
#: this one, how often it has been tried, and why it last failed.
_TASK_COLUMNS = (
    Column("depends_on", JSON(), nullable=False, server_default="[]"),
    Column("attempts", Integer(), nullable=False, server_default="0"),
    Column("error", Text(), nullable=False, server_default=""),
)


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names())
    for table in _TABLES:
        if table.name not in existing:
            table.create(bind=bind)

    if "workspace_tasks" not in existing:
        return
    columns = {item["name"] for item in inspect(bind).get_columns("workspace_tasks")}
    for column in _TASK_COLUMNS:
        if column.name not in columns:
            op.add_column("workspace_tasks", column.copy())


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names())
    if "workspace_tasks" in existing:
        columns = {item["name"] for item in inspect(bind).get_columns("workspace_tasks")}
        for column in _TASK_COLUMNS:
            if column.name in columns:
                op.drop_column("workspace_tasks", column.name)
    for table in reversed(_TABLES):
        if table.name in existing:
            table.drop(bind=bind)
