"""Repository search with filters, previewed replacement, and run-config discovery.

The replacement tests carry most of the weight. Search-and-replace over a whole
tree is one of the few editor operations that can quietly ruin a working copy,
so what is asserted here is mostly about restraint: a literal query stays
literal, a preview writes nothing, an unticked file is left alone, and a file's
trailing newline survives.
"""

from __future__ import annotations

import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest

from daino.repository import runconfigs
from daino.repository.search import (
    SearchQuery,
    apply_replacement,
    iter_files,
    matches_filters,
    search,
)


@pytest.fixture
def project(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.ts").write_text(
        "const total = 1;\nexport function total() {}\n", encoding="utf-8"
    )
    (tmp_path / "src" / "util.py").write_text(
        "total = 2\n# subtotal is different\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("The total is 3.\n", encoding="utf-8")
    # Everything below here must be invisible to a search.
    for skipped in ("node_modules", ".venv", "dist", "__pycache__"):
        directory = tmp_path / skipped
        directory.mkdir()
        (directory / "junk.js").write_text("total total total\n", encoding="utf-8")
    yield tmp_path


# ------------------------------------------------------------------ walking


def test_generated_and_dependency_trees_are_never_walked(project: Path) -> None:
    """A result list swamped by node_modules is a result list nobody reads."""
    found = {path.relative_to(project).as_posix() for path in iter_files(project)}

    assert found == {"src/app.ts", "src/util.py", "README.md"}


def test_a_symlink_is_not_followed(tmp_path: Path) -> None:
    """Following one risks a cycle, or walking out of the repository."""
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "a.txt").write_text("x\n", encoding="utf-8")
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)

    found = {path.relative_to(tmp_path).as_posix() for path in iter_files(tmp_path)}

    assert found == {"real/a.txt"}


# ------------------------------------------------------------------ matching


def test_a_literal_query_is_not_a_regex(project: Path) -> None:
    """Searching for `a.b` must find `a.b`, not `axb`.

    The most common surprise in a search box that treats everything as a
    pattern.
    """
    (project / "src" / "dots.txt").write_text("a.b\naxb\n", encoding="utf-8")

    result = search(project, SearchQuery(query="a.b"))

    assert [match.text for match in result.matches] == ["a.b"]


def test_a_regex_query_is_a_regex(project: Path) -> None:
    result = search(project, SearchQuery(query=r"tot\w+", regex=True))

    assert result.matches
    assert all("tot" in match.text for match in result.matches)


def test_an_invalid_regex_is_reported_not_raised(project: Path) -> None:
    result = search(project, SearchQuery(query="([unclosed", regex=True))

    assert result.error
    assert result.matches == []


def test_case_and_whole_word_narrow_the_results(project: Path) -> None:
    (project / "case.txt").write_text("Total\ntotal\nsubtotal\n", encoding="utf-8")

    insensitive = search(project, SearchQuery(query="total"))
    sensitive = search(project, SearchQuery(query="Total", case_sensitive=True))
    whole = search(
        project, SearchQuery(query="total", whole_word=True, include=("case.txt",))
    )

    assert len(insensitive.matches) > len(sensitive.matches)
    assert [match.text for match in sensitive.matches] == ["Total"]
    # "subtotal" contains "total" but is not the word "total".
    assert {match.text for match in whole.matches} == {"Total", "total"}


def test_a_match_carries_its_column_and_length(project: Path) -> None:
    """So the editor can select the match rather than just scroll to the line."""
    result = search(project, SearchQuery(query="total", include=("README.md",)))

    match = result.matches[0]
    assert match.path == "README.md"
    assert match.line == 1
    assert match.column == 5  # "The total is 3." — 1-based
    assert match.length == 5


def test_include_and_exclude_take_globs(project: Path) -> None:
    only_ts = search(project, SearchQuery(query="total", include=("*.ts",)))
    no_markdown = search(project, SearchQuery(query="total", exclude=("*.md",)))
    by_directory = search(project, SearchQuery(query="total", include=("src/**",)))

    assert {match.path for match in only_ts.matches} == {"src/app.ts"}
    assert "README.md" not in {match.path for match in no_markdown.matches}
    assert {match.path for match in by_directory.matches} == {
        "src/app.ts",
        "src/util.py",
    }


