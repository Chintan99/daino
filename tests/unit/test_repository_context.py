from __future__ import annotations

from pathlib import Path

from vasuki.context import ContextCompiler
from vasuki.repository import RepositoryIndexer
from vasuki.repository.syntax import extract_outline
from vasuki.schemas import TaskSpec


def test_repository_index_queries_and_incremental_reuse(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text(
        "import os\nfrom fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/items')\n"
        "def list_items():\n"
        "    return os.getenv('ITEM_MODE')\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import list_items\n\ndef test_items():\n    assert list_items() is None\n",
        encoding="utf-8",
    )
    indexer = RepositoryIndexer(tmp_path)
    first = indexer.build()
    second = indexer.build()
    assert len(first.files) == len(second.files) == 2
    assert indexer.find_symbol("list_items")[0].path == "app.py"
    assert indexer.api_routes()[0]["route"] == "/items"
    assert indexer.tests() == ["tests/test_app.py"]
    assert indexer.environment_variables()[0]["name"] == "ITEM_MODE"
    assert "FastAPI" in first.frameworks


def test_context_compiler_respects_budget_and_relevance(tmp_path: Path) -> None:
    (tmp_path / "invoice.py").write_text("def total():\n    return 1\n", encoding="utf-8")
    (tmp_path / "unrelated.py").write_text("x = 'x' * 1000\n", encoding="utf-8")
    indexer = RepositoryIndexer(tmp_path)
    indexer.build()
    task = TaskSpec(
        id="task",
        title="Update invoice total",
        objective="Change invoice total",
        expected_files=["invoice.py"],
        acceptance_criteria=["total is updated"],
        verification_commands=["pytest"],
    )
    bundle = ContextCompiler(tmp_path, indexer, token_budget=200).compile(task)
    assert "invoice.py" in bundle.files
    assert "unrelated.py" not in bundle.files
    assert bundle.token_estimate <= 200


def test_tree_sitter_extracts_typescript_symbols() -> None:
    outline = extract_outline(
        "service.ts",
        b"export class InvoiceService { total(): number { return 1; } }\n",
    )
    assert outline is not None
    assert outline.parser == "tree-sitter:typescript"
    assert any(item.name == "InvoiceService" for item in outline.symbols)
