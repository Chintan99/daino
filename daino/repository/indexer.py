"""Incremental repository index with AST and syntax-aware symbol extraction."""

from __future__ import annotations

import ast
import hashlib
import os
import re
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from daino.config import paths
from daino.repository.graph import is_test_path
from daino.repository.languages import IGNORED_DIRS, language_for
from daino.repository.syntax import extract_outlines
from daino.schemas.core import RepositoryFile, RepositoryIndex, RepositorySymbol

MAX_INDEX_FILE_BYTES = 1_000_000

#: Bounds on the walk. A repository is not thousands of levels deep; a path that
#: deep means a link loop or a mistakenly-wide root, and following it is what
#: overflowed the C stack. The file cap keeps an accidental root — a home
#: directory, say — from turning startup into a filesystem crawl.
MAX_INDEX_DEPTH = 40
MAX_INDEX_FILES = 20_000


def _empty_index(root: Path) -> RepositoryIndex:
    """A valid, empty index for a project that has not been indexed yet."""
    return RepositoryIndex(root=str(root), generated_at=datetime.now(UTC), files=[], languages={})


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _python_outline(path: str, text: str) -> tuple[list[RepositorySymbol], list[str]]:
    symbols: list[RepositorySymbol] = []
    imports: list[str] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return symbols, imports
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            arguments = [arg.arg for arg in node.args.args]
            symbols.append(
                RepositorySymbol(
                    name=node.name,
                    kind=kind,
                    path=path,
                    line=node.lineno,
                    signature=f"{node.name}({', '.join(arguments)})",
                )
            )
        elif isinstance(node, ast.ClassDef):
            symbols.append(
                RepositorySymbol(name=node.name, kind="class", path=path, line=node.lineno)
            )
        elif isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return symbols, sorted(set(imports))


DECLARATION_PATTERN = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?"
    r"(?:(class|interface|type|enum|function)\s+([A-Za-z_$][\w$]*)|"
    r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\()",
    re.MULTILINE,
)
IMPORT_PATTERN = re.compile(r"(?:from\s+|require\()['\"]([^'\"]+)|import\s+['\"]([^'\"]+)")


def _javascript_outline(path: str, text: str) -> tuple[list[RepositorySymbol], list[str]]:
    symbols: list[RepositorySymbol] = []
    for match in DECLARATION_PATTERN.finditer(text):
        kind = match.group(1) or "function"
        name = match.group(2) or match.group(3)
        symbols.append(
            RepositorySymbol(
                name=name,
                kind=kind,
                path=path,
                line=text.count("\n", 0, match.start()) + 1,
            )
        )
    imports = sorted({left or right for left, right in IMPORT_PATTERN.findall(text)})
    return symbols, imports


def _generic_outline(path: str, text: str) -> tuple[list[RepositorySymbol], list[str]]:
    symbols: list[RepositorySymbol] = []
    pattern = re.compile(
        r"^\s*(?:pub\s+|public\s+|private\s+|protected\s+)?"
        r"(?:class|struct|trait|interface|func|fn|def)\s+([A-Za-z_]\w*)",
        re.MULTILINE,
    )
    for match in pattern.finditer(text):
        symbols.append(
            RepositorySymbol(
                name=match.group(1),
                kind="declaration",
                path=path,
                line=text.count("\n", 0, match.start()) + 1,
            )
        )
    return symbols, []


def _summarize(path: str, language: str, text: str, symbols: list[RepositorySymbol]) -> str:
    first_doc = next((line.strip("#/ *") for line in text.splitlines() if line.strip()), "")
    symbol_names = ", ".join(symbol.name for symbol in symbols[:8])
    summary = f"{language} file"
    if first_doc:
        summary += f": {first_doc[:180]}"
    if symbol_names:
        summary += f". Defines {symbol_names}"
    return summary


