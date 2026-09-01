"""Add workspaces: the named container for knowledge work.

Revision ID: 0006
Revises: 0005
"""

from sqlalchemy import Column, String, inspect

from alembic import op
from daino.persistence.models import Workspace, WorkspaceSource, WorkspaceTask

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

#: The one link that gives a workspace its history. Nullable, so every existing
#: conversation stays valid and unattached.
_SESSION_WORKSPACE = Column("workspace_id", String(64), nullable=True)


def upgrade() -> None:
    bind = op.get_bind()
    existing_tables = set(inspect(bind).get_table_names())
    for table in (
        Workspace.__table__,
        WorkspaceTask.__table__,
        WorkspaceSource.__table__,
    ):
        if table.name not in existing_tables:
            table.create(bind=bind)

    columns = {item["name"] for item in inspect(bind).get_columns("conversation_sessions")}
    if _SESSION_WORKSPACE.name not in columns:
        op.add_column("conversation_sessions", _SESSION_WORKSPACE.copy())

    indexes = {item["name"] for item in inspect(bind).get_indexes("conversation_sessions")}
    if "ix_conversation_sessions_workspace_id" not in indexes:
        op.create_index(
            "ix_conversation_sessions_workspace_id",
            "conversation_sessions",
            ["workspace_id"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    indexes = {item["name"] for item in inspect(bind).get_indexes("conversation_sessions")}
    if "ix_conversation_sessions_workspace_id" in indexes:
        op.drop_index("ix_conversation_sessions_workspace_id", "conversation_sessions")
    columns = {item["name"] for item in inspect(bind).get_columns("conversation_sessions")}
    if _SESSION_WORKSPACE.name in columns:
        op.drop_column("conversation_sessions", _SESSION_WORKSPACE.name)

    existing_tables = set(inspect(bind).get_table_names())
    for table in (
        WorkspaceSource.__table__,
        WorkspaceTask.__table__,
        Workspace.__table__,
    ):
        if table.name in existing_tables:
            table.drop(bind=bind)
