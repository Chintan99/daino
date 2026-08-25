"""Production memory service: lifecycle, retrieval, tasks, episodes, and compaction."""

from __future__ import annotations

import builtins
import hashlib
import json
import math
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import Engine, Table, create_engine, delete, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from vasuki.config.models import MemoryConfig, Settings
from vasuki.memory.embeddings import (
    DisabledEmbeddingProvider,
    EmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    cosine_similarity,
)
from vasuki.memory.instructions import global_memory_dir
from vasuki.memory.types import (
    CompactedContext,
    DecisionStatus,
    MemoryMatch,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    PersistentTaskStatus,
    WorkingMemory,
)
from vasuki.observability import AuditLog
from vasuki.persistence import Database
from vasuki.persistence.models import (
    MemoryEmbedding,
    MemoryEpisode,
    MemoryRecord,
    PersistentTaskState,
    Project,
)
from vasuki.repository import file_digest, identify_repository
from vasuki.security.secrets import redact, resolve_secret
from vasuki.utils.ids import new_id
from vasuki.utils.time import utcnow

GLOBAL_PROJECT_ID = "global-user"
_WORD = re.compile(r"[a-z0-9_./:-]{2,}", re.I)
_ERROR_HINTS = re.compile(
    r"\b(error|exception|failed|failure|traceback|cannot|could not|refused|timeout|exit code)\b",
    re.I,
)
_ACTIVE_TASK_STATUSES = {
    PersistentTaskStatus.PENDING.value,
    PersistentTaskStatus.PLANNING.value,
    PersistentTaskStatus.IN_PROGRESS.value,
    PersistentTaskStatus.BLOCKED.value,
    PersistentTaskStatus.FAILED.value,
}


def _content_text(record: MemoryRecord) -> str:
    if isinstance(record.content, str):
        return record.content
    if isinstance(record.content, dict):
        text = record.content.get("text")
        if isinstance(text, str):
            return text
        return json.dumps(record.content, sort_keys=True, ensure_ascii=False)
    return str(record.content)


def _metadata(record: MemoryRecord) -> dict[str, Any]:
    if not isinstance(record.content, dict):
        return {}
    metadata = record.content.get("metadata", {})
    return dict(metadata) if isinstance(metadata, dict) else {}


def _unique(values: Iterable[str], *, limit: int | None = None) -> list[str]:
    result = list(dict.fromkeys(item for item in values if item))
    return result[:limit] if limit is not None else result


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _tokens(text: str) -> set[str]:
    stop = {
        "the",
        "and",
        "that",
        "this",
        "with",
        "from",
        "for",
        "into",
        "use",
        "uses",
        "using",
    }
    return {token.casefold() for token in _WORD.findall(text) if token.casefold() not in stop}


def error_fingerprint(error: str) -> str:
    """Normalize volatile paths, addresses, ids, and numbers from an error."""
    value = redact(error).casefold()
    value = re.sub(r"(?:[a-z]:)?[/\\][\w./\\-]+", " <path> ", value)
    value = re.sub(r"\b0x[0-9a-f]+\b", "<hex>", value)
    value = re.sub(r"\b\d+\b", "<n>", value)
    value = re.sub(r"\s+", " ", value).strip()
    return hashlib.sha256(value.encode()).hexdigest()[:24]


class _GlobalMemoryDatabase:
    """Small user-wide SQLite store; application tables remain project-local."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        self.path = path
        self.engine: Engine = create_engine(
            f"sqlite:///{path}",
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
        )
        # SQLAlchemy's declarative typing exposes ``__table__`` as FromClause,
        # although mapped classes provide a concrete Table with ``create``.
        cast(Table, Project.__table__).create(self.engine, checkfirst=True)
        cast(Table, MemoryRecord.__table__).create(self.engine, checkfirst=True)
        cast(Table, MemoryEmbedding.__table__).create(self.engine, checkfirst=True)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        with self.session() as session:
            if session.get(Project, GLOBAL_PROJECT_ID) is None:
                session.add(
                    Project(
                        id=GLOBAL_PROJECT_ID,
                        name="Vasuki user memory",
                        root_path="global://user",
                    )
                )
        try:
            path.chmod(0o600)
        except OSError:
            pass

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Return a session usable as a context manager with automatic commit."""
        session = self.sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def close(self) -> None:
        self.engine.dispose()


