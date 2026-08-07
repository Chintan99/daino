"""Native tool-calling definitions for the builder/debugger action space.

The iterative agent loop can express each step either as a schema-constrained
``AgentAction`` JSON object or as a native OpenAI-format tool call. Both paths
funnel into the same validated ``AgentAction``, so the executor, scope checks,
and observations are identical regardless of what the model backend supports.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from vasuki.schemas import AgentAction, ToolCall

_THOUGHT = {
    "type": "string",
    "description": "One short sentence explaining why you are taking this step.",
}


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {"thought": _THOUGHT, **properties},
                "required": ["thought", *required],
            },
        },
    }


_PATH = {
    "type": "string",
    "description": "Repository-relative path with no leading ./ or /.",
}

AGENT_TOOL_SPECS: list[dict[str, Any]] = [
    _tool(
        "read_file",
        "Read one repository file. Required before editing an existing file you "
        "were not already shown. For a file too large to take in at once, page "
        "through it with offset and limit.",
        {
            "path": _PATH,
            "offset": {
                "type": "integer",
                "description": "First line to read, 1-based. Omit to start at the top.",
            },
            "limit": {"type": "integer", "description": "How many lines to read."},
        },
        ["path"],
    ),
    _tool(
        "search_text",
        "Search the workspace for a literal substring and return matching locations.",
        {"query": {"type": "string", "description": "The substring to find."}},
        ["query"],
    ),
    _tool(
        "glob",
        "List repository files matching a path pattern, such as src/**/*.py. Use "
        "this to find files by name or extension rather than by content.",
        {"pattern": {"type": "string", "description": "Path pattern; ** spans directories."}},
        ["pattern"],
    ),
    _tool(
        "grep",
        "Search file contents by regular expression. Prefer this over search_text "
        "when you need a pattern rather than an exact substring.",
        {
            "query": {"type": "string", "description": "Python regular expression."},
            "pattern": {
                "type": "string",
                "description": "Optional path pattern to restrict the search, e.g. src/**/*.py.",
            },
        },
        ["query"],
    ),
    _tool(
        "run_command",
        "Run one command in the project workspace and read its output. Use it to "
        "run tests, linters and builds, to install a dependency you need, and to "
        "check that your change actually works. There is no shell: give one "
        "executable and its arguments, with no pipes, redirects, && or globs. "
        "Routine commands run immediately; installs and network access ask the "
        "user first; destructive commands are refused.",
        {
            "command": {
                "type": "string",
                "description": "Executable and arguments, e.g. 'pytest -q' or 'pip install httpx'.",
            },
            "timeout": {
                "type": "integer",
                "description": "Seconds to allow before giving up. Omit for the default.",
            },
        },
        ["command"],
    ),
    _tool(
        "multi_edit",
        "Apply several exact replacements to one file in a single step. Each edit "
        "follows the same rules as replace. If any anchor fails to match, the "
        "remaining edits are not applied, so keep each old_string unique.",
        {
            "path": _PATH,
            "edits": {
                "type": "array",
                "description": "Replacements, applied in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {"type": "string", "description": "Exact text to find."},
                        "new_string": {"type": "string", "description": "Replacement text."},
                        "replace_all": {
                            "type": "boolean",
                            "description": "Replace every occurrence instead of one.",
                        },
                    },
                    "required": ["old_string", "new_string"],
                },
            },
        },
        ["path", "edits"],
    ),
    _tool(
        "todo",
        "Record your plan for work that takes several steps, and update it as you "
        "go. Send the whole list each time, with exactly one item in_progress. "
        "Skip this for a request that is a single edit.",
        {
            "todos": {
                "type": "array",
                "description": "The full plan, in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string", "description": "What this step does."},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                            "description": "Where this step stands.",
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
        ["todos"],
    ),
    _tool(
        "list_directory",
        "List one directory of the repository; path defaults to the repository root.",
        {"path": _PATH},
        [],
    ),
    _tool(
        "replace",
        "Change one exact, unique span of an existing file. old_string must be "
        "copied verbatim, including indentation, and must occur once unless "
        "replace_all is set.",
        {
            "path": _PATH,
            "old_string": {"type": "string", "description": "Exact text to find."},
            "new_string": {"type": "string", "description": "Replacement text."},
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence instead of one.",
            },
        },
        ["path", "old_string", "new_string"],
    ),
    _tool(
        "write",
        "Create a NEW file with complete content. Prefer replace for a file that "
        "already exists: a whole-file rewrite often exceeds the output token limit, "
        "and a reply cut off part way through is discarded and changes nothing.",
        {
            "path": _PATH,
            "content": {"type": "string", "description": "Complete file content."},
        },
        ["path", "content"],
    ),
    _tool("delete", "Delete a single file.", {"path": _PATH}, ["path"]),
    _tool(
        "finish",
        "Stop working. Summarize the change and list the executable verification "
        "commands that prove the task passes.",
        {
            "summary": {"type": "string", "description": "What you changed."},
            "verification_commands": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Commands that verify the task; each is one executable and its "
                    "arguments, never a shell one-liner."
                ),
            },
        },
        ["summary"],
    ),
]

_RESPOND = _tool(
    "respond",
    "Answer the user in prose and stop, without changing any file. Use this when "
    "the request was a question, or once you have gathered what was asked for. "
    "Never use this to show code you intend to write: to change a file, edit it.",
    {
        "message": {
            "type": "string",
            "description": "The complete answer to show the user.",
        }
    },
    ["message"],
)

#: The chat agent's action space. Same grounded tools as the builder plus
#: ``respond``, so one loop can answer a question or carry out an edit and the
#: model chooses which the request called for.
CHAT_TOOL_SPECS: list[dict[str, Any]] = [*AGENT_TOOL_SPECS, _RESPOND]


def tool_call_to_action(call: ToolCall) -> AgentAction:
    """Convert one native tool call into the validated loop action."""
    arguments = {key: value for key, value in call.arguments.items() if key != "action"}
    return AgentAction.model_validate({"action": call.name, **arguments})


def action_arguments_invalid(exc: ValidationError) -> str:
    """Summarize a tool-argument validation failure for the model's observation."""
    problems = []
    for error in exc.errors()[:5]:
        location = ".".join(str(part) for part in error["loc"]) or "arguments"
        problems.append(f"{location}: {error['msg']}")
    return "Invalid tool arguments. " + "; ".join(problems)
