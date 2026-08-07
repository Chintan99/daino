"""Persist TUI sessions, messages, and typed events.

Revision ID: 0002
Revises: 0001
"""

from sqlalchemy import inspect

from alembic import op
from vasuki.persistence.models import ConversationMessage, ConversationSession, MissionEventRecord

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names())
    for table in (
        ConversationSession.__table__,
        ConversationMessage.__table__,
        MissionEventRecord.__table__,
    ):
        if table.name not in existing:
            table.create(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names())
    for table in (
        MissionEventRecord.__table__,
        ConversationMessage.__table__,
        ConversationSession.__table__,
    ):
        if table.name in existing:
            table.drop(bind=bind)