class MemoryManager:
    """The only service agents and interfaces use to access durable memory."""

    def __init__(
        self,
        database: Database,
        root: Path | None = None,
        settings: Settings | MemoryConfig | None = None,
        *,
        embedding_provider: EmbeddingProvider | None = None,
        global_path: Path | None = None,
    ) -> None:
        self.database = database
        self.root = (root or database.root).resolve()
        if isinstance(settings, Settings):
            self.config = settings.memory
        elif isinstance(settings, MemoryConfig):
            self.config = settings
        else:
            self.config = MemoryConfig()
        self.project = identify_repository(self.root)
        self.log = AuditLog(self.root)
        if embedding_provider is not None:
            self.embedding = embedding_provider
        elif (
            self.config.embedding_provider != "disabled"
            and self.config.embedding_base_url
            and self.config.embedding_model
        ):
            try:
                api_key = (
                    resolve_secret(self.config.embedding_api_key)
                    if self.config.embedding_api_key
                    else ""
                )
            except Exception as exc:  # noqa: BLE001 - lexical retrieval remains available
                self.log.emit("memory_embedding_unavailable", error=str(exc))
                self.embedding = DisabledEmbeddingProvider()
            else:
                self.embedding = OpenAICompatibleEmbeddingProvider(
                    self.config.embedding_base_url,
                    self.config.embedding_model,
                    api_key=api_key,
                    name=self.config.embedding_provider,
                )
        else:
            self.embedding = DisabledEmbeddingProvider()
        self._scratch: dict[str, MemoryMatch] = {}
        path = global_path or global_memory_dir() / "memory.db"
        self._global = _GlobalMemoryDatabase(path)

    def close(self) -> None:
        self._global.close()

    def _embed(self, text: str) -> builtins.list[float] | None:
        """Treat an unavailable embedding service as a lexical-only fallback."""
        try:
            return self.embedding.embed(text)
        except Exception as exc:  # noqa: BLE001 - optional enhancement must not break memory
            self.log.emit(
                "memory_embedding_unavailable",
                provider=getattr(self.embedding, "name", "unknown"),
                error=str(exc),
            )
            return None

    @staticmethod
    def _sanitize(content: str, source: str = "") -> str:
        if Path(source).name.casefold().startswith(".env"):
            return "[Sensitive .env source omitted from memory]"
        safe = redact(content)
        # Authorization values and common cloud access-key forms are worth a
        # second, memory-specific pass even when the surrounding text is prose.
        safe = re.sub(
            r"(?im)^(authorization\s*:\s*)(?:bearer\s+)?\S+",
            r"\1[REDACTED]",
            safe,
        )
        safe = re.sub(r"\bAKIA[0-9A-Z]{16}\b", "[REDACTED]", safe)
        return safe[:32_000]

    @classmethod
    def _sanitize_value(cls, value: Any) -> Any:
        if isinstance(value, str):
            return cls._sanitize(value)
        if isinstance(value, dict):
            return {str(key): cls._sanitize_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._sanitize_value(item) for item in value]
        return value

    def _record_match(self, record: MemoryRecord, *, score: float = 0.0) -> MemoryMatch:
        raw_type = getattr(record, "memory_type", "semantic") or "semantic"
        raw_scope = record.scope if record.scope in set(MemoryScope) else "project"
        raw_status = getattr(record, "status", "active") or "active"
        return MemoryMatch(
            id=record.id,
            type=MemoryType(raw_type),
            scope=MemoryScope(raw_scope),
            project_id=record.project_id,
            task_id=getattr(record, "task_id", None),
            session_id=getattr(record, "session_id", None),
            content=_content_text(record),
            summary=getattr(record, "summary", "") or "",
            importance=float(getattr(record, "importance", 0.5) or 0.5),
            confidence=float(record.confidence),
            source=record.source,
            source_type=getattr(record, "source_type", "agent") or "agent",
            tags=list(getattr(record, "tags", []) or []),
            status=MemoryStatus(raw_status),
            score=score,
            created_at=_aware(record.created_at),
            updated_at=_aware(record.updated_at),
            last_accessed=_aware(getattr(record, "last_accessed", None)),
            last_verified=_aware(
                getattr(record, "last_verified", None) or record.last_validated_at
            ),
            metadata=_metadata(record),
        )

    def remember(
        self,
        content: str,
        *,
        memory_type: MemoryType | str = MemoryType.SEMANTIC,
        scope: MemoryScope | str = MemoryScope.PROJECT,
        summary: str = "",
        importance: float = 0.5,
        confidence: float = 0.5,
        source: str = "",
        source_type: str = "agent",
        tags: list[str] | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        status: MemoryStatus | str = MemoryStatus.ACTIVE,
        rationale: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Persist one small atomic fact after sanitizing it."""
        typed_scope = MemoryScope(scope)
        typed_type = MemoryType(memory_type)
        typed_status = MemoryStatus(status)
        safe = self._sanitize(content.strip(), source)
        safe_metadata = self._sanitize_value(metadata or {})
        if not safe:
            raise ValueError("Cannot remember empty content")
        record_id = new_id("memory")
        if typed_scope == MemoryScope.SCRATCH:
            self._scratch[record_id] = MemoryMatch(
                id=record_id,
                type=typed_type,
                scope=typed_scope,
                project_id=self.database.project().id,
                content=safe,
                summary=self._sanitize(summary),
                importance=max(0.0, min(1.0, importance)),
                confidence=max(0.0, min(1.0, confidence)),
                source=source,
                source_type=source_type,
                tags=[self._sanitize(item) for item in (tags or [])],
                status=typed_status,
                task_id=task_id,
                session_id=session_id,
                metadata=safe_metadata,
            )
            return record_id

        digest = file_digest(self.root, source) if typed_scope != MemoryScope.GLOBAL else None
        record = MemoryRecord(
            id=record_id,
            project_id=(
                GLOBAL_PROJECT_ID
                if typed_scope == MemoryScope.GLOBAL
                else self.database.project().id
            ),
            category="derived" if source_type != "user" else "authoritative",
            memory_type=typed_type.value,
            source=source or source_type,
            source_type=source_type,
            scope=typed_scope.value,
            task_id=task_id,
            session_id=session_id,
            content={"text": safe, "metadata": safe_metadata},
            summary=self._sanitize(summary),
            importance=max(0.0, min(1.0, importance)),
            confidence=max(0.0, min(1.0, confidence)),
            tags=_unique([self._sanitize(item) for item in (tags or [])], limit=32),
            status=typed_status.value,
            last_validated_at=utcnow() if source_type == "user" else None,
            last_verified=utcnow() if source_type == "user" else None,
            project_revision=self.project.revision or None,
            source_digest=digest,
            rationale=self._sanitize(rationale),
            related_files=[source] if digest else [],
            human_approval_status="approved" if source_type == "user" else "unreviewed",
        )
        session_factory = (
            self._global.session if typed_scope == MemoryScope.GLOBAL else self.database.session
        )
        with session_factory() as session:
            session.add(record)
            vector = self._embed(f"{summary}\n{safe}")
            if vector:
                session.add(
                    MemoryEmbedding(
                        memory_id=record_id,
                        provider=self.embedding.name,
                        model=self.embedding.model,
                        dimensions=len(vector),
                        vector=vector,
                    )
                )
        self.log.emit(
            "memory_created",
            memory_id=record_id,
            memory_type=typed_type.value,
            scope=typed_scope.value,
            source=source,
            rationale=rationale,
        )
        return record_id

    def remember_decision(
        self,
        decision: str,
        *,
        reason: str = "",
        alternatives: list[str] | None = None,
        source: str = "user",
        scope: MemoryScope | str = MemoryScope.PROJECT,
        task_id: str | None = None,
        status: DecisionStatus | str = DecisionStatus.ACTIVE,
    ) -> str:
        explicit_user = source.casefold().startswith("user")
        decision_status = DecisionStatus(status)
        return self.remember(
            decision,
            memory_type=MemoryType.DECISION,
            scope=scope,
            summary=decision,
            importance=0.9,
            confidence=1.0 if explicit_user else 0.75,
            source=source,
            source_type="user" if explicit_user else "agent",
            task_id=task_id,
            status=(
                MemoryStatus.ACTIVE
                if decision_status == DecisionStatus.ACTIVE
                else MemoryStatus.SUPERSEDED
            ),
            rationale=reason,
            metadata={
                "decision": decision,
                "reason": reason,
                "alternatives": alternatives or [],
                "decision_status": decision_status.value,
            },
        )

    def remember_failure(
        self,
        error: str,
        *,
        cause: str,
        solution: str,
        context: str = "",
        failed_attempts: list[str] | None = None,
        scope: MemoryScope | str = MemoryScope.PROJECT,
        confidence: float = 0.8,
        source: str = "verification",
        task_id: str | None = None,
    ) -> str:
        if not self.config.failure_memory_enabled:
            raise ValueError("Failure memory is disabled")
        normalized = re.sub(r"\s+", " ", redact(error)).strip()
        fingerprint = error_fingerprint(error)
        existing = self.search(
            error,
            memory_types=[MemoryType.FAILURE],
            limit=1,
        )
        if existing and existing[0].metadata.get("error_fingerprint") == fingerprint:
            return existing[0].id
        content = f"Error: {normalized}\nCause: {cause}\nSuccessful fix: {solution}"
        if context:
            content += f"\nContext: {context}"
        return self.remember(
            content,
            memory_type=MemoryType.FAILURE,
            scope=scope,
            summary=f"{normalized[:180]} — {solution[:180]}",
            importance=0.85,
            confidence=confidence,
            source=source,
            source_type="tool",
            task_id=task_id,
            tags=["failure", "solution", fingerprint],
            metadata={
                "error": normalized,
                "normalized_error": normalized.casefold(),
                "error_fingerprint": fingerprint,
                "context": context,
                "root_cause": cause,
                "successful_fix": solution,
                "failed_attempts": failed_attempts or [],
            },
        )

    def refresh_staleness(self, repository: Path | None = None) -> None:
        """Invalidate source-derived facts against the current source tree."""
        source_root = (repository or self.root).resolve()
        with self.database.session() as session:
            records = session.scalars(
                select(MemoryRecord).where(
                    MemoryRecord.project_id == self.database.project().id,
                    MemoryRecord.status == MemoryStatus.ACTIVE.value,
                    MemoryRecord.source_digest.is_not(None),
                )
            ).all()
            for record in records:
                current = file_digest(source_root, record.source)
                if current != record.source_digest:
                    record.status = MemoryStatus.STALE.value
                    self.log.emit(
                        "memory_marked_stale",
                        memory_id=record.id,
                        source=record.source,
                        reason="source file changed or disappeared",
                    )

    def _candidate_records(
        self,
        *,
        scope: MemoryScope | None,
        types: set[str] | None,
        include_stale: bool,
        task_id: str | None,
        session_id: str | None,
    ) -> list[tuple[MemoryRecord, str]]:
        candidates: list[tuple[MemoryRecord, str]] = []
        if scope in {None, MemoryScope.SCRATCH}:
            # Scratch records are already typed and ranked separately in search.
            pass
        statuses = [MemoryStatus.ACTIVE.value]
        if include_stale:
            statuses.append(MemoryStatus.STALE.value)

        def collect(session: Session, *, global_store: bool) -> None:
            statement = select(MemoryRecord).where(MemoryRecord.status.in_(statuses))
            if global_store:
                statement = statement.where(MemoryRecord.scope == MemoryScope.GLOBAL.value)
            else:
                statement = statement.where(
                    MemoryRecord.project_id == self.database.project().id,
                    MemoryRecord.scope != MemoryScope.GLOBAL.value,
                )
            if scope is not None:
                statement = statement.where(MemoryRecord.scope == scope.value)
            if types:
                statement = statement.where(MemoryRecord.memory_type.in_(types))
            if task_id:
                statement = statement.where(
                    (MemoryRecord.task_id == task_id) | (MemoryRecord.task_id.is_(None))
                )
            if session_id:
                statement = statement.where(
                    (MemoryRecord.session_id == session_id) | (MemoryRecord.session_id.is_(None))
                )
            for record in session.scalars(statement.limit(500)):
                session.expunge(record)
                candidates.append((record, "global" if global_store else "project"))

        if scope != MemoryScope.GLOBAL:
            with self.database.session() as session:
                collect(session, global_store=False)
        if scope in {None, MemoryScope.GLOBAL} and self.config.user_memory_enabled:
            with self._global.session() as session:
                collect(session, global_store=True)
        return candidates

    def search(
        self,
        query: str,
        *,
        memory_types: Iterable[MemoryType | str] | None = None,
        scope: MemoryScope | str | None = None,
        task_id: str | None = None,
        session_id: str | None = None,
        limit: int | None = None,
        token_budget: int | None = None,
        include_stale: bool = False,
        debug: bool = False,
    ) -> list[MemoryMatch]:
        """Hybrid lexical/semantic/metadata retrieval with bounded output."""
        if not self.config.enabled:
            return []
        typed_scope = MemoryScope(scope) if scope is not None else None
        types = {MemoryType(item).value for item in memory_types} if memory_types else None
        self.refresh_staleness()
        candidates = self._candidate_records(
            scope=typed_scope,
            types=types,
            include_stale=include_stale,
            task_id=task_id,
            session_id=session_id,
        )
        query_terms = _tokens(query)
        query_vector = self._embed(query)
        now = datetime.now(UTC)
        ranked: list[tuple[float, MemoryRecord | None, MemoryMatch, str]] = []

        scratch = self._scratch.values() if typed_scope in {None, MemoryScope.SCRATCH} else []
        for item in scratch:
            if types and item.type.value not in types:
                continue
            if task_id and item.task_id not in {None, task_id}:
                continue
            terms = _tokens(f"{item.summary} {item.content} {' '.join(item.tags)}")
            lexical = len(query_terms & terms) / max(1, len(query_terms))
            item.score = lexical + item.importance * 0.2 + item.confidence * 0.2
            item.why = ["scratch scope", f"lexical={lexical:.3f}"] if debug else []
            ranked.append((item.score, None, item, "scratch"))

        embedding_map: dict[tuple[str, str], list[float]] = {}
        if query_vector:
            project_ids = [record.id for record, store in candidates if store == "project"]
            global_ids = [record.id for record, store in candidates if store == "global"]
            with self.database.session() as session:
                for row in session.scalars(
                    select(MemoryEmbedding).where(
                        MemoryEmbedding.memory_id.in_(project_ids or [""])
                    )
                ):
                    embedding_map[("project", row.memory_id)] = list(row.vector)
            with self._global.session() as session:
                for row in session.scalars(
                    select(MemoryEmbedding).where(MemoryEmbedding.memory_id.in_(global_ids or [""]))
                ):
                    embedding_map[("global", row.memory_id)] = list(row.vector)

        for record, store in candidates:
            item = self._record_match(record)
            haystack = f"{item.summary} {item.content} {' '.join(item.tags)} {item.source}"
            terms = _tokens(haystack)
            overlap = len(query_terms & terms)
            lexical = overlap / max(1, len(query_terms))
            phrase = 0.25 if query.casefold() in haystack.casefold() and query.strip() else 0.0
            project_boost = 0.35 if store == "project" else 0.0
            task_boost = 0.30 if task_id and record.task_id == task_id else 0.0
            type_boost = 0.35 if item.type == MemoryType.DECISION else 0.0
            if item.type == MemoryType.FAILURE and _ERROR_HINTS.search(query):
                type_boost += 0.55
                fingerprint = item.metadata.get("error_fingerprint")
                if fingerprint and fingerprint == error_fingerprint(query):
                    type_boost += 0.8
            created = _aware(record.created_at) or now
            age_days = max(0.0, (now - created).total_seconds() / 86_400)
            recency = (
                math.pow(0.5, age_days / self.config.decay_half_life_days)
                if self.config.decay_enabled
                else 1.0
            )
            source_reliability = {
                "user": 0.25,
                "repository": 0.22,
                "source_code": 0.22,
                "verification": 0.18,
                "tool": 0.12,
                "agent": 0.05,
            }.get(item.source_type, 0.05)
            semantic = cosine_similarity(
                query_vector or [], embedding_map.get((store, record.id), [])
            )
            unrelated = (
                query_terms and overlap == 0 and phrase == 0 and semantic < 0.25 and task_boost == 0
            )
            if unrelated:
                # Metadata quality can order relevant records, but it must not
                # make an unrelated high-importance decision relevant by itself.
                project_boost = 0.0
                type_boost = min(type_boost, 0.1)
            stale_penalty = 1.5 if item.status == MemoryStatus.STALE else 0.0
            score = (
                lexical * 1.5
                + phrase
                + semantic * 0.9
                + project_boost
                + task_boost
                + type_boost
                + item.importance * 0.35
                + item.confidence * 0.35
                + recency * 0.15
                + min(record.access_count, 20) * 0.01
                + source_reliability
                - stale_penalty
                - (1.0 if unrelated else 0.0)
            )
            item.score = score
            if debug:
                item.why = [
                    f"lexical={lexical:.3f}",
                    f"semantic={semantic:.3f}",
                    f"scope={store}",
                    f"type={item.type.value}",
                    f"importance={item.importance:.2f}",
                    f"confidence={item.confidence:.2f}",
                    f"recency={recency:.3f}",
                    f"status={item.status.value}",
                ]
            ranked.append((score, record, item, store))

        ranked.sort(
            key=lambda value: (
                value[0],
                value[2].updated_at or datetime.min.replace(tzinfo=UTC),
            ),
            reverse=True,
        )
        selected: list[tuple[MemoryRecord | None, MemoryMatch, str]] = []
        budget = token_budget or self.config.max_context_tokens
        used = 0
        for score, selected_record, item, store in ranked:
            # Empty-overlap global memories should not leak into unrelated tasks.
            no_lexical_match = query_terms and not (_tokens(item.content) & query_terms)
            if score <= 0 or (no_lexical_match and score < 1.0):
                continue
            cost = max(1, (len(item.summary) + len(item.content)) // 4)
            if selected and used + cost > budget:
                continue
            selected.append((selected_record, item, store))
            used += cost
            if len(selected) >= (limit or self.config.max_retrieved_items):
                break

        accessed = utcnow()
        project_ids = [record.id for record, _, store in selected if record and store == "project"]
        global_ids = [record.id for record, _, store in selected if record and store == "global"]
        if project_ids:
            with self.database.session() as session:
                for record in session.scalars(
                    select(MemoryRecord).where(MemoryRecord.id.in_(project_ids))
                ):
                    record.last_accessed = accessed
                    record.access_count += 1
        if global_ids:
            with self._global.session() as session:
                for record in session.scalars(
                    select(MemoryRecord).where(MemoryRecord.id.in_(global_ids))
                ):
                    record.last_accessed = accessed
                    record.access_count += 1
        self.log.emit(
            "memory_retrieved",
            query=redact(query)[:500],
            selected=[
                {"id": item.id, "score": round(item.score, 4), "why": item.why}
                for _, item, _ in selected
            ],
            token_estimate=used,
        )
        return [item for _, item, _ in selected]

    def retrieve_for_task(
        self,
        query: str,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        errors: list[str] | None = None,
        limit: int | None = None,
        debug: bool = False,
    ) -> list[MemoryMatch]:
        combined = query
        if errors:
            combined += "\n" + "\n".join(errors[-3:])
        return self.search(
            combined,
            task_id=task_id,
            session_id=session_id,
            limit=limit,
            debug=debug,
        )

    def get(self, memory_id: str) -> MemoryMatch:
        if memory_id in self._scratch:
            return self._scratch[memory_id]
        for factory in (self.database.session, self._global.session):
            with factory() as session:
                record = session.get(MemoryRecord, memory_id)
                if record is not None:
                    session.expunge(record)
                    return self._record_match(record)
        raise ValueError(f"Unknown memory {memory_id}")

    def list(
        self,
        *,
        memory_type: MemoryType | str | None = None,
        scope: MemoryScope | str | None = None,
        include_stale: bool = True,
        limit: int = 100,
    ) -> list[MemoryMatch]:
        return self.search(
            "",
            memory_types=[memory_type] if memory_type else None,
            scope=scope,
            include_stale=include_stale,
            limit=limit,
            token_budget=1_000_000,
        )

    def update(self, memory_id: str, **fields: Any) -> None:
        allowed = {
            "summary",
            "importance",
            "confidence",
            "source",
            "source_type",
            "tags",
            "status",
            "rationale",
        }
        if memory_id in self._scratch:
            item = self._scratch[memory_id]
            updates = {key: value for key, value in fields.items() if key in item.model_fields}
            self._scratch[memory_id] = item.model_copy(update=updates)
            return
        for factory in (self.database.session, self._global.session):
            with factory() as session:
                record = session.get(MemoryRecord, memory_id)
                if record is None:
                    continue
                if "content" in fields:
                    record.content = {
                        "text": self._sanitize(str(fields["content"]), record.source),
                        "metadata": _metadata(record),
                    }
                if "metadata" in fields:
                    record.content = {
                        "text": _content_text(record),
                        "metadata": self._sanitize_value(fields["metadata"]),
                    }
                for name in allowed:
                    if name not in fields:
                        continue
                    value = fields[name]
                    if name in {"importance", "confidence"}:
                        value = max(0.0, min(1.0, float(value)))
                    elif name == "status":
                        value = MemoryStatus(value).value
                    elif name in {"summary", "rationale"}:
                        value = self._sanitize(str(value))
                    elif name == "tags":
                        value = self._sanitize_value(value)
                    setattr(record, name, value)
                return
        raise ValueError(f"Unknown memory {memory_id}")

    def forget(self, memory_id: str) -> None:
        if self._scratch.pop(memory_id, None) is not None:
            return
        for factory in (self.database.session, self._global.session):
            with factory() as session:
                record = session.get(MemoryRecord, memory_id)
                if record is None:
                    continue
                session.execute(
                    delete(MemoryEmbedding).where(MemoryEmbedding.memory_id == memory_id)
                )
                session.delete(record)
                self.log.emit("memory_forgotten", memory_id=memory_id)
                return
        raise ValueError(f"Unknown memory {memory_id}")

    def clear(
        self,
        *,
        scope: MemoryScope | str,
        session_id: str | None = None,
    ) -> int:
        """Forget a bounded scope through the same inspectable service API."""
        typed_scope = MemoryScope(scope)
        if typed_scope == MemoryScope.GLOBAL:
            items = self.list(scope=typed_scope, limit=10_000)
        else:
            items = self.list(scope=typed_scope, limit=10_000)
            if session_id is not None:
                items = [item for item in items if item.session_id == session_id]
        for item in items:
            self.forget(item.id)
        return len(items)

    def mark_stale(self, memory_id: str, *, reason: str = "") -> None:
        self.update(memory_id, status=MemoryStatus.STALE, rationale=reason)
        self.log.emit("memory_marked_stale", memory_id=memory_id, reason=reason)

    def supersede(
        self,
        memory_id: str,
        replacement: str | None = None,
        **kwargs: Any,
    ) -> str | None:
        new_id: str | None = None
        if replacement:
            old = self.get(memory_id)
            new_id = self.remember(
                replacement,
                memory_type=old.type,
                scope=old.scope,
                summary=kwargs.pop("summary", ""),
                importance=kwargs.pop("importance", old.importance),
                confidence=kwargs.pop("confidence", old.confidence),
                source=kwargs.pop("source", old.source),
                source_type=kwargs.pop("source_type", old.source_type),
                tags=kwargs.pop("tags", old.tags),
                metadata=kwargs.pop("metadata", old.metadata),
                **kwargs,
            )
        old = self.get(memory_id)
        updates: dict[str, Any] = {"status": MemoryStatus.SUPERSEDED}
        if old.type == MemoryType.DECISION:
            updates["metadata"] = {
                **old.metadata,
                "decision_status": DecisionStatus.SUPERSEDED.value,
            }
        self.update(memory_id, **updates)
        if new_id:
            for factory in (self.database.session, self._global.session):
                with factory() as session:
                    record = session.get(MemoryRecord, memory_id)
                    if record is not None:
                        record.superseded_by = new_id
                        break
        self.log.emit("memory_superseded", memory_id=memory_id, replacement_id=new_id)
        return new_id

    def reverse_decision(self, memory_id: str, *, reason: str = "") -> None:
        item = self.get(memory_id)
        if item.type != MemoryType.DECISION:
            raise ValueError(f"Memory {memory_id} is not a decision")
        self.update(
            memory_id,
            status=MemoryStatus.SUPERSEDED,
            rationale=reason,
            metadata={
                **item.metadata,
                "decision_status": DecisionStatus.REVERSED.value,
                "reversal_reason": reason,
            },
        )
        self.log.emit("memory_reversed", memory_id=memory_id, reason=reason)

    def verify(self, memory_id: str, *, confidence: float | None = None) -> None:
        item = self.get(memory_id)
        digest = file_digest(self.root, item.source) if item.scope != MemoryScope.GLOBAL else None
        for factory in (self.database.session, self._global.session):
            with factory() as session:
                record = session.get(MemoryRecord, memory_id)
                if record is None:
                    continue
                record.last_verified = utcnow()
                record.last_validated_at = utcnow()
                record.status = MemoryStatus.ACTIVE.value
                record.source_digest = digest or record.source_digest
                if confidence is not None:
                    record.confidence = max(0.0, min(1.0, confidence))
                self.log.emit("memory_verified", memory_id=memory_id, source=item.source)
                return

    def _move_scope(self, memory_id: str, target: MemoryScope) -> str:
        item = self.get(memory_id)
        if item.scope == target:
            return memory_id
        if item.scope == MemoryScope.SCRATCH:
            self._scratch.pop(memory_id, None)
        elif item.scope != MemoryScope.GLOBAL and target != MemoryScope.GLOBAL:
            with self.database.session() as session:
                record = session.get(MemoryRecord, memory_id)
                if record is None:
                    raise ValueError(f"Unknown memory {memory_id}")
                record.scope = target.value
            promoted = list(MemoryScope).index(target) > list(MemoryScope).index(item.scope)
            self.log.emit(
                "memory_promoted" if promoted else "memory_demoted",
                memory_id=memory_id,
                from_scope=item.scope.value,
                to_scope=target.value,
            )
            return memory_id

        target_factory = (
            self._global.session if target == MemoryScope.GLOBAL else self.database.session
        )
        with target_factory() as session:
            session.add(
                MemoryRecord(
                    id=memory_id,
                    project_id=(
                        GLOBAL_PROJECT_ID
                        if target == MemoryScope.GLOBAL
                        else self.database.project().id
                    ),
                    category="authoritative" if item.source_type == "user" else "derived",
                    memory_type=item.type.value,
                    source=item.source,
                    source_type=item.source_type,
                    scope=target.value,
                    task_id=item.task_id,
                    session_id=item.session_id,
                    content={"text": item.content, "metadata": item.metadata},
                    summary=item.summary,
                    importance=item.importance,
                    confidence=item.confidence,
                    tags=item.tags,
                    status=item.status.value,
                    last_accessed=item.last_accessed,
                    last_verified=item.last_verified,
                    source_digest=(
                        None
                        if target == MemoryScope.GLOBAL
                        else file_digest(self.root, item.source)
                    ),
                    rationale=f"Moved from {item.scope.value} scope",
                )
            )
            vector = self._embed(f"{item.summary}\n{item.content}")
            if vector:
                session.add(
                    MemoryEmbedding(
                        memory_id=memory_id,
                        provider=self.embedding.name,
                        model=self.embedding.model,
                        dimensions=len(vector),
                        vector=vector,
                    )
                )
        if item.scope == MemoryScope.GLOBAL:
            source_factory = self._global.session
        elif item.scope != MemoryScope.SCRATCH:
            source_factory = self.database.session
        else:
            source_factory = None
        if source_factory is not None:
            with source_factory() as session:
                session.execute(
                    delete(MemoryEmbedding).where(MemoryEmbedding.memory_id == memory_id)
                )
                record = session.get(MemoryRecord, memory_id)
                if record is not None:
                    session.delete(record)
        promoted = list(MemoryScope).index(target) > list(MemoryScope).index(item.scope)
        self.log.emit(
            "memory_promoted" if promoted else "memory_demoted",
            memory_id=memory_id,
            from_scope=item.scope.value,
            to_scope=target.value,
        )
        return memory_id

    def promote(self, memory_id: str, target_scope: MemoryScope | str) -> str:
        return self._move_scope(memory_id, MemoryScope(target_scope))

    def demote(self, memory_id: str, target_scope: MemoryScope | str) -> str:
        return self._move_scope(memory_id, MemoryScope(target_scope))

    # ---- Persistent working/task memory ---------------------------------

    def start_task(
        self,
        original_request: str,
        *,
        interpreted_goal: str = "",
        plan: builtins.list[dict[str, Any]] | None = None,
        mission_id: str | None = None,
        session_id: str | None = None,
        status: PersistentTaskStatus | str = PersistentTaskStatus.PENDING,
        task_id: str | None = None,
        repository: Path | None = None,
        branch: str | None = None,
    ) -> WorkingMemory:
        identifier = task_id or new_id("persistent-task")
        target_root = (repository or self.root).resolve()
        state = PersistentTaskState(
            id=identifier,
            project_id=self.database.project().id,
            mission_id=mission_id,
            session_id=session_id,
            original_request=self._sanitize(original_request),
            interpreted_goal=self._sanitize(interpreted_goal),
            plan=self._sanitize_value(plan or []),
            pending_steps=self._sanitize_value(
                [
                    str(item.get("content") or item.get("title") or "")
                    for item in (plan or [])
                    if str(item.get("status", "pending")) != "completed"
                ]
            ),
            status=PersistentTaskStatus(status).value,
            repository=str(target_root),
            branch=branch if branch is not None else self.project.branch or None,
        )
        with self.database.session() as session:
            session.add(state)
        return self.load_task(identifier)

    @staticmethod
    def _working(row: PersistentTaskState) -> WorkingMemory:
        return WorkingMemory(
            task_id=row.id,
            project_id=row.project_id,
            mission_id=row.mission_id,
            session_id=row.session_id,
            original_request=row.original_request,
            interpreted_goal=row.interpreted_goal,
            plan=list(row.plan or []),
            completed_steps=list(row.completed_steps or []),
            current_step=row.current_step,
            pending_steps=list(row.pending_steps or []),
            status=PersistentTaskStatus(row.status),
            repository=row.repository,
            branch=row.branch,
            files_inspected=list(row.files_inspected or []),
            files_changed=list(row.files_changed or []),
            commands_executed=list(row.commands_executed or []),
            important_outputs=list(row.important_outputs or []),
            test_status=dict(row.test_status or {}),
            unresolved_questions=list(row.unresolved_questions or []),
            unresolved_problems=list(row.unresolved_problems or []),
            hypotheses=list(row.hypotheses or []),
            errors=list(row.errors or []),
            last_action=row.last_action,
            compacted_context=dict(row.compacted_context or {}),
            created_at=_aware(row.created_at),
            updated_at=_aware(row.updated_at),
        )

    def load_task(self, task_id: str) -> WorkingMemory:
        with self.database.session() as session:
            row = session.get(PersistentTaskState, task_id)
            if row is None:
                raise ValueError(f"Unknown persistent task {task_id}")
            return self._working(row)

    def latest_task_for_session(self, session_id: str) -> WorkingMemory | None:
        """Return the newest task for a session whatever its status.

        ``resumable_tasks`` hides finished work, but a chat session that already
        completed one turn is exactly where the next turn has to pick up from,
        so continuation needs the newest row rather than the newest active one.
        """
        with self.database.session() as session:
            row = session.scalar(
                select(PersistentTaskState)
                .where(
                    PersistentTaskState.project_id == self.database.project().id,
                    PersistentTaskState.session_id == session_id,
                )
                .order_by(PersistentTaskState.updated_at.desc())
            )
            return self._working(row) if row else None

    def task_for_mission(self, mission_id: str) -> WorkingMemory | None:
        with self.database.session() as session:
            row = session.scalar(
                select(PersistentTaskState)
                .where(PersistentTaskState.mission_id == mission_id)
                .order_by(PersistentTaskState.updated_at.desc())
            )
            return self._working(row) if row else None

    def update_task(self, task_id: str, **fields: Any) -> WorkingMemory:
        allowed = set(WorkingMemory.model_fields) - {
            "task_id",
            "project_id",
            "created_at",
            "updated_at",
        }
        with self.database.session() as session:
            row = session.get(PersistentTaskState, task_id)
            if row is None:
                raise ValueError(f"Unknown persistent task {task_id}")
            for name, value in fields.items():
                if name not in allowed:
                    continue
                if name == "status":
                    value = PersistentTaskStatus(value).value
                value = self._sanitize_value(value)
                setattr(row, name, value)
        return self.load_task(task_id)

    def record_action(
        self,
        task_id: str,
        *,
        action: str,
        paths: builtins.list[str] | None = None,
        command: str = "",
        success: bool = True,
        output: str = "",
        error: str = "",
    ) -> WorkingMemory:
        """Checkpoint meaningful state immediately after every tool action."""
        state = self.load_task(task_id)
        inspected = list(state.files_inspected)
        changed = list(state.files_changed)
        commands = list(state.commands_executed)
        errors = list(state.errors)
        outputs = list(state.important_outputs)
        if action in {"read_file", "search_text", "grep", "glob", "list_directory"}:
            inspected = _unique([*inspected, *(paths or [])], limit=500)
        if action in {"write", "replace", "multi_edit", "delete", "patch"}:
            changed = _unique([*changed, *(paths or [])], limit=500)
        if command:
            commands.append(
                {
                    "command": self._sanitize(command),
                    "success": success,
                    "output": self._sanitize(output)[:1_000],
                    "timestamp": utcnow().isoformat(),
                }
            )
            commands = commands[-200:]
        if output and (not success or action in {"run_command", "finish"}):
            outputs = _unique([*outputs, self._sanitize(output)[:2_000]], limit=50)
        if error:
            errors = _unique([*errors, self._sanitize(error)[:2_000]], limit=50)
        return self.update_task(
            task_id,
            files_inspected=inspected,
            files_changed=changed,
            commands_executed=commands,
            important_outputs=outputs,
            errors=errors,
            last_action=action,
            status=PersistentTaskStatus.IN_PROGRESS,
        )

    def resumable_tasks(self, *, limit: int = 20) -> builtins.list[WorkingMemory]:
        with self.database.session() as session:
            rows = session.scalars(
                select(PersistentTaskState)
                .where(
                    PersistentTaskState.project_id == self.database.project().id,
                    PersistentTaskState.status.in_(_ACTIVE_TASK_STATUSES),
                )
                .order_by(PersistentTaskState.updated_at.desc())
                .limit(limit)
            ).all()
            return [self._working(row) for row in rows]

    def compact(
        self,
        task_id: str,
        *,
        messages: builtins.list[dict[str, str]] | None = None,
        decisions: builtins.list[str] | None = None,
        user_constraints: builtins.list[str] | None = None,
    ) -> CompactedContext:
        state = self.load_task(task_id)
        recent = self._sanitize_value((messages or [])[-8:])
        compacted = CompactedContext(
            current_goal=state.interpreted_goal or state.original_request,
            original_requirements=state.original_request,
            active_plan=state.plan,
            completed_work=state.completed_steps,
            files_modified=state.files_changed,
            important_code_locations=_unique(
                [*state.files_inspected[-20:], *state.files_changed], limit=30
            ),
            architectural_decisions=self._sanitize_value(decisions or []),
            user_constraints=self._sanitize_value(user_constraints or []),
            test_results=state.test_status,
            errors=state.errors,
            unresolved_issues=[*state.unresolved_questions, *state.unresolved_problems],
            current_hypotheses=state.hypotheses,
            next_recommended_action=(
                state.current_step
                or (state.pending_steps[0] if state.pending_steps else "Verify and finish the task")
            ),
            recent_conversation=recent,
        )
        self.update_task(task_id, compacted_context=compacted.model_dump(mode="json"))
        self.log.emit("context_compacted", task_id=task_id)
        return compacted

    def create_episode(
        self,
        *,
        session_id: str | None,
        task_id: str | None,
        goal: str,
        summary: str,
        major_actions: builtins.list[str] | None = None,
        discoveries: builtins.list[str] | None = None,
        decisions: builtins.list[str] | None = None,
        files_changed: builtins.list[str] | None = None,
        commands: builtins.list[str] | None = None,
        test_results: dict[str, Any] | None = None,
        errors: builtins.list[str] | None = None,
        outcome: str = "",
        unresolved_work: builtins.list[str] | None = None,
    ) -> str:
        episode_id = new_id("episode")
        with self.database.session() as session:
            session.add(
                MemoryEpisode(
                    id=episode_id,
                    project_id=self.database.project().id,
                    session_id=session_id,
                    task_id=task_id,
                    goal=self._sanitize(goal),
                    summary=self._sanitize(summary),
                    major_actions=self._sanitize_value(major_actions or []),
                    discoveries=self._sanitize_value(discoveries or []),
                    decisions=self._sanitize_value(decisions or []),
                    files_changed=self._sanitize_value(files_changed or []),
                    commands=self._sanitize_value(commands or []),
                    test_results=self._sanitize_value(test_results or {}),
                    errors=[self._sanitize(item) for item in (errors or [])],
                    outcome=self._sanitize(outcome),
                    unresolved_work=self._sanitize_value(unresolved_work or []),
                )
            )
        self.remember(
            f"Goal: {goal}\nSummary: {summary}\nOutcome: {outcome}",
            memory_type=MemoryType.EPISODE,
            scope=MemoryScope.PROJECT,
            summary=summary,
            importance=0.65,
            confidence=0.9,
            source=f"episode:{episode_id}",
            source_type="session",
            task_id=task_id,
            session_id=session_id,
            tags=["episode"],
            metadata={
                "episode_id": episode_id,
                "files_changed": files_changed or [],
                "commands": commands or [],
                "test_results": test_results or {},
                "unresolved_work": unresolved_work or [],
            },
        )
        self.log.emit("episode_created", episode_id=episode_id, task_id=task_id)
        return episode_id

    def complete_task(
        self,
        task_id: str,
        *,
        summary: str,
        outcome: str = "completed",
        create_episode: bool = True,
    ) -> WorkingMemory:
        state = self.load_task(task_id)
        status = (
            PersistentTaskStatus.COMPLETED
            if outcome == "completed"
            else PersistentTaskStatus.FAILED
            if outcome == "failed"
            else PersistentTaskStatus.BLOCKED
        )
        updated = self.update_task(task_id, status=status, last_action=outcome)
        if create_episode:
            self.create_episode(
                session_id=state.session_id,
                task_id=task_id,
                goal=state.interpreted_goal or state.original_request,
                summary=summary,
                major_actions=state.completed_steps,
                files_changed=state.files_changed,
                commands=[str(item.get("command", "")) for item in state.commands_executed],
                test_results=state.test_status,
                errors=state.errors,
                outcome=outcome,
                unresolved_work=state.unresolved_problems,
            )
        return updated

    def extract(
        self,
        text: str,
        *,
        task_id: str | None = None,
        session_id: str | None = None,
        source: str = "session",
        source_type: str = "agent",
    ) -> builtins.list[str]:
        """Conservatively promote only explicit durable candidates.

        This deterministic pass handles obvious decisions and across-project
        preferences. Richer LLM extraction can call ``remember`` with validated
        candidates without changing persistence or retrieval.
        """
        if not self.config.auto_extract:
            return []
        created: builtins.list[str] = []
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
            value = sentence.strip(" -*")
            if len(value) < 12 or len(value) > 500:
                continue
            lower = value.casefold()
            global_preference = bool(
                re.search(r"\b(for all|always|across (?:all )?projects|i prefer)\b", lower)
            )
            decision = bool(
                re.search(r"\b(use|choose|keep|preserve|do not|don't|must)\b", lower)
                and re.search(
                    r"\b(rather than|instead of|architecture|api|database|framework|compatib)",
                    lower,
                )
            )
            if global_preference and self.config.user_memory_enabled:
                created.append(
                    self.remember(
                        value,
                        memory_type=MemoryType.USER,
                        scope=MemoryScope.GLOBAL,
                        summary=value,
                        importance=0.8,
                        confidence=0.8,
                        source=source,
                        source_type=source_type,
                        task_id=task_id,
                        session_id=session_id,
                        rationale="Explicit durable cross-project preference",
                    )
                )
            elif decision:
                created.append(
                    self.remember_decision(
                        value,
                        source=source,
                        scope=MemoryScope.PROJECT,
                        task_id=task_id,
                    )
                )
        return created
