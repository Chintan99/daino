from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from daino.config import default_settings
from daino.config.models import MemoryConfig, ModelProfileConfig
from daino.context import ContextBuilder, ContextCompiler, ModelExecutionProfile
from daino.memory import (
    DisabledEmbeddingProvider,
    InstructionResolver,
    MemoryManager,
    MemoryScope,
    MemoryStatus,
    MemoryType,
    PersistentTaskStatus,
    error_fingerprint,
)
from daino.persistence import Database
from daino.repository import RepositoryIndexer
from daino.schemas import AgentAction, TaskSpec
from daino.tools import ActionExecutor, EditTools


def manager_for(root: Path, global_path: Path) -> tuple[MemoryManager, Database]:
    settings = default_settings(root)
    database = Database(settings, root)
    database.initialize()
    return MemoryManager(database, root, settings, global_path=global_path), database


def test_memory_creation_and_hybrid_retrieval(tmp_path: Path) -> None:
    manager, database = manager_for(tmp_path / "project", tmp_path / "global.db")
    try:
        memory_id = manager.remember(
            "Integration tests require Redis.",
            summary="Redis is required for integration tests",
            source="tests/integration/conftest.py",
            source_type="repository",
            importance=0.8,
            confidence=0.9,
            tags=["tests", "redis"],
        )
        matches = manager.search("How do integration tests use Redis?", debug=True)
        assert matches[0].id == memory_id
        assert "lexical=" in " ".join(matches[0].why)
    finally:
        manager.close()
        database.engine.dispose()


def test_project_isolation_and_global_user_memory(tmp_path: Path) -> None:
    global_path = tmp_path / "global.db"
    first, first_db = manager_for(tmp_path / "one", global_path)
    second, second_db = manager_for(tmp_path / "two", global_path)
    try:
        first.remember("Project one uses MongoDB.", source_type="repository")
        global_id = first.remember(
            "For all Python projects I prefer uv instead of pip.",
            memory_type=MemoryType.USER,
            scope=MemoryScope.GLOBAL,
            source="user",
            source_type="user",
        )
        assert not second.search("MongoDB")
        assert second.search("Python uv pip")[0].id == global_id
    finally:
        first.close()
        second.close()
        first_db.engine.dispose()
        second_db.engine.dispose()


def test_decision_precedence_and_relevance(tmp_path: Path) -> None:
    manager, database = manager_for(tmp_path / "project", tmp_path / "global.db")
    try:
        decision = manager.remember_decision(
            "Use RabbitMQ directly rather than Celery.",
            reason="The service needs direct routing semantics.",
        )
        manager.remember_decision("Preserve the public API response format.")
        matches = manager.search("Should this queue use Celery or RabbitMQ?")
        assert matches[0].id == decision
        assert matches[0].type == MemoryType.DECISION
        assert all("response format" not in item.content for item in matches)
        manager.reverse_decision(decision, reason="The broker was removed.")
        assert manager.get(decision).metadata["decision_status"] == "reversed"
        assert not manager.search("Should this queue use Celery or RabbitMQ?")
    finally:
        manager.close()
        database.engine.dispose()


def test_failure_fingerprint_and_solution_retrieval(tmp_path: Path) -> None:
    manager, database = manager_for(tmp_path / "project", tmp_path / "global.db")
    try:
        memory_id = manager.remember_failure(
            "could not select device driver nvidia with capabilities [[gpu]]",
            cause="NVIDIA Container Toolkit missing.",
            solution="Install and configure NVIDIA Container Toolkit for Docker.",
        )
        current = "ERROR: could not select device driver nvidia with capabilities [[gpu]]"
        assert error_fingerprint("timeout on device 17") == error_fingerprint(
            "timeout on device 42"
        )
        matches = manager.retrieve_for_task("Docker command failed", errors=[current])
        assert matches[0].id == memory_id
        assert matches[0].metadata["successful_fix"].startswith("Install")
    finally:
        manager.close()
        database.engine.dispose()


