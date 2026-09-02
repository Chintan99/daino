"""Record how much of a prompt the provider served from its cache.

Revision ID: 0008
Revises: 0007
"""

from sqlalchemy import Column, Integer, inspect

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

#: A turn that reuses its prefix and one that pays for it on every call bill
#: very differently and look identical without this.
_CACHED_TOKENS = Column("cached_tokens", Integer(), nullable=False, server_default="0")


def upgrade() -> None:
    bind = op.get_bind()
    if "model_calls" not in set(inspect(bind).get_table_names()):
        return
    columns = {item["name"] for item in inspect(bind).get_columns("model_calls")}
    if _CACHED_TOKENS.name not in columns:
        op.add_column("model_calls", _CACHED_TOKENS.copy())


def downgrade() -> None:
    bind = op.get_bind()
    if "model_calls" not in set(inspect(bind).get_table_names()):
        return
    columns = {item["name"] for item in inspect(bind).get_columns("model_calls")}
    if _CACHED_TOKENS.name in columns:
        op.drop_column("model_calls", _CACHED_TOKENS.name)
