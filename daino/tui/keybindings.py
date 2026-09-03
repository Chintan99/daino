"""Default keybindings and command metadata."""

from __future__ import annotations

from dataclasses import dataclass

from daino import branding


@dataclass(frozen=True, slots=True)
class SlashCommand:
    name: str
    description: str
    usage: str = ""


SLASH_COMMANDS = (
    SlashCommand("/help", "Open help and shortcut reference"),
    SlashCommand("/clear", "Clear the visible conversation"),
    SlashCommand("/new", "Start a new conversation session", "[title]"),
    SlashCommand("/mode", "Set agent autonomy", "[plan|ask|session|full]"),
    SlashCommand("/ask", "Ask a repository-grounded question", "<question>"),
    SlashCommand("/plan", "Create a persisted mission plan", "<instruction>"),
    SlashCommand("/build", "Plan or execute an approved mission", "[instruction]"),
    SlashCommand("/run", "Plan a complete coding mission", "<instruction>"),
    SlashCommand("/team", "Split work across parallel sub-agents", "<instruction>"),
    SlashCommand("/review", "Review the active mission changes"),
    SlashCommand("/test", "Run verification", "[targeted|failed|full|command]"),
    SlashCommand("/qa", "Open or run comprehensive project QA", "[run]"),
    SlashCommand("/status", "Show active project and mission status"),
    SlashCommand("/missions", "Open the mission browser"),
    SlashCommand("/tasks", "List crash-safe unfinished tasks"),
    SlashCommand(
        "/memory",
        "Inspect or manage durable memory",
        "[search|project|decisions|failures|user|forget|verify]",
    ),
    SlashCommand("/resume", "Resume or open a mission", "[mission-id]"),
    SlashCommand("/cancel", "Cancel current generation or mission"),
    SlashCommand("/files", "Open the repository file browser", "[query]"),
    SlashCommand("/diff", "Open the Git diff viewer", "[staged]"),
    SlashCommand("/checkpoints", "Open checkpoints"),
    SlashCommand("/checkpoint", "Create a checkpoint", "[description]"),
    SlashCommand("/restore", "Restore a checkpoint after confirmation", "<checkpoint-id>"),
    SlashCommand("/model", "Select a session model", "[profile]"),
    SlashCommand(
        "/effort",
        "Set session reasoning effort",
        "[auto|none|minimal|low|medium|high|xhigh|max]",
    ),
    SlashCommand("/verbose", "Show or hide detailed live progress", "[on|off]"),
    SlashCommand("/provider", "Open providers or test a connection", "[name]"),
    SlashCommand("/globalprovider", "Configure providers shared by every project"),
    SlashCommand("/runtime", "Switch the session runtime", "[local|sandbox|docker|ssh]"),
    SlashCommand("/index", "Rebuild repository intelligence"),
    SlashCommand("/playbooks", "Browse engineering playbooks"),
    SlashCommand("/deploy", "Run a deployment operation", "<action> <target>"),
    SlashCommand("/logs", "Open redacted event logs"),
    SlashCommand("/map", "Open the prompt execution map"),
    SlashCommand("/settings", "Open validated project settings"),
    SlashCommand("/bye", f"Exit {branding.NAME} safely"),
    SlashCommand("/quit", f"Quit {branding.NAME} safely"),
)

COMMAND_PALETTE = (
    ("New mission", "/new"),
    ("Plan mode", "/mode plan"),
    ("Ask approval mode", "/mode ask"),
    ("Approve for session", "/mode session"),
    ("Full access mode", "/mode full"),
    ("Resume mission", "/missions"),
    ("Inspect memory", "/memory"),
    ("List unfinished tasks", "/tasks"),
    ("Run repository index", "/index"),
    ("Switch model", "/model"),
    ("Set reasoning effort", "/effort"),
    ("Toggle verbose progress", "/verbose on"),
    ("Switch provider", "/provider"),
    ("Configure global provider", "/globalprovider"),
    ("Switch runtime", "/runtime"),
    ("Run a team of sub-agents", "/team"),
    ("Run tests", "/test"),
    ("Run comprehensive QA", "/qa run"),
    ("Open file", "/files"),
    ("Open symbol", "/files @symbol:"),
    ("View diff", "/diff"),
    ("Create checkpoint", "/checkpoint"),
    ("Restore checkpoint", "/checkpoints"),
    ("Inspect deployment", "/deploy inspect"),
    ("Open settings", "/settings"),
    ("Open logs", "/logs"),
    ("Open prompt map", "/map"),
    ("Help", "/help"),
    ("Quit", "/bye"),
)

SHORTCUTS = (
    ("Ctrl+P", "Command palette"),
    ("Shift+Tab", "Cycle Plan / Ask / Session / Full mode"),
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
