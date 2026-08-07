"""Persist session-scoped file context.

Revision ID: 0003
Revises: 0002
"""

from sqlalchemy import JSON, Column, inspect

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in inspect(bind).get_columns("conversation_sessions")}
    if "context_files" not in columns:
        op.add_column(
            "conversation_sessions",
            Column("context_files", JSON(), nullable=False, server_default="[]"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {item["name"] for item in inspect(bind).get_columns("conversation_sessions")}
    if "context_files" in columns:
        op.drop_column("conversation_sessions", "context_files")
