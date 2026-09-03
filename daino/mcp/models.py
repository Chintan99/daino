"""What an MCP server is, and what it offers.

Model Context Protocol servers are how the rest of the tool ecosystem is
reachable: a Postgres server, a Sentry server, a company's internal API server.
Daino had none of it, which meant every integration was a feature request rather
than a config line.

Two transports cover essentially all of it. ``stdio`` launches a local process
and speaks newline-delimited JSON-RPC to it — the common case, and the one that
needs no network. ``http`` posts JSON-RPC to a URL, for a server someone else is
running.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

#: Protocol revision this client implements. Sent at initialize; a server that
#: speaks something else answers with its own version and we proceed anyway,
#: because the three methods used here have been stable across revisions.
PROTOCOL_VERSION = "2025-06-18"

#: Separator in the flattened tool name. Two underscores, matching the
#: convention MCP clients have converged on, so a tool a user has seen named
#: ``mcp__github__create_issue`` elsewhere is named that here too.
NAME_SEPARATOR = "__"
NAME_PREFIX = "mcp"


class MCPServerConfig(BaseModel):
    """One configured server."""

    transport: Literal["stdio", "http"] = "stdio"
    #: stdio: the executable to launch.
    command: str = ""
    args: list[str] = Field(default_factory=list)
    #: Extra environment for a stdio server. Values may be ``env://NAME``
    #: references, resolved the same way a provider key is, so a token for an
    #: MCP server never has to be written into a file in the checkout.
    env: dict[str, str] = Field(default_factory=dict)
    #: http: the endpoint that accepts JSON-RPC POSTs.
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    #: Seconds to wait for one request, and for the handshake.
    timeout: float = Field(default=30.0, gt=0, le=600)
    enabled: bool = True
    #: Tools to expose, by their server-side name. Empty means all of them.
    #: A server with sixty tools would otherwise spend a large part of the
    #: model's context describing tools this project will never call.
    allowed_tools: list[str] = Field(default_factory=list)
    #: Tools to withhold, applied after ``allowed_tools``.
    denied_tools: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def require_transport_target(self) -> MCPServerConfig:
        if self.transport == "stdio" and not self.command:
            raise ValueError("a stdio MCP server needs a command")
        if self.transport == "http" and not self.url:
            raise ValueError("an http MCP server needs a url")
        return self

    def exposes(self, tool_name: str) -> bool:
        if self.allowed_tools and tool_name not in self.allowed_tools:
            return False
        return tool_name not in self.denied_tools


class MCPTool(BaseModel):
    """One tool a connected server advertises."""

    server: str
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)

    @property
    def qualified_name(self) -> str:
        """The name the model sees, namespaced so two servers cannot collide."""
        return qualify(self.server, self.name)

    def to_openai_spec(self) -> dict[str, Any]:
        """The tool as an OpenAI-format function definition.

        The server's own JSON Schema is passed through rather than rewritten.
        Providers that reject an unusual schema will say so, and a schema Daino
        had "helpfully" simplified would describe a tool that does not exist.
        """
        schema = dict(self.input_schema or {})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        return {
            "type": "function",
            "function": {
                "name": self.qualified_name,
                "description": (
                    self.description
                    or f"The {self.name} tool provided by the {self.server} MCP server."
                ),
                "parameters": schema,
            },
        }


def qualify(server: str, tool: str) -> str:
    return f"{NAME_PREFIX}{NAME_SEPARATOR}{server}{NAME_SEPARATOR}{tool}"


def unqualify(qualified: str) -> tuple[str, str]:
    """Split a namespaced name back into ``(server, tool)``.

    Returns ``("", "")`` for anything that is not an MCP name, so a caller can
    use this as the membership test as well as the parser. A tool name may itself
    contain the separator, so the split is bounded to two pieces from the left.
    """
    prefix = f"{NAME_PREFIX}{NAME_SEPARATOR}"
    if not qualified.startswith(prefix):
        return ("", "")
    remainder = qualified[len(prefix) :]
    server, separator, tool = remainder.partition(NAME_SEPARATOR)
    if not separator or not server or not tool:
        return ("", "")
    return (server, tool)
