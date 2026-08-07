"""Default keybindings and command metadata."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SlashCommand:
    name: str
    description: str
    usage: str = ""


SLASH_COMMANDS = (
    SlashCommand("/help", "Open help and shortcut reference"),
    SlashCommand("/clear", "Clear the visible conversation"),
    SlashCommand("/new", "Start a new conversation session", "[title]"),
    SlashCommand("/ask", "Ask a repository-grounded question", "<question>"),
    SlashCommand("/plan", "Create a persisted mission plan", "<instruction>"),
    SlashCommand("/build", "Plan or execute an approved mission", "[instruction]"),
    SlashCommand("/run", "Plan a complete coding mission", "<instruction>"),
    SlashCommand("/team", "Split work across parallel sub-agents", "<instruction>"),
    SlashCommand("/review", "Review the active mission changes"),
    SlashCommand("/test", "Run verification", "[targeted|failed|full|command]"),
    SlashCommand("/status", "Show active project and mission status"),
    SlashCommand("/missions", "Open the mission browser"),
    SlashCommand("/resume", "Resume or open a mission", "[mission-id]"),
    SlashCommand("/cancel", "Cancel current generation or mission"),
    SlashCommand("/files", "Open the repository file browser", "[query]"),
    SlashCommand("/diff", "Open the Git diff viewer", "[staged]"),
    SlashCommand("/checkpoints", "Open checkpoints"),
    SlashCommand("/checkpoint", "Create a checkpoint", "[description]"),
    SlashCommand("/restore", "Restore a checkpoint after confirmation", "<checkpoint-id>"),
    SlashCommand("/model", "Select a session model", "[profile]"),
    SlashCommand("/provider", "Open providers or test a connection", "[name]"),
    SlashCommand("/runtime", "Switch the session runtime", "[local|docker|ssh]"),
    SlashCommand("/index", "Rebuild repository intelligence"),
    SlashCommand("/playbooks", "Browse engineering playbooks"),
    SlashCommand("/deploy", "Run a deployment operation", "<action> <target>"),
    SlashCommand("/logs", "Open redacted event logs"),
    SlashCommand("/settings", "Open validated project settings"),
    SlashCommand("/bye", "Exit Vasuki safely"),
    SlashCommand("/quit", "Quit Vasuki safely"),
)

COMMAND_PALETTE = (
    ("New mission", "/new"),
    ("Resume mission", "/missions"),
    ("Run repository index", "/index"),
    ("Switch model", "/model"),
    ("Switch provider", "/provider"),
    ("Switch runtime", "/runtime"),
    ("Run a team of sub-agents", "/team"),
    ("Run tests", "/test"),
    ("Open file", "/files"),
    ("Open symbol", "/files @symbol:"),
    ("View diff", "/diff"),
    ("Create checkpoint", "/checkpoint"),
    ("Restore checkpoint", "/checkpoints"),
    ("Inspect deployment", "/deploy inspect"),
    ("Open settings", "/settings"),
    ("Open logs", "/logs"),
    ("Help", "/help"),
    ("Quit", "/bye"),
)

SHORTCUTS = (
    ("Ctrl+P", "Command palette"),
    ("Ctrl+N", "New mission"),
    ("Ctrl+O", "Open files"),
    ("Ctrl+M", "Select model"),
    ("Ctrl+R", "Resume mission"),
    ("Ctrl+T", "Run tests"),
    ("Ctrl+D", "Open diff"),
    ("Ctrl+L", "Open logs"),
    ("Ctrl+I", "Toggle the context strip"),
    ("Enter", "Submit input"),
    ("Shift+Enter", "Insert a newline"),
    ("Esc", "Close modal"),
    ("Ctrl+C", "Cancel active work"),
    ("Ctrl+Q", "Quit"),
    ("?", "Help"),
)
