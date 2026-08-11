"""Add the multi-layer memory and crash-safe task state schema.

Revision ID: 0005
Revises: 0004
"""

from sqlalchemy import JSON, Column, DateTime, Float, Integer, String, Text, inspect

from alembic import op
from vasuki.persistence.models import MemoryEmbedding, MemoryEpisode, PersistentTaskState

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


_MEMORY_COLUMNS = (
    Column("memory_type", String(32), nullable=False, server_default="semantic"),
    Column("task_id", String(64), nullable=True),
    Column("session_id", String(64), nullable=True),
    Column("summary", Text(), nullable=False, server_default=""),
    Column("importance", Float(), nullable=False, server_default="0.5"),
    Column("source_type", String(32), nullable=False, server_default="agent"),
    Column("tags", JSON(), nullable=False, server_default="[]"),
    Column("status", String(32), nullable=False, server_default="active"),
    Column("last_accessed", DateTime(timezone=True), nullable=True),
    Column("last_verified", DateTime(timezone=True), nullable=True),
    Column("access_count", Integer(), nullable=False, server_default="0"),
    Column("project_revision", String(64), nullable=True),
    Column("source_digest", String(128), nullable=True),
    Column("superseded_by", String(64), nullable=True),
    Column("rationale", Text(), nullable=False, server_default=""),
)


def upgrade() -> None:
    bind = op.get_bind()
    existing_tables = set(inspect(bind).get_table_names())
    for table in (
        MemoryEmbedding.__table__,
        PersistentTaskState.__table__,
        MemoryEpisode.__table__,
    ):
        if table.name not in existing_tables:
            table.create(bind=bind)

    columns = {item["name"] for item in inspect(bind).get_columns("memory_records")}
    for column in _MEMORY_COLUMNS:
        if column.name not in columns:
            op.add_column("memory_records", column.copy())

    # Explicit indexes keep the cheap filtering stage cheap even on a long-lived
    # user database. SQLite accepts these independently of Alembic batch mode.
    indexes = {item["name"] for item in inspect(bind).get_indexes("memory_records")}
    for name, fields in (
        ("ix_memory_records_memory_type", ["memory_type"]),
        ("ix_memory_records_task_id", ["task_id"]),
        ("ix_memory_records_session_id", ["session_id"]),
        ("ix_memory_records_status", ["status"]),
        ("ix_memory_records_superseded_by", ["superseded_by"]),
    ):
        if name not in indexes:
            op.create_index(name, "memory_records", fields)


def downgrade() -> None:
    bind = op.get_bind()
    existing_tables = set(inspect(bind).get_table_names())
    for table in (
        MemoryEpisode.__table__,
        PersistentTaskState.__table__,
        MemoryEmbedding.__table__,
    ):
        if table.name in existing_tables:
            table.drop(bind=bind)
    columns = {item["name"] for item in inspect(bind).get_columns("memory_records")}
    for column in reversed(_MEMORY_COLUMNS):
        if column.name in columns:
            op.drop_column("memory_records", column.name)
