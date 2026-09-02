from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from sqlalchemy import inspect

from alembic import command
from daino.config import default_settings
from daino.memory import MemoryStore
from daino.persistence import Database
from daino.playbooks import PlaybookLoader


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
    tables = set(inspect(database.engine).get_table_names())
    assert "missions" in tables
    # The migration chain has to carry the executing half of a workspace too,
    # or an operator who migrates rather than letting create_all run gets a
    # database that cannot record a run.
    assert {
        "workspace_runs",
        "workspace_run_steps",
        "workspace_change_sets",
        "workspace_change_entries",
        "workspace_links",
    } <= tables
    task_columns = {
        item["name"] for item in inspect(database.engine).get_columns("workspace_tasks")
    }
    assert {"depends_on", "attempts", "error"} <= task_columns


def test_an_existing_plan_table_gains_the_columns_execution_needs(tmp_path: Path) -> None:
    """``create_all`` cannot widen a table, so restart recovery has a bridge.

    Simulates the database of a project that had workspaces before plans could
    be executed: the table exists, the columns do not, and every task read would
    fail until the bridge adds them.
    """
    from sqlalchemy import text

    settings = default_settings(tmp_path)
    database = Database(settings, tmp_path)
    database.initialize()
    with database.engine.begin() as connection:
        for column in ("depends_on", "attempts", "error"):
            connection.execute(text(f"ALTER TABLE workspace_tasks DROP COLUMN {column}"))
    assert "attempts" not in {
        item["name"] for item in inspect(database.engine).get_columns("workspace_tasks")
    }

    Database(settings, tmp_path).initialize()

    columns = {item["name"] for item in inspect(database.engine).get_columns("workspace_tasks")}
    assert {"depends_on", "attempts", "error"} <= columns
    database.engine.dispose()


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
