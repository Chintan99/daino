"""Tree-sitter syntax extraction with deterministic fallbacks in the indexer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Language, Node, Parser
from tree_sitter_language_pack import get_parser
from tree_sitter_typescript import language_tsx, language_typescript

from vasuki.schemas import RepositorySymbol

#: Extensions we can parse with a grammar that ships with the language pack.
#:
#: Every entry here must be bundled. ``get_parser`` falls back to downloading a
#: grammar it does not have and loading the result into this process, which
#: means a network call during indexing and a binary of unknown provenance
#: dlopen'd into the interpreter — a mismatched one corrupts the heap and takes
#: the process down with no traceback. ``.cs`` was mapped to ``c_sharp``, which
#: is not bundled under that name and triggered exactly that path; C# now falls
#: back to the regex outline instead.
GRAMMARS = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
}

DECLARATIONS = {
    "class_declaration": "class",
    "class_definition": "class",
    "function_declaration": "function",
    "function_definition": "function",
    "method_declaration": "method",
    "method_definition": "method",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
    "struct_item": "struct",
    "function_item": "function",
    "impl_item": "implementation",
}


@dataclass(frozen=True)
class SyntaxOutline:
    symbols: list[RepositorySymbol]
    parser: str


def _name(node: Node, source: bytes) -> str | None:
    name_node = node.child_by_field_name("name")
    if name_node is None and node.type == "impl_item":
        name_node = node.child_by_field_name("type")
    if name_node is None:
        return None
    return source[name_node.start_byte : name_node.end_byte].decode("utf-8", errors="replace")


#: Grammar name -> parser, or None once we know a grammar is unusable. Loading a
#: grammar is expensive and a failing one fails every time, so an index build
#: over thousands of files must not retry it per file.
_PARSERS: dict[str, Parser | None] = {}


def _parser_for(grammar: str) -> Parser | None:
    if grammar in _PARSERS:
        return _PARSERS[grammar]
    parser: Parser | None
    try:
        if grammar == "typescript":
            parser = Parser(Language(language_typescript()))
        elif grammar == "tsx":
            parser = Parser(Language(language_tsx()))
        else:
            parser = get_parser(grammar)
    except Exception:
        parser = None
    _PARSERS[grammar] = parser
    return parser


def extract_outline(relative: str, source: bytes) -> SyntaxOutline | None:
    """Extract declarations with the grammar matching the file extension."""
    grammar = GRAMMARS.get(Path(relative).suffix.lower())
    if grammar is None:
        return None
    try:
        parser = _parser_for(grammar)
        if parser is None:
            return None
        tree = parser.parse(source)
    except Exception:
        return None
    symbols: list[RepositorySymbol] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        kind = DECLARATIONS.get(node.type)
        if kind:
            name = _name(node, source)
            if name:
                symbols.append(
                    RepositorySymbol(
                        name=name,
                        kind=kind,
                        path=relative,
                        line=node.start_point.row + 1,
                    )
                )
        stack.extend(reversed(node.children))
    return SyntaxOutline(symbols=symbols, parser=f"tree-sitter:{grammar}")
