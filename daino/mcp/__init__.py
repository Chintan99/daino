"""Model Context Protocol client: external tool servers, reachable from the agent."""

from daino.mcp.client import (
    HTTPMCPClient,
    MCPClient,
    MCPError,
    StdioMCPClient,
    build_client,
    render_content,
)
from daino.mcp.loader import (
    MCP_FILENAME,
    LoadedServers,
    global_mcp_path,
    load_servers,
    project_mcp_path,
)
from daino.mcp.models import MCPServerConfig, MCPTool, qualify, unqualify
from daino.mcp.registry import UNTRUSTED_BANNER, MCPRegistry, ServerStatus

__all__ = [
    "MCP_FILENAME",
    "UNTRUSTED_BANNER",
    "HTTPMCPClient",
    "LoadedServers",
    "MCPClient",
    "MCPError",
    "MCPRegistry",
    "MCPServerConfig",
    "MCPTool",
    "ServerStatus",
    "StdioMCPClient",
    "build_client",
    "global_mcp_path",
    "load_servers",
    "project_mcp_path",
    "qualify",
    "render_content",
    "unqualify",
]
