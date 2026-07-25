"""Database lifecycle and unit-of-work helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from vasuki.config.models import Settings
from vasuki.persistence.models import Base, Project
from vasuki.utils.ids import new_id


def normalized_database_url(settings: Settings, root: Path) -> str:
    url = settings.database.url
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
        with self.session() as session:
            project = session.scalar(select(Project).where(Project.root_path == str(self.root)))
            if project is None:
                session.add(
                    Project(id=new_id("project"), name=self.root.name, root_path=str(self.root))
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
