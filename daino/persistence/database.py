"""Database lifecycle and unit-of-work helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from daino.config import paths
from daino.config.models import Settings
from daino.persistence.models import Base, Project
from daino.repository.identity import identify_repository


def normalized_database_url(settings: Settings, root: Path) -> str:
    url = settings.database.url
    # The unconfigured default resolves through the path resolver so an existing
    # legacy ``.vasuki/vasuki.db`` keeps being used in place (read-legacy/write-new).
    if url in {paths.DEFAULT_DATABASE_URL, paths.LEGACY_DATABASE_URL}:
        return f"sqlite:///{paths.resolved_database_file(root)}"
    prefix = "sqlite:///"
    if url.startswith(prefix) and not url.startswith("sqlite:////"):
        relative = url[len(prefix) :]
        return f"sqlite:///{(root / relative).resolve()}"
    return url


class Database:
    """Owns the SQLAlchemy engine and short-lived sessions."""

    def __init__(self, settings: Settings, root: Path) -> None:
        self.root = root.resolve()
        url = normalized_database_url(settings, self.root)
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        poolclass = NullPool if url.startswith("sqlite") else None
        self.engine: Engine = create_engine(url, connect_args=connect_args, poolclass=poolclass)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self) -> None:
        if self.engine.url.get_backend_name() == "sqlite" and self.engine.url.database:
            Path(self.engine.url.database).parent.mkdir(parents=True, exist_ok=True)
        Base.metadata.create_all(self.engine)
        self._upgrade_additive_schema()
        with self.session() as session:
            project = session.scalar(select(Project).where(Project.root_path == str(self.root)))
            if project is None:
                identity = identify_repository(self.root)
                session.add(
                    Project(
                        id=identity.project_id,
                        name=self.root.name,
                        root_path=str(self.root),
                    )
                )

    def _upgrade_additive_schema(self) -> None:
        """Repair databases created before the memory envelope was expanded.

        Vasuki historically used ``create_all`` at application startup, while
        Alembic is available to operators and packaging. ``create_all`` cannot
        add columns to an existing SQLite table, so this tiny idempotent bridge
        keeps restart recovery working immediately after an upgrade; the 0005
        migration remains the canonical versioned schema change.
        """
        if self.engine.url.get_backend_name() != "sqlite":
            return
        inspector = inspect(self.engine)
        if "memory_records" not in inspector.get_table_names():
            return
        existing = {item["name"] for item in inspector.get_columns("memory_records")}
        definitions = {
            "memory_type": "VARCHAR(32) NOT NULL DEFAULT 'semantic'",
            "task_id": "VARCHAR(64)",
            "session_id": "VARCHAR(64)",
            "summary": "TEXT NOT NULL DEFAULT ''",
            "importance": "FLOAT NOT NULL DEFAULT 0.5",
            "source_type": "VARCHAR(32) NOT NULL DEFAULT 'agent'",
            "tags": "JSON NOT NULL DEFAULT '[]'",
            "status": "VARCHAR(32) NOT NULL DEFAULT 'active'",
            "last_accessed": "DATETIME",
            "last_verified": "DATETIME",
            "access_count": "INTEGER NOT NULL DEFAULT 0",
            "project_revision": "VARCHAR(64)",
            "source_digest": "VARCHAR(128)",
            "superseded_by": "VARCHAR(64)",
            "rationale": "TEXT NOT NULL DEFAULT ''",
        }
        missing = [(name, sql) for name, sql in definitions.items() if name not in existing]
        task_tables = set(inspect(self.engine).get_table_names())
        task_columns = (
            {item["name"] for item in inspect(self.engine).get_columns("persistent_task_states")}
            if "persistent_task_states" in task_tables
            else set()
        )
        missing_questions = bool(task_columns) and "unresolved_questions" not in task_columns
        if not missing and not missing_questions:
            return
        with self.engine.begin() as connection:
            for name, sql in missing:
                connection.execute(text(f"ALTER TABLE memory_records ADD COLUMN {name} {sql}"))
            if missing_questions:
                connection.execute(
                    text(
                        "ALTER TABLE persistent_task_states ADD COLUMN "
                        "unresolved_questions JSON NOT NULL DEFAULT '[]'"
                    )
                )

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def project(self) -> Project:
        with self.session() as session:
            project = session.scalar(select(Project).where(Project.root_path == str(self.root)))
            if project is None:
                raise RuntimeError("Database has not been initialized")
            session.expunge(project)
            return project
