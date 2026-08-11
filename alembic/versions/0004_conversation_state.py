"""Persist interaction mode and the live task checklist.

Revision ID: 0004
Revises: 0003
"""

from sqlalchemy import inspect

from alembic import op
from vasuki.persistence.models import ConversationState

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if ConversationState.__tablename__ not in inspect(bind).get_table_names():
        ConversationState.__table__.create(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    if ConversationState.__tablename__ in inspect(bind).get_table_names():
        ConversationState.__table__.drop(bind=bind)
