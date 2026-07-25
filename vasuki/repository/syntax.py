"""Tree-sitter syntax extraction with deterministic fallbacks in the indexer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Language, Node, Parser
from tree_sitter_language_pack import get_parser
from tree_sitter_typescript import language_tsx, language_typescript

from vasuki.schemas import RepositorySymbol

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
    ".cs": "c_sharp",
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


def extract_outline(relative: str, source: bytes) -> SyntaxOutline | None:
    """Extract declarations with the grammar matching the file extension."""
    grammar = GRAMMARS.get(Path(relative).suffix.lower())
    if grammar is None:
        return None
    try:
        if grammar == "typescript":
            parser = Parser(Language(language_typescript()))
        elif grammar == "tsx":
            parser = Parser(Language(language_tsx()))
        else:
            parser = get_parser(grammar)
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
