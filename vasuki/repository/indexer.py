"""Incremental repository index with AST and syntax-aware symbol extraction."""

from __future__ import annotations

import ast
import hashlib
import re
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from vasuki.repository.languages import IGNORED_DIRS, language_for
from vasuki.repository.syntax import extract_outline
from vasuki.schemas.core import RepositoryFile, RepositoryIndex, RepositorySymbol

MAX_INDEX_FILE_BYTES = 1_000_000


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
        self.index_path = self.root / ".vasuki" / "repository-index.json"

    def _load_existing(self) -> dict[str, RepositoryFile]:
        if not self.index_path.exists():
            return {}
        try:
            index = RepositoryIndex.model_validate_json(self.index_path.read_text(encoding="utf-8"))
            return {item.path: item for item in index.files}
        except (OSError, ValueError):
            return {}

    def build(self) -> RepositoryIndex:
        previous = self._load_existing()
        files: list[RepositoryFile] = []
        languages: Counter[str] = Counter()
        frameworks: set[str] = set()
        entrypoints: list[str] = []
        for path in sorted(self.root.rglob("*")):
            relative_path = path.relative_to(self.root)
            if not path.is_file() or any(part in IGNORED_DIRS for part in relative_path.parts):
                continue
            if path.is_symlink() or path.stat().st_size > MAX_INDEX_FILE_BYTES:
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
                    syntax = extract_outline(relative, data)
                    if language in {"JavaScript", "TypeScript"}:
                        fallback_symbols, imports = _javascript_outline(relative, text)
                    else:
                        fallback_symbols, imports = _generic_outline(relative, text)
                    symbols = syntax.symbols if syntax and syntax.symbols else fallback_symbols
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

    def load(self) -> RepositoryIndex:
        if not self.index_path.exists():
            return self.build()
        return RepositoryIndex.model_validate_json(self.index_path.read_text(encoding="utf-8"))

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
        return [
            item.path
            for item in self.load().files
            if "test" in Path(item.path).name.lower() or "tests" in Path(item.path).parts
        ]

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

    def summary(self) -> str:
        index = self.load()
        top = ", ".join(f"{name}: {count}" for name, count in sorted(index.languages.items()))
        return (
            f"{len(index.files)} indexed files. Languages: {top or 'none'}. "
            f"Frameworks: {', '.join(index.frameworks) or 'none detected'}. "
            f"Entrypoints: {', '.join(index.entrypoints) or 'none detected'}."
        )
