"""Polished CLI surface for Daino's core engine."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.tree import Tree
from sqlalchemy import select

from daino import __version__, branding
from daino.agents import ReviewerAgent
from daino.application import initialize_project
from daino.config import (
    config_path,
    find_project_root,
    load_settings,
    save_settings,
    set_value,
)
from daino.config.globals import save_global
from daino.config.models import ModelProfileConfig, ProviderConfig
from daino.deployment import DeploymentManager
from daino.git import GitClient
from daino.infra.manager import InfrastructureManager
from daino.memory import MemoryManager, MemoryScope, MemoryType
from daino.missions import EvidenceExporter, MissionService
from daino.model_router import ModelRole
from daino.observability import AuditLog, collect_stats
from daino.persistence import Database
from daino.persistence.models import Approval, Checkpoint, Mission, Provider, Task
from daino.playbooks import PlaybookLoader
from daino.repository import RepositoryIndexer
from daino.runtimes import LocalRuntime
from daino.schemas import Message, MissionStatus, ProjectMode, RequirementSpec
from daino.security import PolicyEngine
from daino.utils.ids import new_id
from daino.verification import VerificationEngine
from daino.workspace import Workspace, WorkspaceManager

console = Console()
app = typer.Typer(
    name="daino",
    help="Local-first autonomous software engineering control plane.",
    no_args_is_help=False,
    invoke_without_command=True,
)
_active_project: Path | None = None
config_app = typer.Typer(help="Inspect and update project configuration.")
providers_app = typer.Typer(help="Manage OpenAI-compatible providers.")
models_app = typer.Typer(help="Inspect, test, and route model profiles.")
repo_app = typer.Typer(help="Index and query repository intelligence.")
missions_app = typer.Typer(help="Inspect and control durable missions.")
memory_app = typer.Typer(help="Inspect and control local durable memory.")
checkpoints_app = typer.Typer(help="Create and restore workspace checkpoints.")
deploy_app = typer.Typer(help="Inspect and deploy versioned Docker Compose releases.")
playbooks_app = typer.Typer(help="Discover and run versioned engineering playbooks.")
infra_app = typer.Typer(help="Validate and apply Terraform/OpenTofu projects.")
app.add_typer(config_app, name="config")
app.add_typer(providers_app, name="providers")
app.add_typer(models_app, name="models")
app.add_typer(repo_app, name="repo")
app.add_typer(missions_app, name="missions")
app.add_typer(memory_app, name="memory")
app.add_typer(checkpoints_app, name="checkpoints")
app.add_typer(deploy_app, name="deploy")
app.add_typer(playbooks_app, name="playbooks")
app.add_typer(infra_app, name="infra")


def _root() -> Path:
    return find_project_root(_active_project)


def _context(*, require: bool = True) -> tuple[Path, Any, Database]:
    root = _root()
    settings = load_settings(root, require=require)
    database = Database(settings, root)
    database.initialize()
    return root, settings, database


def _json(data: Any) -> None:
    console.print_json(json.dumps(data, default=str))


def _mission_panel(mission: Mission, tasks: list[Task] | None = None) -> Panel:
    task_lines = []
    icons = {
        "completed": "[green]✓[/green]",
        "running": "[yellow]→[/yellow]",
        "failed": "[red]✗[/red]",
        "blocked": "[red]■[/red]",
    }
    for task in tasks or []:
        task_lines.append(f"{icons.get(task.status, '○')} {task.title} [{task.status}]")
    body = (
        f"[bold]Status:[/bold] {mission.status}\n"
        f"[bold]Mode:[/bold] {mission.mode}\n"
        f"[bold]Workspace:[/bold] {mission.workspace_path or 'not created'}\n"
        f"[bold]Branch:[/bold] {mission.branch or 'not created'}"
    )
    if task_lines:
        body += "\n\n" + "\n".join(task_lines)
    if mission.failure:
        body += f"\n\n[red]Failure:[/red] {mission.failure}"
    return Panel(body, title=f"Mission: {mission.id}")


# Root-level subcommands and sub-Typers. A leading CLI token matching one of
# these is dispatched as a command; anything else (``daino .``) is treated as a
# project path so the bare invocation opens a workspace.
KNOWN_SUBCOMMANDS = frozenset(
    {
        "tui",
        "init",
        "doctor",
        "ask",
        "plan",
        "build",
        "run",
        "test",
        "review",
        "stats",
        "logs",
        "config",
        "providers",
        "models",
        "repo",
        "missions",
        "memory",
        "checkpoints",
        "deploy",
        "playbooks",
        "infra",
        "ps",
        "kill",
    }
)


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Show the installed version.")] = False,
    project: Annotated[
        Path | None,
        typer.Option("--project", help="Repository to open or operate on."),
    ] = None,
    gui: Annotated[
        bool, typer.Option("--gui", help="Launch the local browser IDE instead of the TUI.")
    ] = False,
    tui: Annotated[
        bool, typer.Option("--tui", help="Launch the terminal UI workspace.")
    ] = False,
    host: Annotated[
        str, typer.Option("--host", help="Host to bind the GUI server to.")
    ] = "127.0.0.1",
    port: Annotated[
        int, typer.Option("--port", help="Port for the GUI server (0 picks a free port).")
    ] = 4173,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Do not open a browser for --gui.")
    ] = False,
    foreground: Annotated[
        bool,
        typer.Option(
            "--foreground",
            "-f",
            help="Keep the GUI server attached to this terminal (default is background).",
        ),
    ] = False,
    serve: Annotated[
        bool,
        typer.Option("--serve", hidden=True, help="Internal: run the detached GUI server."),
    ] = False,
) -> None:
    """Open the interactive workspace, or run an automation-friendly subcommand."""
    global _active_project
    _active_project = project.resolve() if project else None
    # run_tui installs crash handling after resolving the exact launch folder.
    # Installing it here first would let parent-repository discovery pin the
    # one-shot crash log to the wrong project for a bare `daino` invocation.
    if ctx.invoked_subcommand not in {None, "tui"}:
        _install_crash_handling(_active_project)
    if version:
        console.print(f"daino {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        if gui:
            if serve:
                # The detached server process the background launcher spawned.
                from daino.server.launch import serve_gui

                serve_gui(_active_project, host=host, port=port)
                return
            if foreground:
                from daino.server.launch import run_gui

                run_gui(_active_project, host=host, port=port, open_browser=not no_browser)
                return
            # Default: detach so the terminal stays free (daino ps / daino kill).
            from daino.server.launch import launch_gui_background

            launch_gui_background(
                _active_project, host=host, port=port, open_browser=not no_browser
            )
            return
        if tui:
            from daino.tui import run_tui

            run_tui(_active_project)
            return
        # No interactive flag and no subcommand: show the command list, so `daino`
        # is discoverable. Launch an interface explicitly with `--tui` or `--gui`.
        console.print(ctx.get_help())


def _rewrite_leading_path(argv: list[str]) -> list[str]:
    """Turn a leading project-path token into ``--project <path>``.

    A Click group cannot carry a positional argument without shadowing its
    subcommand names, so ``daino .`` / ``daino ./app --gui`` are normalized here
    before Typer parses them. A leading option or a known subcommand is left
    untouched, so ``daino config show`` and ``daino --gui`` still work.
    """
    if not argv:
        return argv
    first = argv[0]
    if first.startswith("-") or first in KNOWN_SUBCOMMANDS:
        return argv
    return ["--project", first, *argv[1:]]


def run_cli() -> None:
    """Console-script entry point for ``daino`` (normalizes a leading path)."""
    import sys

    sys.argv = [sys.argv[0], *_rewrite_leading_path(sys.argv[1:])]
    app()


def legacy_main() -> None:
    """Entry point for the deprecated ``vasuki`` console script.

    The command was renamed to ``daino``; this shim prints a one-time notice and
    then delegates to the same Typer application so existing scripts keep working.
    """
    console.print(
        "[yellow]The `vasuki` command has been renamed to `daino`.[/yellow]\n"
        "Please use `daino` instead."
    )
    run_cli()


@app.command("ps")
def gui_ps() -> None:
    """List the D[Ai]NO GUI servers running in the background."""
    from datetime import datetime

    from daino.server.launch import list_servers

    servers = list_servers()
    if not servers:
        console.print(f"No {branding.NAME} GUI servers are running.")
        return

    def parse(iso: str) -> datetime | None:
        try:
            return datetime.fromisoformat(iso)
        except ValueError:
            return None

    def when(dt: datetime | None) -> str:
        if dt is None:
            return "—"
        return dt.astimezone().strftime("%b %d %H:%M")

    def uptime(dt: datetime | None) -> str:
        if dt is None:
            return "—"
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        secs = max(0, int((now - dt).total_seconds()))
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m"
        if secs < 86400:
            return f"{secs // 3600}h {secs % 3600 // 60}m"
        return f"{secs // 86400}d {secs % 86400 // 3600}h"

    table = Table(title=f"{branding.NAME} GUI servers", box=None, pad_edge=False)
    table.add_column("SESSION", style="bold")
    table.add_column("URL")
    table.add_column("PROJECT")
    table.add_column("PID", justify="right")
    table.add_column("STARTED")
    table.add_column("UPTIME", justify="right")
    for server in servers:
        started = parse(str(server.get("started", "")))
        table.add_row(
            str(server.get("id", "?")),
            str(server.get("url", "")),
            str(server.get("dir", "")),
            str(server.get("pid", "")),
            when(started),
            uptime(started),
        )
    console.print(table)


@app.command("kill")
def gui_kill(
    target: Annotated[
        str | None,
        typer.Argument(
            help="Session id or project directory. Defaults to the current directory.",
        ),
    ] = None,
) -> None:
    """Stop a background D[Ai]NO GUI server (by session id or directory)."""
    from daino.server.launch import kill_server

    killed = kill_server(target)
    if killed:
        console.print(
            f"Stopped {branding.NAME} GUI session "
            f"[bold]{killed['id']}[/bold] ({killed['url']})."
        )
    else:
        where = target or "the current directory"
        console.print(f"[yellow]No running {branding.NAME} GUI server matched {where}.[/yellow]")


@app.command("tui")
def tui_command(
    project: Annotated[
        Path | None,
        typer.Option("--project", help="Repository to open."),
    ] = None,
) -> None:
    """Open the persistent interactive terminal workspace."""
    from daino.tui import run_tui

    run_tui(project or _active_project)


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Repository directory.")] = Path("."),
    force: Annotated[
        bool, typer.Option("--force", help=f"Replace an existing {branding.NAME} configuration.")
    ] = False,
) -> None:
    """Initialize a repository, database, runtime inventory, and repository index."""
    root = path.resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = config_path(root)
    if target.exists() and not force:
        console.print(f"[yellow]Already initialized:[/yellow] {target}")
        raise typer.Exit(1)
    result = initialize_project(root, force=force)
    languages = result["languages"]
    frameworks = result["frameworks"]
    runtimes = result["runtimes"]
    console.print(
        Panel(
            f"Project: [bold]{root.name}[/bold]\n"
            f"Root: {root}\n"
            f"Indexed files: {result['files']}\n"
            f"Languages: {', '.join(languages) or 'none'}\n"
            f"Frameworks: {', '.join(frameworks) or 'none'}\n"
            f"Runtimes: "
            f"{', '.join(name for name, present in runtimes.items() if present)}",
            title=f"{branding.NAME} initialized",
            border_style="green",
        )
    )


@app.command()
def doctor(
    fix_terminal: Annotated[
        bool,
        typer.Option("--fix-terminal", help="Repair a terminal left broken by a crash."),
    ] = False,
) -> None:
    """Check the host, configuration, provider references, and runtime prerequisites.

    ``--fix-terminal`` undoes what a crashed full-screen session leaves behind. A
    native crash skips the TUI's cleanup, so the terminal stays in
    alternate-screen and mouse-reporting mode and every mouse movement arrives at
    the shell as text like ``35;72;10M``. It cannot be undone from inside the
    process that died, so it is offered here.
    """
    from daino.utils import crashlog

    if fix_terminal:
        crashlog.restore_terminal()
        console.print("[green]Terminal restored.[/green]")
        return
    root = _root()
    checks: list[tuple[str, bool, str]] = []
    try:
        settings = load_settings(root)
        checks.append(("Configuration", True, str(config_path(root))))
        database = Database(settings, root)
        database.initialize()
        checks.append(("Database", True, settings.database.url))
    except Exception as exc:
        settings = None
        checks.append(("Configuration/database", False, str(exc)))
    for executable, required in (
        ("git", True),
        ("docker", settings is not None and settings.runtime.default == "docker"),
        ("ssh", False),
        ("rg", False),
        ("terraform", False),
        ("tofu", False),
    ):
        location = shutil.which(executable)
        checks.append(
            (
                executable,
                bool(location) or not required,
                location or ("required but missing" if required else "optional, not installed"),
            )
        )
    table = Table("Check", "Status", "Detail")
    for name, passed, detail in checks:
        table.add_row(name, "[green]pass[/green]" if passed else "[red]fail[/red]", detail)
    console.print(table)
    if not all(item[1] for item in checks):
        raise typer.Exit(1)


@config_app.command("show")
def config_show() -> None:
    """Show validated configuration; only secret references are displayed."""
    _, settings, _ = _context()
    console.print(Syntax(yaml.safe_dump(settings.safe_dump(), sort_keys=False), "yaml"))


@config_app.command("set")
def config_set(
    key: Annotated[str, typer.Argument(help="Dotted configuration key.")],
    value: Annotated[str, typer.Argument(help="YAML-formatted value.")],
) -> None:
    """Set and validate one configuration value."""
    settings = set_value(_root(), key, value)
    console.print(f"[green]Updated[/green] {key} = {value}")
    del settings


@config_app.command("validate")
def config_validate() -> None:
    """Validate the complete configuration."""
    _, settings, _ = _context()
    console.print(
        f"[green]Valid[/green]: {len(settings.providers)} providers, "
        f"{len(settings.models)} model profiles"
    )


@providers_app.command("list")
def providers_list() -> None:
    _, settings, _ = _context()
    table = Table("Name", "Type", "Base URL", "Model", "Secret reference")
    for name, provider in settings.providers.items():
        table.add_row(
            name, provider.type, provider.base_url, provider.model, provider.api_key or "none"
        )
    console.print(table)


@providers_app.command("add")
def providers_add(
    name: Annotated[str, typer.Argument(help="Stable provider name.")],
    provider_type: Annotated[
        str, typer.Option("--type", help="openrouter, ollama, vllm, or openai-compatible")
    ],
    base_url: Annotated[str, typer.Option("--base-url")],
    model: Annotated[str, typer.Option("--model")],
    api_key: Annotated[
        str,
        typer.Option(
            "--api-key-ref",
            help="Secret reference such as env://OPENROUTER_API_KEY; never the value.",
        ),
    ] = "",
    local: Annotated[bool, typer.Option("--local")] = False,
) -> None:
    """Add a provider and matching model profile without storing a secret value."""
    root, settings, database = _context()
    provider = ProviderConfig(
        type=provider_type,
        base_url=base_url,
        model=model,
        api_key=api_key,
        timeout=300 if provider_type in {"ollama", "vllm"} else 120,
    )
    settings.providers[name] = provider
    settings.models[name] = ModelProfileConfig(
        provider=name, model=model, local=local or provider_type in {"ollama", "vllm"}
    )
    for role in ModelRole:
        settings.routing.setdefault(role.value, name)
    # A provider is a user-level fact, so it is written globally as well as to
    # this project; the next directory then needs no configuration at all.
    save_global(settings)
    save_settings(settings, root)
    with database.session() as session:
        existing = session.scalar(select(Provider).where(Provider.name == name))
        if existing:
            existing.type = provider.type
            existing.base_url = provider.base_url
            existing.api_key_reference = provider.api_key
            existing.config = provider.model_dump(mode="json")
        else:
            session.add(
                Provider(
                    id=new_id("provider"),
                    name=name,
                    type=provider.type,
                    base_url=provider.base_url,
                    api_key_reference=provider.api_key,
                    config=provider.model_dump(mode="json"),
                )
            )
    console.print(f"[green]Added provider[/green] {name}")


async def _provider_health(name: str) -> dict[str, object]:
    from daino.providers import create_provider

    _, settings, _ = _context()
    if name not in settings.providers:
        raise typer.BadParameter(f"Unknown provider {name}")
    provider = create_provider(name, settings.providers[name])
    try:
        return await provider.health_check()
    finally:
        await provider.close()


@providers_app.command("test")
def providers_test(name: Annotated[str, typer.Argument()]) -> None:
    result = asyncio.run(_provider_health(name))
    _json(result)
    if not result.get("healthy"):
        raise typer.Exit(1)


@providers_app.command("remove")
def providers_remove(name: Annotated[str, typer.Argument()]) -> None:
    root, settings, database = _context()
    if name not in settings.providers:
        raise typer.BadParameter(f"Unknown provider {name}")
    del settings.providers[name]
    remove_profiles = [
        profile for profile, config in settings.models.items() if config.provider == name
    ]
    for profile in remove_profiles:
        del settings.models[profile]
    settings.routing = {
        role: profile
        for role, profile in settings.routing.items()
        if profile not in remove_profiles
    }
    save_settings(settings, root)
    with database.session() as session:
        stored = session.scalar(select(Provider).where(Provider.name == name))
        if stored:
            session.delete(stored)
    console.print(f"[yellow]Removed provider[/yellow] {name}")


@models_app.command("list")
def models_list() -> None:
    _, settings, _ = _context()
    routed: dict[str, list[str]] = {}
    for role, profile in settings.routing.items():
        routed.setdefault(profile, []).append(role)
    table = Table("Profile", "Provider", "Model", "Local", "Context", "Role")
    for name, profile in settings.models.items():
        table.add_row(
            name,
            profile.provider,
            profile.model,
            str(profile.local),
            str(profile.context_window),
            ", ".join(sorted(routed.get(name, []))),
        )
    console.print(table)


@models_app.command("test")
def models_test(profile: Annotated[str, typer.Argument()]) -> None:
    _, settings, _ = _context()
    if profile not in settings.models:
        raise typer.BadParameter(f"Unknown model profile {profile}")
    asyncio.run(_model_test(profile))


async def _model_test(profile: str) -> None:
    from daino.providers import create_provider

    _, settings, _ = _context()
    model = settings.models[profile]
    provider_config = settings.providers[model.provider].model_copy(update={"model": model.model})
    provider = create_provider(model.provider, provider_config)
    try:
        response = await provider.complete(
            [Message(role="user", content="Reply with exactly: ok")], max_tokens=16
        )
        if not response.content.strip():
            console.print(
                "[red]The provider returned no visible content.[/red] "
                "Try a larger output limit or another model; some reasoning models "
                "consume a very small allowance before emitting their answer."
            )
            raise typer.Exit(1)
        console.print(
            f"[green]Response[/green] {response.content.strip()} ({response.latency_ms:.0f} ms)"
        )
    finally:
        await provider.close()


@models_app.command("route")
def models_route(
    role: Annotated[ModelRole, typer.Argument()],
    profile: Annotated[str, typer.Argument()],
    fallback: Annotated[list[str] | None, typer.Option("--fallback")] = None,
) -> None:
    root, settings, _ = _context()
    if profile not in settings.models:
        raise typer.BadParameter(f"Unknown profile {profile}")
    settings.routing[role.value] = profile
    if fallback is not None:
        unknown = set(fallback) - settings.models.keys()
        if unknown:
            raise typer.BadParameter(f"Unknown fallback profiles: {sorted(unknown)}")
        settings.routing_fallbacks[role.value] = fallback
    save_settings(settings, root)
    console.print(f"[green]{role.value}[/green] → {profile}")


@repo_app.command("index")
def repo_index() -> None:
    root, _, _ = _context()
    index = RepositoryIndexer(root).build()
    console.print(f"[green]Indexed[/green] {len(index.files)} files")


@repo_app.command("status")
def repo_status() -> None:
    root, _, _ = _context()
    indexer = RepositoryIndexer(root)
    index = indexer.load()
    console.print(indexer.summary(include_files=False))
    console.print(f"Last indexed: {index.generated_at.isoformat()}")


@repo_app.command("map")
def repo_map() -> None:
    root, _, _ = _context()
    index = RepositoryIndexer(root).load()
    tree = Tree(f"[bold]{root.name}[/bold]")
    nodes: dict[str, Tree] = {"": tree}
    for item in index.files:
        parts = Path(item.path).parts
        parent = ""
        for part in parts[:-1]:
            key = f"{parent}/{part}" if parent else part
            if key not in nodes:
                nodes[key] = nodes[parent].add(f"[blue]{part}/[/blue]")
            parent = key
        nodes[parent].add(f"{parts[-1]} [dim]({item.language})[/dim]")
    console.print(tree)


@repo_app.command("symbols")
def repo_symbols(query: Annotated[str | None, typer.Argument()] = None) -> None:
    index = RepositoryIndexer(_root()).load()
    table = Table("Symbol", "Kind", "File", "Line")
    for item in index.files:
        for symbol in item.symbols:
            if query is None or query.lower() in symbol.name.lower():
                table.add_row(symbol.name, symbol.kind, symbol.path, str(symbol.line))
    console.print(table)


@repo_app.command("references")
def repo_references(symbol: Annotated[str, typer.Argument()]) -> None:
    _json(RepositoryIndexer(_root()).find_references(symbol))


@repo_app.command("routes")
def repo_routes() -> None:
    _json(RepositoryIndexer(_root()).api_routes())


@repo_app.command("databases")
def repo_databases() -> None:
    _json([item.model_dump(mode="json") for item in RepositoryIndexer(_root()).database_models()])


@repo_app.command("tests")
def repo_tests() -> None:
    _json(RepositoryIndexer(_root()).tests())


@repo_app.command("dependencies")
def repo_dependencies() -> None:
    _json(RepositoryIndexer(_root()).dependencies())


@app.command()
def ask(question: Annotated[str, typer.Argument()]) -> None:
    """Ask a configured model about a compact repository summary."""
    asyncio.run(_ask(question))


async def _ask(question: str) -> None:
    root, settings, database = _context()
    service = MissionService(root, settings, database)
    mission = service.create(question, ProjectMode.DIRECT)
    role = (
        ModelRole.SUMMARIZER
        if service._role_available(ModelRole.SUMMARIZER)
        else ModelRole.ARCHITECT
    )
    indexer = RepositoryIndexer(root)
    response = await service.gateway.complete(
        mission.id,
        role,
        [
            Message(
                role="system",
                content="Answer using only the supplied repository map. State uncertainty clearly.",
            ),
            Message(role="user", content=f"{question}\n\n{indexer.summary()}"),
        ],
    )
    console.print(Panel(response.content, title=f"{response.provider}/{response.model}"))


def _parse_mode(mode: str | None) -> ProjectMode | None:
    return ProjectMode(mode) if mode else None


@app.command()
def plan(
    request: Annotated[str, typer.Argument()],
    mode: Annotated[str | None, typer.Option("--mode")] = None,
) -> None:
    """Compile requirements and persist a dependency-aware task plan."""
    asyncio.run(_plan(request, _parse_mode(mode)))


async def _plan(request: str, mode: ProjectMode | None) -> None:
    root, settings, database = _context()
    mission, requirements, task_plan = await MissionService(root, settings, database).plan(
        request, mode
    )
    console.print(_mission_panel(mission))
    console.print(Panel(requirements.problem_statement, title="Requirements"))
    table = Table("Task", "Risk", "Dependencies", "Verification")
    for task in task_plan.tasks:
        table.add_row(
            task.title,
            task.risk_level,
            ", ".join(task.dependencies) or "none",
            ", ".join(task.verification_commands) or "auto-detect",
        )
    console.print(table)


@app.command()
def build(
    request: Annotated[str, typer.Argument()],
    mode: Annotated[str | None, typer.Option("--mode")] = None,
) -> None:
    """Implement and verify a change in an isolated mission workspace."""
    asyncio.run(_run_mission(request, _parse_mode(mode)))


@app.command()
def run(
    request: Annotated[str, typer.Argument()],
    mode: Annotated[str | None, typer.Option("--mode")] = None,
    non_interactive: Annotated[
        bool,
        typer.Option("--non-interactive", help="Never prompt; suitable for automation."),
    ] = False,
) -> None:
    """Run planning, implementation, verification, review, commit, and evidence export."""
    del non_interactive
    asyncio.run(_run_mission(request, _parse_mode(mode)))


async def _run_mission(request: str, mode: ProjectMode | None) -> None:
    root, settings, database = _context()
    service = MissionService(root, settings, database)
    with console.status(f"Running {branding.NAME} mission…", spinner="dots"):
        mission, evidence = await service.run(request, mode)
    with database.session() as session:
        tasks = session.scalars(select(Task).where(Task.mission_id == mission.id)).all()
    console.print(_mission_panel(mission, list(tasks)))
    if evidence:
        console.print(f"Evidence: [link={evidence.as_uri()}]{evidence}[/link]")


@app.command("test")
def test_command(
    command: Annotated[list[str] | None, typer.Argument()] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit a structured verification report.")
    ] = False,
) -> None:
    """Run project verification through the configured runtime."""
    asyncio.run(_test(command or [], json_output=json_output))


async def _test(commands: list[str], *, json_output: bool = False) -> None:
    root, settings, database = _context()
    runtime = MissionService(root, settings, database)._runtime(root)
    await runtime.prepare()
    try:
        report = await VerificationEngine(root, runtime).run(commands or None)
    finally:
        await runtime.cleanup()
    if json_output:
        _json(report.model_dump(mode="json"))
    else:
        table = Table("Command", "Status", "Duration")
        for check in report.checks:
            table.add_row(
                check.command,
                "[yellow]skip[/yellow]"
                if check.skipped
                else "[green]pass[/green]"
                if check.passed
                else "[red]fail[/red]",
                f"{check.result.duration_seconds:.2f}s" if check.result else "—",
            )
        console.print(table)
    if not report.passed:
        if not json_output:
            _json([item.model_dump(mode="json") for item in report.failures])
        raise typer.Exit(1)


@app.command()
def review() -> None:
    """Run an independent model review of the current Git diff."""
    asyncio.run(_review_current())


async def _review_current() -> None:
    root, settings, database = _context()
    service = MissionService(root, settings, database)
    mission = service.create("Review current Git changes", ProjectMode.DIRECT)
    diff = GitClient(root).diff()
    requirements = RequirementSpec(
        problem_statement="Review current uncommitted changes",
        goals=["Identify correctness, security, compatibility, and test gaps"],
        functional_requirements=["Review the supplied diff"],
        acceptance_criteria=["No blocking finding remains"],
        test_strategy=["Use the supplied verification evidence"],
    )
    report = await ReviewerAgent(service.gateway).review(
        mission.id, requirements, requirements.acceptance_criteria, diff, "Not supplied"
    )
    _json(report.model_dump(mode="json"))
    if not report.approved:
        raise typer.Exit(1)


@missions_app.command("list")
def missions_list(
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit structured mission records.")
    ] = False,
) -> None:
    _, _, database = _context()
    with database.session() as session:
        missions = session.scalars(select(Mission).order_by(Mission.created_at.desc())).all()
    if json_output:
        _json(
            [
                {
                    "id": mission.id,
                    "status": mission.status,
                    "mode": mission.mode,
                    "request": mission.request,
                    "created_at": mission.created_at,
                    "updated_at": mission.updated_at,
                    "branch": mission.branch,
                    "workspace_path": mission.workspace_path,
                }
                for mission in missions
            ]
        )
        return
    table = Table("ID", "Status", "Mode", "Request", "Created")
    for mission in missions:
        table.add_row(
            mission.id,
            mission.status,
            mission.mode,
            mission.request[:60],
            mission.created_at.isoformat(timespec="seconds"),
        )
    console.print(table)


@missions_app.command("show")
def missions_show(
    mission_id: Annotated[str, typer.Argument()],
    diff: Annotated[bool, typer.Option("--diff")] = False,
) -> None:
    root, _, database = _context()
    with database.session() as session:
        mission = session.get(Mission, mission_id)
        if not mission:
            raise typer.BadParameter("Unknown mission")
        tasks = session.scalars(select(Task).where(Task.mission_id == mission_id)).all()
        session.expunge(mission)
    console.print(_mission_panel(mission, list(tasks)))
    if diff and mission.workspace_path and mission.initial_revision:
        patch = GitClient(Path(mission.workspace_path)).diff(mission.initial_revision)
        console.print(Syntax(patch, "diff"))


@missions_app.command("resume")
def missions_resume(mission_id: Annotated[str, typer.Argument()]) -> None:
    asyncio.run(_resume(mission_id))


async def _resume(mission_id: str) -> None:
    root, settings, database = _context()
    service = MissionService(root, settings, database)
    mission = service.get(mission_id)
    if mission.status == MissionStatus.AWAITING_APPROVAL.value:
        completed, evidence = await service.execute(mission_id)
    else:
        completed, evidence = await service.resume(mission_id)
    console.print(_mission_panel(completed))
    if evidence:
        console.print(str(evidence))


@missions_app.command("cancel")
def missions_cancel(mission_id: Annotated[str, typer.Argument()]) -> None:
    _, _, database = _context()
    with database.session() as session:
        mission = session.get(Mission, mission_id)
        if not mission:
            raise typer.BadParameter("Unknown mission")
        if mission.status == MissionStatus.COMPLETED.value:
            raise typer.BadParameter("A completed mission cannot be cancelled")
        mission.status = MissionStatus.CANCELLED.value
        for task in session.scalars(select(Task).where(Task.mission_id == mission_id)):
            if task.status != "completed":
                task.status = "cancelled"
    console.print(f"[yellow]Cancelled[/yellow] {mission_id}")


@missions_app.command("retry")
def missions_retry(mission_id: Annotated[str, typer.Argument()]) -> None:
    """Retry a failed mission as a new isolated mission, preserving the failed evidence."""
    root, settings, database = _context()
    original = MissionService(root, settings, database).get(mission_id)
    if original.status not in {MissionStatus.FAILED.value, MissionStatus.BLOCKED.value}:
        raise typer.BadParameter("Only failed or blocked missions can be retried")
    asyncio.run(_run_mission(original.request, ProjectMode(original.mode)))


@missions_app.command("export")
def missions_export(
    mission_id: Annotated[str, typer.Argument()],
    format: Annotated[str, typer.Option("--format")] = "markdown",
) -> None:
    root, _, database = _context()
    path = EvidenceExporter(root, database).export(mission_id, format)
    console.print(str(path))


@missions_app.command("approve")
def missions_approve(mission_id: Annotated[str, typer.Argument()]) -> None:
    _, _, database = _context()
    with database.session() as session:
        if session.get(Mission, mission_id) is None:
            raise typer.BadParameter("Unknown mission")
        session.add(
            Approval(
                id=new_id("approval"),
                mission_id=mission_id,
                category="mission_execution",
                subject="Execute persisted mission plan",
                approved=True,
            )
        )
    console.print(
        f"[green]Approved[/green] {mission_id}; run `daino missions resume {mission_id}`"
    )


@missions_app.command("discard")
def missions_discard(
    mission_id: Annotated[str, typer.Argument()],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    root, _, database = _context()
    with database.session() as session:
        mission = session.get(Mission, mission_id)
        if not mission:
            raise typer.BadParameter("Unknown mission")
        session.expunge(mission)
    if not yes and not typer.confirm(f"Delete worktree and branch for {mission_id}?"):
        raise typer.Abort()
    if mission.workspace_path and mission.branch:
        workspace = Workspace(
            mission.id,
            Path(mission.workspace_path),
            mission.branch,
            mission.initial_revision or "HEAD",
            "",
        )
        WorkspaceManager(root).cleanup(workspace, discard=True)
    with database.session() as session:
        stored = session.get(Mission, mission_id)
        if stored:
            stored.status = MissionStatus.CANCELLED.value
    console.print(f"[yellow]Discarded workspace and branch[/yellow] for {mission_id}")


def _memory_manager() -> MemoryManager:
    root, settings, database = _context()
    return MemoryManager(database, root, settings)


def _memory_table(items: list[Any]) -> Table:
    table = Table("ID", "Type", "Scope", "Status", "Memory", "Source", "Confidence")
    for item in items:
        table.add_row(
            item.id,
            item.type.value,
            item.scope.value,
            item.status.value,
            item.summary or item.content,
            item.source,
            f"{item.confidence:.2f}",
        )
    return table


@memory_app.command("list")
def memory_list(
    memory_type: Annotated[str | None, typer.Option("--type")] = None,
    scope: Annotated[str | None, typer.Option("--scope")] = None,
) -> None:
    manager = _memory_manager()
    try:
        items = manager.list(
            memory_type=MemoryType(memory_type) if memory_type else None,
            scope=MemoryScope(scope) if scope else None,
        )
        console.print(_memory_table(items))
    finally:
        manager.close()


@memory_app.command("search")
def memory_search(query: Annotated[str, typer.Argument()]) -> None:
    manager = _memory_manager()
    try:
        console.print(_memory_table(manager.search(query, include_stale=True, debug=True)))
    finally:
        manager.close()


@memory_app.command("forget")
def memory_forget(memory_id: Annotated[str, typer.Argument()]) -> None:
    manager = _memory_manager()
    try:
        manager.forget(memory_id)
        console.print(f"[yellow]Forgot[/yellow] {memory_id}")
    finally:
        manager.close()


@memory_app.command("verify")
def memory_verify(memory_id: Annotated[str, typer.Argument()]) -> None:
    manager = _memory_manager()
    try:
        manager.verify(memory_id)
        console.print(f"[green]Verified[/green] {memory_id}")
    finally:
        manager.close()


@memory_app.command("clear-project")
def memory_clear_project(
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    if not yes and not typer.confirm("Forget all project-scoped memories for this repository?"):
        raise typer.Abort()
    manager = _memory_manager()
    try:
        count = manager.clear(scope=MemoryScope.PROJECT)
        console.print(f"[yellow]Forgot[/yellow] {count} project memory item(s)")
    finally:
        manager.close()


@checkpoints_app.command("list")
def checkpoints_list() -> None:
    _, _, database = _context()
    with database.session() as session:
        items = session.scalars(select(Checkpoint).order_by(Checkpoint.created_at.desc())).all()
    table = Table("ID", "Mission", "Revision", "Archive", "Description")
    for item in items:
        table.add_row(
            item.id,
            item.mission_id or "",
            item.revision or "",
            item.archive_path or "",
            item.description,
        )
    console.print(table)


@checkpoints_app.command("create")
def checkpoints_create(
    description: Annotated[str, typer.Option("--description")] = "Manual checkpoint",
) -> None:
    root, _, database = _context()
    git = GitClient(root)
    workspace = Workspace("manual", root, git.current_branch(), git.revision(), git.status())
    checkpoint_id, path = WorkspaceManager(root).checkpoint(workspace, description)
    with database.session() as session:
        session.add(
            Checkpoint(
                id=checkpoint_id,
                revision=workspace.initial_revision,
                archive_path=str(path),
                description=description,
            )
        )
    console.print(f"[green]Created[/green] {checkpoint_id}: {path}")


@checkpoints_app.command("restore")
def checkpoints_restore(
    checkpoint_id: Annotated[str, typer.Argument()],
    yes: Annotated[bool, typer.Option("--yes")] = False,
) -> None:
    root, _, database = _context()
    with database.session() as session:
        item = session.get(Checkpoint, checkpoint_id)
        if not item or not item.archive_path:
            raise typer.BadParameter("Unknown or unrestorable checkpoint")
        archive = Path(item.archive_path)
    if not yes and not typer.confirm("Overwrite matching workspace files from this checkpoint?"):
        raise typer.Abort()
    WorkspaceManager(root).restore_checkpoint(archive, root)
    console.print(f"[green]Restored[/green] {checkpoint_id}")


def _deployment_manager() -> tuple[DeploymentManager, str | None]:
    root, settings, database = _context()
    return DeploymentManager(root, settings, database), None


@deploy_app.command("inspect")
def deploy_inspect(
    target: Annotated[str, typer.Option("--target")],
    json_output: Annotated[
        bool, typer.Option("--json", help="Emit structured inspection data.")
    ] = False,
) -> None:
    manager, _ = _deployment_manager()
    result = asyncio.run(manager.inspect(target))
    if json_output:
        _json(result)
    else:
        _json(result)


@deploy_app.command("plan")
def deploy_plan(target: Annotated[str, typer.Option("--target")]) -> None:
    manager, _ = _deployment_manager()
    plan_data = asyncio.run(manager.create_plan(target))
    _json(plan_data.model_dump(mode="json"))


@deploy_app.command("apply")
def deploy_apply(
    target: Annotated[str, typer.Option("--target")],
    approve: Annotated[bool, typer.Option("--approve")] = False,
    mission: Annotated[str | None, typer.Option("--mission")] = None,
) -> None:
    manager, _ = _deployment_manager()
    result = asyncio.run(manager.apply(target, approved=approve, mission_id=mission))
    _json({"deployment_id": result.id, "release": result.release_id, "status": result.status})


@deploy_app.command("verify")
def deploy_verify(target: Annotated[str, typer.Option("--target")]) -> None:
    manager, _ = _deployment_manager()
    result = asyncio.run(manager.verify(target))
    _json(result)
    if not result["healthy"]:
        raise typer.Exit(1)


@deploy_app.command("status")
def deploy_status(target: Annotated[str, typer.Option("--target")]) -> None:
    manager, _ = _deployment_manager()
    _json(manager.status(target))


@deploy_app.command("rollback")
def deploy_rollback(
    target: Annotated[str, typer.Option("--target")],
    approve: Annotated[bool, typer.Option("--approve")] = False,
) -> None:
    manager, _ = _deployment_manager()
    _json(asyncio.run(manager.rollback(target, approved=approve)))


@deploy_app.command("logs")
def deploy_logs(target: Annotated[str, typer.Option("--target")]) -> None:
    manager, _ = _deployment_manager()
    _json(asyncio.run(manager.logs(target)))


@playbooks_app.command("list")
def playbooks_list() -> None:
    table = Table("Name", "Version", "Purpose")
    for item in PlaybookLoader(_root()).list():
        table.add_row(item.name, item.version, item.purpose)
    console.print(table)


@playbooks_app.command("show")
def playbooks_show(name: Annotated[str, typer.Argument()]) -> None:
    playbook = PlaybookLoader(_root()).get(name)
    console.print(Syntax(yaml.safe_dump(playbook.model_dump(), sort_keys=False), "yaml"))


@playbooks_app.command("run")
def playbooks_run(
    name: Annotated[str, typer.Argument()],
    request: Annotated[str | None, typer.Option("--request")] = None,
) -> None:
    playbook = PlaybookLoader(_root()).get(name)
    mission_request = request or playbook.purpose
    asyncio.run(_run_mission(mission_request, ProjectMode.SPECIFICATION))


def _infra_runtime() -> tuple[InfrastructureManager, LocalRuntime]:
    root, settings, _ = _context()
    runtime = LocalRuntime(
        root,
        PolicyEngine(settings.security),
        timeout=settings.runtime.command_timeout_seconds,
    )
    return InfrastructureManager(root, runtime), runtime


@infra_app.command("validate")
def infra_validate() -> None:
    manager, _ = _infra_runtime()
    _json(asyncio.run(manager.validate()))


@infra_app.command("plan")
def infra_plan() -> None:
    manager, _ = _infra_runtime()
    _json(asyncio.run(manager.plan()))


@infra_app.command("apply")
def infra_apply(approve: Annotated[bool, typer.Option("--approve")] = False) -> None:
    manager, _ = _infra_runtime()
    _json(asyncio.run(manager.apply(approved=approve)))


@infra_app.command("destroy")
def infra_destroy(
    approve: Annotated[bool, typer.Option("--approve")] = False,
    confirm: Annotated[str, typer.Option("--confirm")] = "",
) -> None:
    manager, _ = _infra_runtime()
    _json(asyncio.run(manager.destroy(approved=approve, confirmation=confirm)))


@app.command()
def stats(mission: Annotated[str | None, typer.Option("--mission")] = None) -> None:
    _, _, database = _context()
    with database.session() as session:
        _json(collect_stats(session, mission))


@app.command()
def logs(
    mission: Annotated[str | None, typer.Option("--mission")] = None,
    limit: Annotated[int, typer.Option("--limit")] = 100,
) -> None:
    root, _, _ = _context()
    events = AuditLog(root).read(mission)
    for event in events[-limit:]:
        console.print(json.dumps(event, ensure_ascii=False))


def _install_crash_handling(project: Path | None) -> None:
    """Record native crashes and leave the terminal usable, for every command.

    Installed in the top-level callback rather than only around the TUI: a crash
    in any invocation still wrecks the terminal, and a crash log that only some
    processes write is a log that never explains the interesting failure.
    """
    from daino.config.loader import find_project_root
    from daino.utils import crashlog

    try:
        root = find_project_root(project)
    except Exception:  # noqa: BLE001 - diagnostics must never stop the command
        root = project or Path.cwd()
    crashlog.install(root)