def test_source_change_marks_memory_stale_and_repository_wins(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    source = root / "compose.yaml"
    source.write_text("services:\n  mongodb:\n    image: mongo\n", encoding="utf-8")
    manager, database = manager_for(root, tmp_path / "global.db")
    try:
        memory_id = manager.remember(
            "The database is MongoDB.",
            source="compose.yaml",
            source_type="repository",
            confidence=0.95,
        )
        assert manager.search("database MongoDB")[0].id == memory_id
        source.write_text("services:\n  postgres:\n    image: postgres\n", encoding="utf-8")
        assert not manager.search("database MongoDB")
        assert manager.get(memory_id).status == MemoryStatus.STALE
        manager.verify(memory_id, confidence=0.2)
        assert manager.get(memory_id).status == MemoryStatus.ACTIVE
    finally:
        manager.close()
        database.engine.dispose()


def test_superseding_preserves_history_but_removes_old_from_retrieval(tmp_path: Path) -> None:
    manager, database = manager_for(tmp_path / "project", tmp_path / "global.db")
    try:
        old = manager.remember("The backend uses Flask.")
        new = manager.supersede(old, "The backend uses FastAPI.")
        assert new is not None
        assert manager.get(old).status == MemoryStatus.SUPERSEDED
        matches = manager.search("backend FastAPI Flask", include_stale=True)
        assert new in {item.id for item in matches}
        assert old not in {item.id for item in matches}
    finally:
        manager.close()
        database.engine.dispose()


def test_task_persistence_compaction_and_restart_recovery(tmp_path: Path) -> None:
    root = tmp_path / "project"
    manager, database = manager_for(root, tmp_path / "global.db")
    task = manager.start_task(
        "Implement authentication without Redis.",
        interpreted_goal="Add JWT authentication",
        plan=[
            {"content": "Add service", "status": "in_progress"},
            {"content": "Run tests", "status": "pending"},
        ],
        mission_id="mission-1",
        session_id="session-1",
        status=PersistentTaskStatus.IN_PROGRESS,
    )
    manager.record_action(task.task_id, action="read_file", paths=["auth/service.py"])
    manager.record_action(
        task.task_id,
        action="run_command",
        command="pytest tests/test_auth.py",
        success=False,
        error="one test failed",
    )
    compacted = manager.compact(
        task.task_id,
        messages=[{"role": "user", "content": "Keep API compatibility."}],
        user_constraints=["Do not introduce Redis."],
    )
    assert compacted.current_goal == "Add JWT authentication"
    assert compacted.errors == ["one test failed"]
    manager.close()

    restarted = MemoryManager(
        database, root, default_settings(root), global_path=tmp_path / "global.db"
    )
    try:
        resumed = restarted.resumable_tasks()[0]
        assert resumed.task_id == task.task_id
        assert resumed.files_inspected == ["auth/service.py"]
        assert resumed.commands_executed[0]["command"] == "pytest tests/test_auth.py"
        assert resumed.compacted_context["user_constraints"] == ["Do not introduce Redis."]
    finally:
        restarted.close()
        database.engine.dispose()


def test_nested_vasuki_instructions_and_keyed_conflict_resolution(tmp_path: Path) -> None:
    root = tmp_path / "project"
    backend = root / "backend" / "auth"
    backend.mkdir(parents=True)
    global_file = tmp_path / "home" / "VASUKI.md"
    global_file.parent.mkdir()
    global_file.write_text("python_version: 3.10\nPrefer small changes.\n", encoding="utf-8")
    (root / "VASUKI.md").write_text("python_version: 3.12\nUse service layers.\n", encoding="utf-8")
    (root / "backend" / "VASUKI.md").write_text(
        "API routes must call the service layer.\n", encoding="utf-8"
    )
    resolved = InstructionResolver(root, global_path=global_file).resolve(
        ["backend/auth/service.py"],
        user_instruction="Preserve response compatibility.",
    )
    assert "python_version: 3.12" in resolved.text
    assert "python_version: 3.10" not in resolved.text
    assert "API routes must call the service layer" in resolved.text
    assert resolved.text.index("[repository]") < resolved.text.index("[scoped:backend/VASUKI.md]")
    assert resolved.text.rstrip().endswith("Preserve response compatibility.")


def test_context_builder_centralizes_instructions_task_memory_and_source(tmp_path: Path) -> None:
    root = tmp_path / "project"
    backend = root / "backend"
    backend.mkdir(parents=True)
    (backend / "service.py").write_text("FRAMEWORK = 'fastapi'\n", encoding="utf-8")
    (backend / "VASUKI.md").write_text("Keep route handlers thin.\n", encoding="utf-8")
    settings = default_settings(root)
    database = Database(settings, root)
    database.initialize()
    manager = MemoryManager(database, root, settings, global_path=tmp_path / "global.db")
    try:
        manager.remember(
            "Backend services use FastAPI.",
            source="backend/service.py",
            source_type="repository",
        )
        task = manager.start_task(
            "Update the backend service.",
            interpreted_goal="Keep the FastAPI service compatible",
            status="in_progress",
        )
        bundle = ContextBuilder(root, settings, manager).build(
            TaskSpec(
                id="task",
                title="Update FastAPI service",
                objective="Update the FastAPI backend service",
                expected_files=["backend/service.py"],
                allowed_files=["backend/service.py"],
                acceptance_criteria=["Compatibility is preserved"],
                verification_commands=[],
            ),
            current_user_instruction="Do not change the public response.",
            task_state_id=task.task_id,
        )
        assert "Keep route handlers thin" in bundle.effective_instructions
        assert bundle.working_memory["interpreted_goal"] == "Keep the FastAPI service compatible"
        assert bundle.relevant_memories[0]["content"] == "Backend services use FastAPI."
        assert "backend/service.py" in bundle.files
        assert "current repository/source code" in bundle.memory_precedence
    finally:
        manager.close()
        database.engine.dispose()


def test_compact_model_gets_bounded_task_packet_with_staged_retrieval(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    paths = [f"service_{number}.py" for number in range(6)]
    for number, path in enumerate(paths):
        (root / path).write_text(
            f"def service_{number}():\n    return {number}\n" + "# detail\n" * 100,
            encoding="utf-8",
        )
    settings = default_settings(root)
    database = Database(settings, root)
    database.initialize()
    manager = MemoryManager(database, root, settings, global_path=tmp_path / "global.db")
    profile = ModelExecutionProfile.resolve(
        "tiny",
        ModelProfileConfig(
            provider="local",
            model="tiny-coder",
            local=True,
            execution_mode="compact",
        ),
        input_budget_tokens=12_000,
        project_budget_tokens=24_000,
        memory_items=8,
        memory_tokens=2_000,
    )
    try:
        for number in range(6):
            manager.remember(f"Service {number} follows compatibility rule {number}.")
        bundle = ContextBuilder(root, settings, manager).build(
            TaskSpec(
                id="compact",
                title="Update services",
                objective="Update every service safely",
                expected_files=paths,
                allowed_files=paths,
                acceptance_criteria=["All services remain compatible"],
                verification_commands=["pytest -q"],
            ),
            execution_profile=profile,
        )
        assert bundle.execution_mode == "compact"
        assert bundle.retrieval_stage == "initial"
        assert bundle.task_packet is not None
        assert bundle.task_packet.objective == "Update every service safely"
        assert bundle.task_packet.verification_commands == ["pytest -q"]
        assert len(bundle.included_paths) <= 4
        assert len(bundle.relevant_memories) <= 4
        assert any("source files" in item for item in bundle.omitted_context)
        assert "memory_search" in bundle.task_packet.retrieval_hint
    finally:
        manager.close()
        database.engine.dispose()


def test_compact_source_prefers_exact_relevant_symbol_window(tmp_path: Path) -> None:
    source = "\n".join(
        ["def unrelated_start():", "    return 'start'", *("# filler" for _ in range(300))]
        + ["def resolve_redirect(user):", "    return '/dashboard' if user else '/'"]
        + ["# tail" for _ in range(300)]
    )
    (tmp_path / "auth.py").write_text(source, encoding="utf-8")
    indexer = RepositoryIndexer(tmp_path)
    indexer.build()
    bundle = ContextCompiler(
        tmp_path,
        indexer,
        token_budget=1_000,
        per_file_tokens=300,
        prefer_symbol_slices=True,
    ).compile(
        TaskSpec(
            id="redirect",
            title="Fix redirect",
            objective="Correct resolve_redirect",
            expected_files=["auth.py"],
            allowed_files=["auth.py"],
            relevant_symbols=["resolve_redirect"],
            acceptance_criteria=["Redirect is correct"],
            verification_commands=[],
        )
    )

    shown = bundle.files["auth.py"]
    assert "def resolve_redirect(user):" in shown
    assert "    return '/dashboard' if user else '/'" in shown
    assert "def unrelated_start" not in shown
    assert len(shown) < len(source)


def test_secret_redaction_and_env_omission(tmp_path: Path) -> None:
    manager, database = manager_for(tmp_path / "project", tmp_path / "global.db")
    try:
        secret = manager.remember(
            "Authorization: Bearer sk-abcdefghijklmnopqrstuvwxyz\npassword=hunter2",
            source="terminal",
        )
        env = manager.remember("TOKEN=ghp_abcdefghijklmnopqrstuvwxyz", source=".env")
        assert "sk-" not in manager.get(secret).content
        assert "hunter2" not in manager.get(secret).content
        assert manager.get(env).content == "[Sensitive .env source omitted from memory]"
    finally:
        manager.close()
        database.engine.dispose()


def test_embedding_disabled_operation_and_ranking(tmp_path: Path) -> None:
    manager, database = manager_for(tmp_path / "project", tmp_path / "global.db")
    manager.embedding = DisabledEmbeddingProvider()
    try:
        low = manager.remember("Tests use Redis fixtures.", importance=0.1, confidence=0.4)
        high = manager.remember("Integration tests require Redis.", importance=1.0, confidence=1.0)
        matches = manager.search("Redis integration tests")
        assert matches[0].id == high
        assert low in {item.id for item in matches}
    finally:
        manager.close()
        database.engine.dispose()


def test_embedding_failure_falls_back_and_secret_config_is_validated(tmp_path: Path) -> None:
    class BrokenEmbedding:
        name = "broken"
        model = "broken"

        def embed(self, text: str) -> list[float]:
            raise RuntimeError("embedding server is offline")

    with pytest.raises(ValidationError):
        MemoryConfig(embedding_api_key="literal-secret")

    root = tmp_path / "project"
    settings = default_settings(root)
    database = Database(settings, root)
    database.initialize()
    manager = MemoryManager(
        database,
        root,
        settings,
        global_path=tmp_path / "global.db",
        embedding_provider=BrokenEmbedding(),
    )
    try:
        memory_id = manager.remember("The API uses FastAPI.")
        assert manager.search("FastAPI API")[0].id == memory_id
    finally:
        manager.close()
        database.engine.dispose()


def test_promotion_preserves_identity_and_episode_is_retrievable(tmp_path: Path) -> None:
    manager, database = manager_for(tmp_path / "project", tmp_path / "global.db")
    try:
        memory_id = manager.remember(
            "For all projects prefer minimal dependencies.",
            memory_type=MemoryType.USER,
            scope=MemoryScope.SESSION,
            source="user",
            source_type="user",
        )
        assert manager.promote(memory_id, MemoryScope.GLOBAL) == memory_id
        assert manager.get(memory_id).scope == MemoryScope.GLOBAL

        episode_id = manager.create_episode(
            session_id="session-1",
            task_id="task-1",
            goal="Fix WebSocket disconnects",
            summary="nginx proxy timeout was too low",
            files_changed=["nginx.conf"],
            commands=["nginx -t"],
            outcome="fixed and verified",
        )
        episodes = manager.search(
            "WebSocket nginx disconnect timeout",
            memory_types=[MemoryType.EPISODE],
        )
        assert episodes[0].metadata["episode_id"] == episode_id
    finally:
        manager.close()
        database.engine.dispose()


@pytest.mark.asyncio
async def test_validated_memory_agent_tools(tmp_path: Path) -> None:
    root = tmp_path / "project"
    manager, database = manager_for(root, tmp_path / "global.db")
    executor = ActionExecutor(EditTools(root), memory=manager, memory_session_id="session-1")
    try:
        saved, _ = await executor.execute(
            AgentAction(
                thought="This is a durable project fact.",
                action="memory_save",
                content="The frontend HTTP client lives in src/lib/api.ts.",
                summary="Frontend HTTP client location",
                source="src/lib/api.ts",
                confidence=0.9,
            )
        )
        assert saved.success
        searched, _ = await executor.execute(
            AgentAction(
                thought="Recall the client location.",
                action="memory_search",
                query="frontend HTTP client",
            )
        )
        assert searched.success
        assert searched.data["memories"][0]["id"] == saved.data["memory_id"]
    finally:
        manager.close()
        database.engine.dispose()
