from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from sqlalchemy import inspect

from alembic import command
from vasuki.config import default_settings
from vasuki.memory import MemoryStore
from vasuki.persistence import Database
from vasuki.playbooks import PlaybookLoader


def test_database_initializes_complete_schema(tmp_path: Path) -> None:
    settings = default_settings(tmp_path)
    database = Database(settings, tmp_path)
    database.initialize()
    tables = set(inspect(database.engine).get_table_names())
    assert {
        "missions",
        "requirement_versions",
        "tasks",
        "model_calls",
        "tool_calls",
        "verification_runs",
        "deployment_runs",
        "memory_records",
        "memory_embeddings",
        "memory_episodes",
        "persistent_task_states",
    } <= tables


def test_all_builtin_playbooks_validate(tmp_path: Path) -> None:
    playbooks = PlaybookLoader(tmp_path).list()
    assert len(playbooks) == 10
    assert all(item.verification_steps and item.rollback_steps for item in playbooks)


def test_alembic_initial_migration(tmp_path: Path) -> None:
    repository_root = Path(__file__).parents[2]
    config = Config(str(repository_root / "alembic.ini"))
    config.set_main_option("script_location", str(repository_root / "alembic"))
    database_path = tmp_path / "migration.db"
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "head")
    settings = default_settings(tmp_path)
    settings.database.url = f"sqlite:///{database_path}"
    database = Database(settings, tmp_path)
    assert "missions" in inspect(database.engine).get_table_names()


def test_memory_and_architecture_decisions(project: tuple[Path, object, Database]) -> None:
    _, _, database = project
    memory = MemoryStore(database)
    record_id = memory.remember(
        category="authoritative",
        source="human",
        scope="api",
        content={"rule": "published records are immutable"},
        related_files=["models.py"],
    )
    memory.validate(record_id, confidence=1, approved=True)
    decision_id = memory.add_decision(
        title="Immutable published records",
        decision="Published records cannot be edited",
        implementation_rule="Create a new draft version",
        related_files=["models.py"],
    )
    assert memory.query(category="authoritative")[0].human_approval_status == "approved"
    decisions = memory.relevant_decisions(["models.py"])
    assert decision_id in decisions[0]