def test_a_bare_glob_matches_on_the_basename() -> None:
    """`*.ts` should mean "any .ts file", not "one at the root"."""
    assert matches_filters("src/deep/app.ts", SearchQuery(query="x", include=("*.ts",)))
    assert not matches_filters(
        "src/deep/app.py", SearchQuery(query="x", include=("*.ts",))
    )


def test_a_truncated_search_says_so(project: Path) -> None:
    """Implying it found everything when it stopped early would be worse."""
    (project / "many.txt").write_text("total\n" * 50, encoding="utf-8")

    result = search(project, SearchQuery(query="total", limit=5))

    assert len(result.matches) == 5
    assert result.truncated is True


def test_unreadable_files_are_counted_not_hidden(project: Path) -> None:
    """A search that quietly ignored half a repo is worse than one that admits it."""
    (project / "logo.bin").write_bytes(b"\x00\x01total\x02")

    result = search(project, SearchQuery(query="total"))

    assert result.skipped >= 1


# --------------------------------------------------------------- replacement


def test_a_preview_writes_nothing(project: Path) -> None:
    before = (project / "src" / "app.ts").read_text(encoding="utf-8")

    result = search(project, SearchQuery(query="total"), replacement="sum")

    assert result.matches
    assert all(match.replacement for match in result.matches)
    assert "sum" in result.matches[0].replacement
    assert (project / "src" / "app.ts").read_text(encoding="utf-8") == before


def test_applying_writes_only_the_files_that_were_ticked(project: Path) -> None:
    """The whole point of a preview: accepting some of it."""
    query = SearchQuery(query="total")

    summary = apply_replacement(project, query, "sum", only_paths=["src/util.py"])

    assert summary.files == ["src/util.py"]
    assert "sum = 2" in (project / "src" / "util.py").read_text(encoding="utf-8")
    # The other matches are untouched.
    assert "total" in (project / "src" / "app.ts").read_text(encoding="utf-8")
    assert "total" in (project / "README.md").read_text(encoding="utf-8")


def test_applying_with_no_selection_uses_the_filters(project: Path) -> None:
    query = SearchQuery(query="total", include=("*.ts",))

    summary = apply_replacement(project, query, "sum")

    assert summary.files == ["src/app.ts"]
    assert summary.replacements == 2
    assert "total" in (project / "src" / "util.py").read_text(encoding="utf-8")


def test_a_literal_replacement_is_taken_literally(project: Path) -> None:
    """Someone replacing a Windows path did not mean to write an escape."""
    (project / "paths.txt").write_text("see C:\\old here\n", encoding="utf-8")
    query = SearchQuery(query="C:\\old")

    apply_replacement(project, query, "D:\\new", only_paths=["paths.txt"])

    assert (project / "paths.txt").read_text(encoding="utf-8") == "see D:\\new here\n"


def test_regex_backreferences_work_in_a_regex_replacement(project: Path) -> None:
    (project / "swap.txt").write_text("alpha=1\nbeta=2\n", encoding="utf-8")
    query = SearchQuery(query=r"(\w+)=(\d+)", regex=True)

    apply_replacement(project, query, r"\2=\1", only_paths=["swap.txt"])

    assert (project / "swap.txt").read_text(encoding="utf-8") == "1=alpha\n2=beta\n"


def test_a_trailing_newline_survives_a_replacement(project: Path) -> None:
    """Otherwise every replaced file shows a spurious last-line change."""
    target = project / "src" / "util.py"
    assert target.read_text(encoding="utf-8").endswith("\n")

    apply_replacement(project, SearchQuery(query="total"), "sum", only_paths=["src/util.py"])

    assert target.read_text(encoding="utf-8").endswith("\n")