class RepositoryIndexer:
    """Builds and persists a compact, incrementally reusable index."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.index_path = paths.state_dir(self.root) / "repository-index.json"

    def _load_existing(self) -> dict[str, RepositoryFile]:
        if not self.index_path.exists():
            return {}
        try:
            index = RepositoryIndex.model_validate_json(self.index_path.read_text(encoding="utf-8"))
            return {item.path: item for item in index.files}
        except (OSError, ValueError):
            return {}

    def _walk(self) -> Iterator[tuple[Path, PurePosixPath]]:
        """Yield indexable files, pruning as it descends.

        ``rglob("*")`` was wrong in three ways that together crashed the process.
        It descends into every ignored directory before anything filters them, so
        a tree containing ``node_modules`` or ``.venv`` costs a full traversal.
        It follows directory symlinks, so a link pointing at an ancestor — common
        in a home directory — recurses without end. And the paths it produces
        then went through ``Path.relative_to``, which recurses per component:
        once the walk was deep enough that call overflowed the C stack and
        segfaulted, with no traceback and no way for the user to tell why.

        Walking explicitly fixes all three: ignored directories are pruned before
        being entered, symlinks are never followed, depth is bounded, and the
        relative path is accumulated as we go instead of being recomputed.
        """
        stack: list[tuple[Path, PurePosixPath, int]] = [(self.root, PurePosixPath(), 0)]
        seen = 0
        while stack:
            directory, relative, depth = stack.pop()
            if depth > MAX_INDEX_DEPTH:
                continue
            try:
                entries = sorted(os.scandir(directory), key=lambda item: item.name)
            except OSError:
                continue
            for entry in entries:
                # Never follow a symlink, in either direction: a link to an
                # ancestor is an infinite tree, and a link to a file outside the
                # repository is not part of it.
                if entry.is_symlink() or entry.name in IGNORED_DIRS:
                    continue
                child = relative / entry.name
                try:
                    if entry.is_dir():
                        stack.append((Path(entry.path), child, depth + 1))
                    elif entry.is_file():
                        seen += 1
                        if seen > MAX_INDEX_FILES:
                            return
                        yield Path(entry.path), child
                except OSError:
                    continue

    def build(self) -> RepositoryIndex:
        previous = self._load_existing()
        files: list[RepositoryFile] = []
        languages: Counter[str] = Counter()
        frameworks: set[str] = set()
        entrypoints: list[str] = []
        # Tree-sitter runs in a child process for the whole build (see
        # daino.repository.syntax.extract_outlines): the grammars are native
        # code that can take the process down with a signal, and indexing must
        # not be able to kill the server. Done as one batch up front rather than
        # per file so the cost is one subprocess, not thousands.
        outlines = self._syntax_outlines(previous)
        for path, relative_path in self._walk():
            if path.stat().st_size > MAX_INDEX_FILE_BYTES:
                continue
            relative = relative_path.as_posix()
            try:
                data = path.read_bytes()
                if b"\0" in data[:8192]:
                    continue
                text = data.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            digest = _digest(data)
            existing = previous.get(relative)
            if existing and existing.digest == digest:
                item = existing
            else:
                language = language_for(path)
                if language == "Python":
                    symbols, imports = _python_outline(relative, text)
                else:
                    parsed = outlines.get(relative)
                    if language in {"JavaScript", "TypeScript"}:
                        fallback_symbols, imports = _javascript_outline(relative, text)
                    else:
                        fallback_symbols, imports = _generic_outline(relative, text)
                    # A file the parser never reached falls back to the regex
                    # outline. "No symbols" and "never parsed" are different
                    # facts, and only the second one wants a fallback.
                    symbols = parsed if parsed else fallback_symbols
                item = RepositoryFile(
                    path=relative,
                    language=language,
                    size=len(data),
                    digest=digest,
                    summary=_summarize(relative, language, text, symbols),
                    imports=imports,
                    symbols=symbols,
                )
            files.append(item)
            languages[item.language] += 1
            lower = text.lower()
            framework_markers = {
                "FastAPI": "fastapi",
                "Django": "django",
                "Flask": "flask",
                "React": "react",
                "Vue": "vue",
                "Next.js": "next",
                "SQLAlchemy": "sqlalchemy",
                "Terraform": "terraform",
            }
            frameworks.update(name for name, marker in framework_markers.items() if marker in lower)
            if path.name in {
                "main.py",
                "app.py",
                "manage.py",
                "package.json",
                "Dockerfile",
                "compose.yaml",
                "docker-compose.yml",
            }:
                entrypoints.append(relative)
        index = RepositoryIndex(
            root=str(self.root),
            generated_at=datetime.now(UTC),
            files=files,
            languages=dict(languages),
            frameworks=sorted(frameworks),
            entrypoints=entrypoints,
        )
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(index.model_dump_json(indent=2), encoding="utf-8")
        return index

    def _syntax_outlines(
        self, previous: dict[str, RepositoryFile]
    ) -> dict[str, list[RepositorySymbol]]:
        """Parse every changed file that has a grammar, in one isolated batch.

        Files whose digest is unchanged are skipped: their symbols are already
        in the previous index, and re-parsing them would make an incremental
        build cost the same as a first one.
        """
        candidates: list[str] = []
        for path, relative_path in self._walk():
            relative = relative_path.as_posix()
            if language_for(path) == "Python":
                continue
            try:
                if path.stat().st_size > MAX_INDEX_FILE_BYTES:
                    continue
                existing = previous.get(relative)
                if existing and existing.digest == _digest(path.read_bytes()):
                    continue
            except OSError:
                continue
            candidates.append(relative)
        if not candidates:
            return {}
        batch = extract_outlines(self.root, candidates)
        return batch.outlines

    def load(self) -> RepositoryIndex:
        """Return the cached index, or an empty one when nothing is cached.

        Deliberately does not build. Building is a full filesystem walk, and
        hiding it behind a read meant a view mounting at startup could kick off
        an index of the entire project — or, when the root resolved somewhere
        unintended, of a whole home directory. Indexing happens where the user
        asked for it: ``daino init``, ``/index``, and mission execution.
        """
        if not self.index_path.exists():
            return _empty_index(self.root)
        try:
            return RepositoryIndex.model_validate_json(self.index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A truncated or half-written index must not be fatal; it is a cache.
            return _empty_index(self.root)

    def find_symbol(self, name: str) -> list[RepositorySymbol]:
        return [
            symbol for file in self.load().files for symbol in file.symbols if symbol.name == name
        ]

    def find_references(self, symbol: str) -> list[dict[str, int | str]]:
        results: list[dict[str, int | str]] = []
        pattern = re.compile(rf"\b{re.escape(symbol)}\b")
        for file in self.load().files:
            path = self.root / file.path
            try:
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                    if pattern.search(line):
                        results.append({"path": file.path, "line": number, "text": line.strip()})
            except OSError:
                continue
        return results

    def api_routes(self) -> list[dict[str, str | int]]:
        route_pattern = re.compile(
            r"@(?:app|router|blueprint)\.(get|post|put|patch|delete)\([\"']([^\"']+)"
        )
        results: list[dict[str, str | int]] = []
        for file in self.load().files:
            path = self.root / file.path
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for match in route_pattern.finditer(text):
                results.append(
                    {
                        "method": match.group(1).upper(),
                        "route": match.group(2),
                        "path": file.path,
                        "line": text.count("\n", 0, match.start()) + 1,
                    }
                )
        return results

    def database_models(self) -> list[RepositorySymbol]:
        return [
            symbol
            for file in self.load().files
            for symbol in file.symbols
            if symbol.kind == "class"
            and any(marker in file.summary for marker in ("Base", "Model", "SQLAlchemy"))
        ]

    def tests(self) -> list[str]:
        # One shared predicate: this and the context compiler used to classify
        # test files slightly differently, so a file could be a test to one and
        # source to the other and land in the wrong half of a bundle.
        return [item.path for item in self.load().files if is_test_path(item.path)]

    def dependencies(self) -> dict[str, list[str]]:
        return {item.path: item.imports for item in self.load().files if item.imports}

    def environment_variables(self) -> list[dict[str, str | int]]:
        pattern = re.compile(
            r"(?:os\.(?:getenv|environ\.get)|process\.env\.)(?:\(?[\"']?)([A-Z][A-Z0-9_]*)"
        )
        results: list[dict[str, str | int]] = []
        for file in self.load().files:
            path = self.root / file.path
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for match in pattern.finditer(text):
                results.append(
                    {
                        "name": match.group(1),
                        "path": file.path,
                        "line": text.count("\n", 0, match.start()) + 1,
                    }
                )
        return results

    def docker_services(self) -> list[str]:
        services: list[str] = []
        for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"):
            path = self.root / name
            if not path.exists():
                continue
            try:
                import yaml

                raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                services.extend((raw.get("services") or {}).keys())
            except (OSError, ValueError):
                continue
        return sorted(set(services))

    def inventory(self, limit: int = 200) -> str:
        """List the repository's files with their symbols.

        Planning and question answering both depend on this: an agent told only
        that a repository has "144 indexed files" cannot target any of them, so
        it invents new filenames instead of editing what is already there.
        """
        index = self.load()
        if not index.files:
            return "No files indexed. Run /index to build the repository index."
        ranked = sorted(index.files, key=lambda item: (item.path.count("/"), item.path))
        lines: list[str] = []
        for item in ranked[:limit]:
            names = ", ".join(symbol.name for symbol in item.symbols[:8])
            detail = f"  [{names}]" if names else ""
            lines.append(f"{item.path} ({item.language}, {item.size}B){detail}")
        if len(ranked) > limit:
            lines.append(f"… and {len(ranked) - limit} more files")
        return "\n".join(lines)

    def summary(self, *, include_files: bool = True, file_limit: int = 200) -> str:
        index = self.load()
        top = ", ".join(f"{name}: {count}" for name, count in sorted(index.languages.items()))
        overview = (
            f"{len(index.files)} indexed files. Languages: {top or 'none'}. "
            f"Frameworks: {', '.join(index.frameworks) or 'none detected'}. "
            f"Entrypoints: {', '.join(index.entrypoints) or 'none detected'}."
        )
        if not include_files:
            return overview
        return f"{overview}\n\nExisting files:\n{self.inventory(file_limit)}"
