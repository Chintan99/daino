"""Durable project memory and architecture-decision access."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select

from daino.persistence import Database
from daino.persistence.models import ArchitectureDecision, MemoryRecord
from daino.utils.ids import new_id
from daino.utils.time import utcnow

MemoryCategory = Literal["authoritative", "derived", "execution", "playbook"]


class MemoryStore:
    """Persists knowledge independently of model conversation history."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def remember(
        self,
        *,
        category: MemoryCategory,
        source: str,
        scope: str,
        content: dict[str, Any],
        confidence: float = 0.5,
        related_files: list[str] | None = None,
        approval_status: str = "unreviewed",
        last_validated_at: datetime | None = None,
    ) -> str:
        record_id = new_id("memory")
        project_id = self.database.project().id
        with self.database.session() as session:
            session.add(
                MemoryRecord(
                    id=record_id,
                    project_id=project_id,
                    category=category,
                    source=source,
                    scope=scope,
                    content=content,
                    last_validated_at=last_validated_at,
                    confidence=max(0, min(1, confidence)),
                    related_files=related_files or [],
                    human_approval_status=approval_status,
                )
            )
        return record_id

    def query(
        self, *, category: MemoryCategory | None = None, scope: str | None = None
    ) -> list[MemoryRecord]:
        project_id = self.database.project().id
        with self.database.session() as session:
            statement = select(MemoryRecord).where(MemoryRecord.project_id == project_id)
            if category:
                statement = statement.where(MemoryRecord.category == category)
            if scope:
                statement = statement.where(MemoryRecord.scope == scope)
            records = list(session.scalars(statement.order_by(MemoryRecord.created_at)))
            for record in records:
                session.expunge(record)
            return records

    def validate(self, record_id: str, *, confidence: float, approved: bool) -> None:
        with self.database.session() as session:
            record = session.get(MemoryRecord, record_id)
            if record is None:
                raise ValueError(f"Unknown memory record {record_id}")
            record.last_validated_at = utcnow()
            record.confidence = max(0, min(1, confidence))
            record.human_approval_status = "approved" if approved else "rejected"

    def add_decision(
        self,
        *,
        title: str,
        decision: str,
        implementation_rule: str,
        related_files: list[str] | None = None,
    ) -> str:
        decision_id = new_id("adr")
        project_id = self.database.project().id
        with self.database.session() as session:
            session.add(
                ArchitectureDecision(
                    id=decision_id,
                    project_id=project_id,
                    title=title,
                    decision=decision,
                    implementation_rule=implementation_rule,
                    related_files=related_files or [],
                )
            )
        return decision_id

    def relevant_decisions(self, files: list[str]) -> list[str]:
        project_id = self.database.project().id
        with self.database.session() as session:
            decisions = session.scalars(
                select(ArchitectureDecision).where(
                    ArchitectureDecision.project_id == project_id,
                    ArchitectureDecision.status == "accepted",
                )
            ).all()
            result = []
            for item in decisions:
                if not item.related_files or set(files) & set(item.related_files):
                    result.append(
                        f"{item.id} — {item.title}: {item.decision}. "
                        f"Implementation rule: {item.implementation_rule}"
                    )
            return result
