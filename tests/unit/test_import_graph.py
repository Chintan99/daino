"""Turning recorded import statements into file-level edges.

The index has collected ``RepositoryFile.imports`` for every file since it was
written, and until now only the architecture diagram read them — at module
granularity, and with relative imports resolved against the repository root
rather than the importing file. These tests pin the resolution, because
everything downstream of it is only as good as the edges it produces.
"""

from __future__ import annotations

from daino.repository.graph import (
    BARREL_FANOUT_LIMIT,
    HUB_FANIN_LIMIT,
    ImportGraph,
    import_fragment,
    is_test_path,
    resolve_import,
)
from tests.conftest import repository_index

# ------------------------------------------------------------------ resolution


def test_a_relative_import_resolves_against_the_importing_file() -> None:
    """The bug this module exists to fix.

    ``../../api/client`` from ``src/components/agent/Panel.tsx`` means
    ``src/api/client.ts``. Resolving it against the repository root instead
    yields ``api/client``, which matches nothing, so the edge is silently lost —
    and on this repository that is every one of the front end's 521 imports.
    """
    paths = {"src/api/client.ts", "src/components/agent/Panel.tsx"}

    resolved = resolve_import("../../api/client", "src/components/agent/Panel.tsx", paths)

    assert resolved == "src/api/client.ts"


def test_each_dot_pair_is_one_directory_not_one_character() -> None:
    """Counting the leading dot run resolves `../../` one level too deep."""
    # From a/b/c/d.ts: "./" is a/b/c, "../" is a/b, "../../" is a.
    assert import_fragment("./x", "a/b/c/d.ts") == "a/b/c/x"
    assert import_fragment("../lib/x", "a/b/c/d.ts") == "a/b/lib/x"
    assert import_fragment("../../lib/x", "a/b/c/d.ts") == "a/lib/x"


def test_an_import_naming_a_directory_resolves_to_its_entry_point() -> None:
    paths = {"src/components/Button/index.tsx", "src/App.tsx"}

    assert resolve_import("./components/Button", "src/App.tsx", paths) == (
        "src/components/Button/index.tsx"
    )


def test_a_python_package_import_resolves_to_its_init() -> None:
    paths = {"daino/repository/__init__.py", "daino/context/compiler.py"}

    assert resolve_import("daino.repository", "daino/context/compiler.py", paths) == (
        "daino/repository/__init__.py"
    )


def test_a_module_is_preferred_over_the_package_of_the_same_name() -> None:
    paths = {"daino/repository.py", "daino/repository/__init__.py", "a.py"}

    assert resolve_import("daino.repository", "a.py", paths) == "daino/repository.py"


def test_external_packages_never_become_edges() -> None:
    """A graph of what the code imports from PyPI and npm is the lockfile."""
    paths = {"app.py", "src/App.tsx"}

    assert resolve_import("pathlib", "app.py", paths) == ""
    assert resolve_import("react", "src/App.tsx", paths) == ""
    assert resolve_import("@tanstack/react-query", "src/App.tsx", paths) == ""


def test_the_graph_cannot_nominate_a_path_that_is_not_indexed() -> None:
    """Structural, not a check: resolution is membership in the index.

    A retrieval layer that could name a nonexistent file would hand the agent a
    read that fails, every turn, with no way to tell a stale index from a typo.
    """
    graph = ImportGraph.build(
        repository_index({"a.py": ["b", "ghost", "also_missing"], "b.py": []})
    )

    assert graph.imports["a.py"] == {"b.py"}
    assert all(target in graph.paths for targets in graph.imports.values() for target in targets)


def test_a_file_never_imports_itself() -> None:
    graph = ImportGraph.build(repository_index({"a.py": ["a"], "b.py": []}))

    assert "a.py" not in graph.imports.get("a.py", set())


# -------------------------------------------------------------------- structure


def test_reverse_edges_are_the_exact_transpose() -> None:
    """Built in the same pass, because computing them twice lets them disagree."""
    graph = ImportGraph.build(
        repository_index(
            {
                "a.py": ["b", "c"],
                "b.py": ["c"],
                "c.py": [],
            }
        )
    )

    forward = {(src, dst) for src, dsts in graph.imports.items() for dst in dsts}
    reverse = {(src, dst) for dst, srcs in graph.imported_by.items() for src in srcs}

    assert forward == reverse
    assert graph.imported_by["c.py"] == {"a.py", "b.py"}


