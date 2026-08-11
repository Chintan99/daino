from __future__ import annotations

from pathlib import Path

from vasuki.config import default_settings
from vasuki.memory import MemoryManager, MemoryStatus
from vasuki.persistence import Database


def test_three_session_learning_resume_and_stale_correction(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    root.mkdir()
    source = root / "backend.toml"
    source.write_text('framework = "flask"\n', encoding="utf-8")
    settings = default_settings(root)
    database = Database(settings, root)
    database.initialize()
    global_path = tmp_path / "user-memory.db"

    # Session 1: learn a sourced project fact and checkpoint an unfinished task.
    first = MemoryManager(database, root, settings, global_path=global_path)
    memory_id = first.remember(
        "The backend framework is Flask.",
        source="backend.toml",
        source_type="repository",
        confidence=0.95,
    )
    task = first.start_task(
        "Migrate the backend framework.",
        interpreted_goal="Migrate Flask to FastAPI",
        mission_id="mission-migration",
        status="in_progress",
    )
    first.record_action(task.task_id, action="read_file", paths=["backend.toml"])
    first.close()

    # Session 2: a new process-level manager retrieves both task and fact.
    second = MemoryManager(database, root, settings, global_path=global_path)
    assert second.resumable_tasks()[0].task_id == task.task_id
    assert second.search("backend Flask framework")[0].id == memory_id
    second.close()

    # Repository changes between sessions. Session 3 trusts it and invalidates memory.
    source.write_text('framework = "fastapi"\n', encoding="utf-8")
    third = MemoryManager(database, root, settings, global_path=global_path)
    try:
        assert not third.search("backend Flask framework")
        assert third.get(memory_id).status == MemoryStatus.STALE
        replacement = third.supersede(
            memory_id,
            "The backend framework is FastAPI.",
            source="backend.toml",
            source_type="repository",
        )
        assert third.search("backend FastAPI framework")[0].id == replacement
    finally:
        third.close()
        database.engine.dispose()