def test_a_file_without_a_trailing_newline_does_not_gain_one(project: Path) -> None:
    (project / "bare.txt").write_text("total", encoding="utf-8")

    apply_replacement(project, SearchQuery(query="total"), "sum", only_paths=["bare.txt"])

    assert (project / "bare.txt").read_text(encoding="utf-8") == "sum"


# ------------------------------------------------------------- run configs


def test_npm_scripts_become_run_configs(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"name": "app", "scripts": {"dev": "vite", "build": "vite build",'
        ' "test": "vitest run"}}',
        encoding="utf-8",
    )
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")

    found = {item.id: item for item in runconfigs.discover(tmp_path)}

    assert found["npm:dev"].command == "npm run dev"
    assert found["npm:dev"].kind == "run"
    assert found["npm:build"].kind == "build"
    assert found["npm:test"].kind == "test"


def test_the_lockfile_decides_the_package_manager(tmp_path: Path) -> None:
    """`npm run dev` in a pnpm project fails confusingly rather than loudly."""
    (tmp_path / "package.json").write_text(
        '{"name": "app", "scripts": {"dev": "vite"}}', encoding="utf-8"
    )
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: 9\n", encoding="utf-8")

    found = runconfigs.by_id(tmp_path, "npm:dev")

    assert found is not None
    assert found.command == "pnpm run dev"


def test_yarn_takes_the_script_name_directly(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        '{"name": "app", "scripts": {"dev": "vite"}}', encoding="utf-8"
    )
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")

    found = runconfigs.by_id(tmp_path, "npm:dev")

    assert found is not None
    assert found.command == "yarn dev"


def test_makefile_targets_are_found_and_recipes_are_not(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text(
        textwrap.dedent(
            """\
            .PHONY: build test
            VERSION := 1.0

            build:
            \tgo build ./...

            test: build
            \tgo test ./...
            """
        ),
        encoding="utf-8",
    )

    found = {item.id for item in runconfigs.discover(tmp_path)}

    assert "make:build" in found
    assert "make:test" in found
    # `.PHONY` is a convention and `VERSION :=` is an assignment.
    assert not any(item.startswith("make:.PHONY") for item in found)
    assert not any("VERSION" in item for item in found)


def test_compose_services_are_offered_individually(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text(
        textwrap.dedent(
            """\
            services:
              web:
                image: nginx
              db:
                image: postgres
            volumes:
              data:
            """
        ),
        encoding="utf-8",
    )

    found = {item.id: item for item in runconfigs.discover(tmp_path)}

    assert found["compose:web"].command == "docker compose up web"
    assert "compose:db" in found
    # `volumes:` is a sibling block, not a service.
    assert "compose:data" not in found


def test_a_user_task_overrides_a_discovered_one(tmp_path: Path) -> None:
    """The discovered command is a good default until it is not."""
    (tmp_path / "package.json").write_text(
        '{"name": "app", "scripts": {"dev": "vite"}}', encoding="utf-8"
    )
    runconfigs.save_user_tasks(
        tmp_path,
        [
            {
                "id": "npm:dev",
                "label": "dev (with env)",
                "command": "DEBUG=1 npm run dev",
                "kind": "run",
            },
            {"label": "seed", "command": "./scripts/seed.sh", "kind": "other"},
        ],
    )

    found = {item.id: item for item in runconfigs.discover(tmp_path)}

    assert found["npm:dev"].command == "DEBUG=1 npm run dev"
    assert found["npm:dev"].source == "user"
    assert any(item.label == "seed" for item in found.values())


def test_a_malformed_tasks_file_is_ignored_rather_than_fatal(tmp_path: Path) -> None:
    from daino.config import paths

    directory = paths.state_dir(tmp_path, create=True)
    (directory / runconfigs.TASKS_FILE).write_text("{not json", encoding="utf-8")

    assert runconfigs.discover(tmp_path) == []


def test_python_entry_points_become_run_configs(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [project]
            name = "myapp"
            version = "0.1.0"
            scripts = { myapp = "myapp.cli:main" }
            """
        ),
        encoding="utf-8",
    )

    found = runconfigs.by_id(tmp_path, "python:myapp")

    assert found is not None
    assert found.detail == "myapp.cli:main"