def test_a_hub_does_not_nominate_everything_that_imports_it() -> None:
    """ "Everything that imports the schemas package" is the repository."""
    importers = {f"mod_{index}.py": ["hub"] for index in range(HUB_FANIN_LIMIT + 10)}
    graph = ImportGraph.build(repository_index({"hub.py": [], **importers}))

    reached = [item.path for item in graph.neighbours("hub.py")]

    assert reached == []
    # The edges still exist — they are simply not a retrieval signal.
    assert len(graph.imported_by["hub.py"]) == HUB_FANIN_LIMIT + 10


def test_a_modest_fan_in_is_still_worth_following() -> None:
    importers = {f"mod_{index}.py": ["core"] for index in range(3)}
    graph = ImportGraph.build(repository_index({"core.py": [], **importers}))

    reached = {item.path for item in graph.neighbours("core.py")}

    assert reached == {"mod_0.py", "mod_1.py", "mod_2.py"}


def test_a_package_init_that_re_exports_is_a_barrel() -> None:
    graph = ImportGraph.build(
        repository_index({"pkg/__init__.py": ["pkg.a", "pkg.b"], "pkg/a.py": [], "pkg/b.py": []})
    )

    assert graph.is_barrel("pkg/__init__.py")
    assert not graph.is_barrel("pkg/a.py")


def test_a_fat_init_is_a_module_rather_than_a_barrel() -> None:
    """Hopping through it would spray candidates across the whole package."""
    modules = {f"pkg/m{index}.py": [] for index in range(BARREL_FANOUT_LIMIT + 5)}
    graph = ImportGraph.build(repository_index({"pkg/__init__.py": list(modules), **modules}))

    assert not graph.is_barrel("pkg/__init__.py")


def test_directory_siblings_are_available_for_a_file_with_no_edges() -> None:
    """The common planner case: a task scoped to a file that does not exist yet."""
    graph = ImportGraph.build(repository_index({"pkg/a.py": [], "pkg/b.py": [], "other/c.py": []}))

    assert graph.siblings("pkg/a.py") == {"pkg/b.py"}


# --------------------------------------------------------------------- symbols


def test_a_symbol_defined_in_one_place_identifies_that_file() -> None:
    graph = ImportGraph.build(
        repository_index({"a.py": [], "b.py": []}, symbols={"a.py": ["compile_bundle"]})
    )

    assert graph.defines("compile_bundle") == {"a.py"}


def test_a_symbol_defined_everywhere_is_not_a_signal() -> None:
    """`run`, `main` and `build` are in every third file and identify nothing."""
    files = {f"mod_{index}.py": [] for index in range(8)}
    graph = ImportGraph.build(repository_index(files, symbols={path: ["run"] for path in files}))

    assert graph.defines("run") == set()


# ------------------------------------------------------------- test detection


def test_one_predicate_decides_what_a_test_file_is() -> None:
    """Three copies of this had already drifted apart."""
    assert is_test_path("tests/unit/test_x.py")
    assert is_test_path("src/__tests__/App.spec.tsx")
    assert is_test_path("app/user_test.go")
    assert is_test_path("spec/models/user.rb")
    assert not is_test_path("daino/context/compiler.py")
    assert not is_test_path("src/latest/index.ts")


def test_the_graph_of_an_empty_index_is_empty_rather_than_broken() -> None:
    """Every caller degrades to what it did before, rather than failing."""
    graph = ImportGraph.build(repository_index({}))

    assert graph.imports == {}
    assert graph.neighbours("anything.py") == []
    assert graph.siblings("anything.py") == set()
    assert graph.defines("anything") == set()


def test_a_language_with_no_import_extraction_simply_has_no_edges() -> None:
    """Go, Rust, Java and HTML land on whatever the caller did before."""
    graph = ImportGraph.build(repository_index({"main.go": [], "util.go": []}))

    assert graph.imports == {}
    assert graph.paths == {"main.go", "util.go"}
