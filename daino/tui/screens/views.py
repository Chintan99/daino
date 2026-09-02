"""Engineering workspace views backed by application services."""

from __future__ import annotations

import json
from pathlib import Path
from time import monotonic
from typing import Any

import yaml
from rich.markup import escape
from rich.syntax import Syntax
from textual import work
from textual.app import ComposeResult
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    Markdown,
    RichLog,
    Select,
    Static,
    Tree,
)

from daino import branding
from daino.application import (
    CheckpointApplicationService,
    DeploymentApplicationService,
    ExecutionMapApplicationService,
    MissionApplicationService,
    ProviderApplicationService,
    RepositoryApplicationService,
    SettingsApplicationService,
    severity_counts,
)
from daino.application.view_models import (
    ExecutionTrace,
    ExecutionTraceStep,
    OpenRouterModel,
    ProviderStatus,
)
from daino.events import MissionEvent
from daino.observability import AuditLog
from daino.playbooks import PlaybookLoader
from daino.schemas import ChangeReview, QAReport
from daino.security import redact
from daino.tui.highlight import highlight_unified_diff
from daino.tui.keybindings import SHORTCUTS, SLASH_COMMANDS


class ViewPanel(Vertical):
    title = ""

    def compose(self) -> ComposeResult:
        yield Label(self.title, classes="view-title")


class MissionsView(ViewPanel):
    title = "Missions"

    def __init__(self, service: MissionApplicationService) -> None:
        super().__init__(id="missions-view")
        self.service = service

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield DataTable(id="missions-table", cursor_type="row")
        yield Static("Select a mission to inspect its complete evidence.", id="mission-detail")

    def on_mount(self) -> None:
        table = self.query_one("#missions-table", DataTable)
        table.add_columns("ID", "Title", "Status", "Mode", "Updated")
        self.refresh_data()

    def refresh_data(self) -> None:
        table = self.query_one("#missions-table", DataTable)
        table.clear()
        for item in self.service.list_missions():
            table.add_row(
                item.id,
                item.title[:48],
                item.status,
                item.mode,
                item.updated_at.strftime("%Y-%m-%d %H:%M"),
                key=item.id,
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "missions-table":
            return
        mission_id = str(event.row_key.value)
        details = self.service.mission_details(mission_id)
        mission = details["mission"]
        tasks = details["tasks"]
        icons = {"completed": "✓", "running": "→"}
        task_text = "\n".join(
            f"{icons.get(task['status'], '○')} {task['title']} [{task['status']}]" for task in tasks
        )
        self.query_one("#mission-detail", Static).update(
            Content.assemble(
                (str(mission["request"]), "bold"),
                "\n"
                f"Status: {mission['status']}  •  "
                f"Branch: {mission['branch'] or 'not created'}\n"
                f"Workspace: {mission['workspace_path'] or 'not created'}\n\n",
                ("Tasks\n", "bold"),
                task_text or "No tasks persisted",
                f"\n\nApprovals: {len(details['approvals'])}  •  "
                f"Test runs: {len(details['tests'])}  •  "
                f"Reviews: {len(details['reviews'])}",
            )
        )
        screen = self.screen
        if hasattr(screen, "set_active_mission"):
            screen.set_active_mission(mission_id)


#: The release gate, rendered for a terminal that has no banner to put it in.
_VERDICTS = {
    "pass": "[green]SAFE TO PUSH[/]",
    "warn": "[yellow]REVIEW BEFORE PUSH[/]",
    "blocked": "[red]DO NOT PUSH[/]",
    "unknown": "[dim]NO VERDICT[/]",
}


#: The merge gate. Same shape as the push gate above, different decision.
_MERGE_VERDICTS = {
    "pass": "[green]READY TO MERGE[/]",
    "warn": "[yellow]NEEDS A LOOK[/]",
    "blocked": "[red]DO NOT MERGE[/]",
    "unknown": "[dim]NO VERDICT[/]",
}


class InspectorView(ViewPanel):
    """The Inspector: scan the repository, and review one change.

    Two subjects, one panel. A scan asks whether the repository is sound before
    a push; a review asks whether the change in front of you is sound before it
    lands. They sit together because they answer the same question at different
    scopes, and the terminal has no room to make that two screens.
    """

    title = "Inspector"

    #: Scopes offered for a review, in the order they are usually wanted.
    SCOPES: tuple[tuple[str, str], ...] = (
        ("Working tree — everything uncommitted", "working"),
        ("Staged — as it would be committed", "staged"),
        ("Branch — against its base", "branch"),
    )

    def __init__(self) -> None:
        super().__init__(id="inspector-view")

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="inspector-scroll"):
            yield Label(self.title, classes="view-title")

            # Both actions sit at the top together: the panel has two subjects
            # and burying one of them under the other's tables would make it
            # the half nobody uses.
            with Horizontal(classes="toolbar"):
                yield Button("Run scan", id="run-qa", variant="primary")
                yield Button("Review change", id="run-review", variant="primary")
                yield Select(
                    self.SCOPES,
                    value="working",
                    allow_blank=False,
                    id="review-scope",
                )
                yield Button("Refresh scans", id="refresh-qa-history")
            yield Static(
                "Parallel read-only reviewers + tests, Playwright, and dependency audits",
                id="qa-state",
            )
            yield Static(
                "Syntax, credentials, conflict markers, debugging left in, test gaps, "
                "then reviewers read the diff.",
                id="review-state",
            )

            # --- change review ------------------------------------------------
            yield Label("Change review — files", classes="section-title")
            yield DataTable(id="review-files", cursor_type="row")
            yield Label("Change review — findings", classes="section-title")
            yield DataTable(id="review-findings", cursor_type="row")
            yield Label("The review", classes="section-title")
            yield Markdown(
                "No review yet. Select Review change to read the change in front of you.",
                id="review-report",
            )

            # --- repository scan ----------------------------------------------
            yield Label("Saved scans — select a row to load", classes="section-title")
            yield Static("No saved scans for this repository.", id="qa-history-state")
            yield DataTable(id="qa-history", cursor_type="row")
            yield Label("Specialists", classes="section-title")
            yield DataTable(id="qa-specialists", cursor_type="row")
            yield Label("Automated evidence", classes="section-title")
            yield DataTable(id="qa-checks", cursor_type="row")
            yield Label("Consolidated report", classes="section-title")
            yield Markdown(
                "No scan report yet. Select Run scan to inspect this project.", id="qa-report"
            )

    def on_mount(self) -> None:
        self.query_one("#review-files", DataTable).add_columns(
            "File", "Change", "Added", "Removed", "Findings"
        )
        self.query_one("#review-findings", DataTable).add_columns("Severity", "Finding", "Location")
        self.query_one("#qa-history", DataTable).add_columns(
            "Started", "Status", "Profile", "Failures", "Report"
        )
        self.query_one("#qa-specialists", DataTable).add_columns(
            "Specialist", "Role", "Status", "Result"
        )
        self.query_one("#qa-checks", DataTable).add_columns("Check", "Category", "Status", "Result")

    def review_scope(self) -> str:
        """The scope the user has selected for a review."""
        value = self.query_one("#review-scope", Select).value
        return str(value) if value is not None else "working"

    def set_history(self, reports: list[QAReport]) -> None:
        table = self.query_one("#qa-history", DataTable)
        table.clear()
        for report in reports:
            failures = sum(item.status == "failed" for item in report.checks)
            failures += sum(item.status == "failed" for item in report.specialists)
            table.add_row(
                report.started_at.strftime("%Y-%m-%d %H:%M"),
                report.status.replace("_", " ").upper(),
                ", ".join(report.project_profile) or "general",
                str(failures),
                report.id,
                key=report.id,
            )
        self.query_one("#qa-history-state", Static).update(
            f"{len(reports)} saved scan(s) for this repository."
            if reports
            else "No saved scans for this repository."
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "qa-history":
            return
        screen = self.screen
        if hasattr(screen, "load_qa_report"):
            screen.load_qa_report(str(event.row_key.value))

    def show_review(self, review: ChangeReview) -> None:
        """Render one change review: its verdict, its files, its findings."""
        self.query_one("#run-review", Button).disabled = review.status == "running"
        counts = severity_counts(review.findings)
        tally = ", ".join(f"{count} {level}" for level, count in counts.items() if count)
        self.query_one("#review-state", Static).update(
            f"{_MERGE_VERDICTS[review.verdict]}  •  {review.subject}  •  "
            f"{len(review.files)} file(s)  •  [green]+{review.insertions}[/] "
            f"[red]-{review.deletions}[/]  •  {tally or 'no findings'}"
        )

        files = self.query_one("#review-files", DataTable)
        files.clear()
        for item in review.files:
            files.add_row(
                item.path,
                item.kind.upper()[:1],
                f"+{item.insertions}",
                f"-{item.deletions}",
                str(item.findings) if item.findings else "—",
                key=item.path,
            )

        findings = self.query_one("#review-findings", DataTable)
        findings.clear()
        for finding in review.findings:
            where = finding.location or "change"
            if finding.line:
                where = f"{where}:{finding.line}"
            findings.add_row(
                finding.severity.upper(),
                finding.title[:90],
                where,
                key=finding.id,
            )

        self.query_one("#review-report", Markdown).update(review.summary or "Reading the change…")

    def show_report(self, report: QAReport) -> None:
        self.query_one("#run-qa", Button).disabled = report.status == "running"
        failed = sum(item.status == "failed" for item in report.checks)
        skipped = sum(item.status == "skipped" for item in report.checks)
        # The verdict leads: it is the one line that decides whether to push.
        counts = severity_counts(report.findings)
        tally = ", ".join(f"{count} {level}" for level, count in counts.items() if count)
        self.query_one("#qa-state", Static).update(
            f"{_VERDICTS[report.verdict]}  •  {report.id}  •  "
            f"{report.started_at.strftime('%Y-%m-%d %H:%M')}  •  "
            f"{report.status.replace('_', ' ').title()}  •  "
            f"{', '.join(report.project_profile)}  •  {tally or 'no findings'}  •  "
            f"{failed} failed  •  {skipped} skipped"
        )
        specialists = self.query_one("#qa-specialists", DataTable)
        specialists.clear()
        for specialist in report.specialists:
            result = specialist.error or _first_line(specialist.summary) or "—"
            specialists.add_row(
                specialist.label,
                specialist.role,
                "DONE" if specialist.status == "passed" else specialist.status.upper(),
                result[:100],
                key=specialist.id,
            )
        checks = self.query_one("#qa-checks", DataTable)
        checks.clear()
        for check in report.checks:
            checks.add_row(
                check.label,
                check.category,
                check.status.upper(),
                (_first_line(check.summary) or "—")[:100],
                key=check.id,
            )
        self.query_one("#qa-report", Markdown).update(
            report.summary or "The scan is gathering evidence…"
        )


def _first_line(value: str) -> str:
    return next((line.strip() for line in value.splitlines() if line.strip()), "")


class RepositoryView(ViewPanel):
    title = "Repository intelligence"

    def __init__(self, service: RepositoryApplicationService) -> None:
        super().__init__(id="repository-view")
        self.service = service

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield Static("", id="repository-summary")

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        try:
            item = self.service.intelligence()
            languages = ", ".join(f"{name} ({count})" for name, count in item["languages"].items())
            self.query_one("#repository-summary", Static).update(
                Content.assemble(
                    ("Languages\n", "bold"),
                    f"{languages or 'None detected'}\n\n",
                    ("Frameworks\n", "bold"),
                    f"{', '.join(item['frameworks']) or 'None detected'}\n\n",
                    ("Entry points\n", "bold"),
                    f"{chr(10).join(item['entrypoints']) or 'None detected'}\n\n",
                    ("API routes", "bold"),
                    f" {len(item['routes'])}  •  ",
                    ("Database models", "bold"),
                    f" {len(item['database_models'])}  •  ",
                    ("Tests", "bold"),
                    f" {len(item['tests'])}\n\nIndex generated: {item['generated_at']}",
                )
            )
        except Exception as exc:
            self.query_one("#repository-summary", Static).update(
                Content.assemble(
                    ("Repository index unavailable: ", "bold"),
                    f"{exc}\nRun /index to rebuild it.",
                )
            )


class FilesView(ViewPanel):
    title = "Files"

    def __init__(self, service: RepositoryApplicationService) -> None:
        super().__init__(id="files-view")
        self.service = service
        self.selected_path: str | None = None

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield Input(placeholder="Search files or symbols…", id="file-search")
        with Horizontal(id="file-browser"):
            yield Tree("project", id="file-tree")
            with Vertical(id="file-preview-panel"):
                yield Button("Add/remove from context", id="toggle-file-context")
                yield Static("Select a file", id="file-outline")
                yield VerticalScroll(
                    Static("", id="file-preview"),
                    id="file-preview-scroll",
                )

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self, query: str = "") -> None:
        tree = self.query_one("#file-tree", Tree)
        tree.clear()
        tree.root.set_label(self.service.context.root.name)
        nodes: dict[tuple[str, ...], Any] = {(): tree.root}
        try:
            files = self.service.files(query)
        except Exception as exc:
            tree.root.add_leaf(f"Index unavailable: {exc}")
            return
        for item in files:
            parts = Path(item.path).parts
            prefix: tuple[str, ...] = ()
            for part in parts[:-1]:
                next_prefix = (*prefix, part)
                if next_prefix not in nodes:
                    nodes[next_prefix] = nodes[prefix].add(part, expand=False)
                prefix = next_prefix
            marker = f"{item.status} " if item.status else ""
            nodes[prefix].add_leaf(f"{marker}{parts[-1]}", data=item.path)
        tree.root.expand()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "file-search":
            self.refresh_data(event.value)

    def on_tree_node_selected(self, event: Tree.NodeSelected[Any]) -> None:
        path = event.node.data
        if not isinstance(path, str):
            event.node.toggle()
            return
        self.selected_path = path
        try:
            content, language = self.service.preview(path)
            file_item = next(
                (item for item in self.service.files() if item.path == path),
                None,
            )
            symbols = "\n".join(file_item.symbols) if file_item else ""
            self.query_one("#file-outline", Static).update(
                Content.assemble(
                    (path, "bold"),
                    (f"  {language}", "dim"),
                    f"\n{symbols or 'No symbols detected'}",
                )
            )
            self.query_one("#file-preview", Static).update(
                Syntax(
                    content,
                    language.lower(),
                    line_numbers=True,
                    word_wrap=False,
                    theme="monokai",
                )
            )
        except Exception as exc:
            self.query_one("#file-preview", Static).update(Content(str(exc)))


class DiffView(ViewPanel):
    title = "Changes"

    def __init__(self, service: RepositoryApplicationService) -> None:
        super().__init__(id="changes-view")
        self.service = service
        self.mission_id: str | None = None

    def compose(self) -> ComposeResult:
        yield from super().compose()
        with Horizontal(classes="toolbar"):
            yield Select(
                [("Unstaged / mission", "unstaged"), ("Staged", "staged")],
                value="unstaged",
                id="diff-mode",
            )
            yield Button("Refresh", id="refresh-diff")
        yield Static("", id="diff-stats")
        yield VerticalScroll(Static("", id="diff-content"), id="diff-scroll")

    def refresh_data(self, mission_id: str | None = None) -> None:
        if mission_id:
            self.mission_id = mission_id
        staged = self.query_one("#diff-mode", Select).value == "staged"
        if not self.service.has_git():
            # Nothing is wrong here: a directory that was never initialized has
            # no diff to show. What the agent wrote is still known, so it is
            # listed rather than reported as a Git error.
            self.show_written_files()
            return
        try:
            diff = self.service.diff(
                staged=staged,
                mission_id=self.mission_id,
            )
        except Exception:
            self.show_written_files()
            return
        added = sum(
            1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++")
        )
        removed = sum(
            1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---")
        )
        files = sum(1 for line in diff.splitlines() if line.startswith("diff --git "))
        self.query_one("#diff-stats", Static).update(
            f"{files} files  [b]+{added}[/b]  [b]-{removed}[/b]"
        )
        self.query_one("#diff-content", Static).update(highlight_unified_diff(diff))

    def show_written_files(self) -> None:
        """List the files the agent created or edited, with no Git involved."""
        written = self.service.written_files(self.mission_id)
        if not written:
            self.query_one("#diff-stats", Static).update("0 files")
            self.query_one("#diff-content", Static).update(
                "No files have been created or edited yet."
            )
            return
        lines = [
            f"{item['action']:>10}  {item['path']}"
            + (f"  ({item['lines']} lines)" if item["exists"] else "  (removed)")
            for item in written
        ]
        self.query_one("#diff-stats", Static).update(
            f"{len(written)} file(s) written · no Git repository, showing files instead of a diff"
        )
        self.query_one("#diff-content", Static).update(Content("\n".join(lines)))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "refresh-diff":
            self.refresh_data()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "diff-mode":
            self.refresh_data()


class TestsView(ViewPanel):
    title = "Tests"

    def __init__(self) -> None:
        super().__init__(id="tests-view")

    def compose(self) -> ComposeResult:
        yield from super().compose()
        with Horizontal(classes="toolbar"):
            yield Button("Run targeted", id="run-targeted", variant="primary")
            yield Button("Run full suite", id="run-full")
            yield Button("Run failed", id="run-failed")
        yield DataTable(id="tests-table")
        yield RichLog(id="test-failures", wrap=True, markup=True)

    def on_mount(self) -> None:
        self.query_one("#tests-table", DataTable).add_columns("Command", "Status", "Duration")

    def show_report(self, report: Any) -> None:
        table = self.query_one("#tests-table", DataTable)
        table.clear()
        for check in report.checks:
            table.add_row(
                check.command,
                "SKIP" if check.skipped else "PASS" if check.passed else "FAIL",
                f"{check.result.duration_seconds:.2f}s" if check.result else "—",
            )
        failures = self.query_one("#test-failures", RichLog)
        failures.clear()
        for item in report.failures:
            failures.write(
                f"[b]FAILED[/b]\n{item.command}\n\n"
                f"{item.summary}\n"
                f"Location: {item.file or 'unknown'}:{item.line or '?'}\n"
                f"Likely cause: {item.likely_correction_area or 'inspect detailed output'}"
            )


class CheckpointsView(ViewPanel):
    title = "Checkpoints"

    def __init__(self, service: CheckpointApplicationService) -> None:
        super().__init__(id="checkpoints-view")
        self.service = service

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield DataTable(id="checkpoints-table", cursor_type="row")

    def on_mount(self) -> None:
        table = self.query_one("#checkpoints-table", DataTable)
        table.add_columns("ID", "Mission", "Description", "Revision", "Created")
        self.refresh_data()

    def refresh_data(self) -> None:
        table = self.query_one("#checkpoints-table", DataTable)
        table.clear()
        for item in self.service.list():
            table.add_row(
                item.id,
                item.mission_id or "",
                item.description,
                item.revision or "",
                item.created_at.strftime("%Y-%m-%d %H:%M"),
                key=item.id,
            )


class PlaybooksView(ViewPanel):
    title = "Playbooks"

    def __init__(self, root: Path) -> None:
        super().__init__(id="playbooks-view")
        self.loader = PlaybookLoader(root)

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield DataTable(id="playbooks-table", cursor_type="row")
        yield Static("", id="playbook-detail")

    def on_mount(self) -> None:
        table = self.query_one("#playbooks-table", DataTable)
        table.add_columns("Name", "Version", "Purpose")
        for item in self.loader.list():
            table.add_row(item.name, item.version, item.purpose, key=item.name)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "playbooks-table":
            return
        item = self.loader.get(str(event.row_key.value))
        self.query_one("#playbook-detail", Static).update(
            Syntax(
                yaml.safe_dump(item.model_dump(), sort_keys=False),
                "yaml",
                theme="monokai",
            )
        )


class DeploymentsView(ViewPanel):
    title = "Deployments"

    def __init__(self, service: DeploymentApplicationService) -> None:
        super().__init__(id="deployments-view")
        self.service = service

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield DataTable(id="deployments-table", cursor_type="row")
        yield Static(
            "Select a target, then use /deploy inspect|plan|apply|verify|rollback <target>.",
            id="deployment-detail",
        )

    def on_mount(self) -> None:
        table = self.query_one("#deployments-table", DataTable)
        table.add_columns("Target", "Type", "Host", "Environment", "Runtime", "Approval")
        for item in self.service.targets():
            table.add_row(
                item["name"],
                item["type"],
                item["host"],
                item["environment"],
                item["runtime"],
                item["approval"],
                key=item["name"],
            )

    def show_result(self, result: object) -> None:
        if hasattr(result, "model_dump"):
            result = result.model_dump(mode="json")
        self.query_one("#deployment-detail", Static).update(
            Syntax(json.dumps(result, indent=2, default=str), "json", theme="monokai")
        )


#: Provider types that report their own model list, mapped to what the picker
#: calls that list. Everything else takes a typed identifier.
CATALOG_PROVIDERS = {
    "openrouter": "the OpenRouter model catalog",
    "ollama": "the models installed on this Ollama",
}


class ProvidersView(ViewPanel):
    title = "Providers and models"

    def __init__(self, service: ProviderApplicationService) -> None:
        super().__init__(id="providers-view")
        self.service = service
        self._catalog_models: list[OpenRouterModel] = []
        self._configured_provider: ProviderStatus | None = self._active_provider()
        self._preferred_openrouter_model = (
            self._configured_provider.model
            if self._configured_provider and self._configured_provider.type == "openrouter"
            else ""
        )
        self.scope = "project"

    def _active_provider(self) -> ProviderStatus | None:
        """Prefer the provider the agent roles actually route to over an arbitrary one."""
        configured = self.service.providers()
        if not configured:
            return None
        routed = self.service.context.settings.models.get(self.service.routable_profile())
        if routed is not None:
            for item in configured:
                if item.name == routed.provider:
                    return item
        return configured[0]

    def compose(self) -> ComposeResult:
        yield from super().compose()
        with Vertical(id="provider-form"):
            with Grid(id="provider-core-fields"):
                with Vertical(classes="provider-field"):
                    yield Label("Provider name")
                    yield Input(
                        value=(self._configured_provider.name if self._configured_provider else ""),
                        placeholder="openrouter",
                        id="provider-name",
                    )
                with Vertical(classes="provider-field"):
                    yield Label("Provider type")
                    yield Select(
                        [
                            ("OpenRouter", "openrouter"),
                            ("Local Ollama", "ollama"),
                            ("Local vLLM", "vllm"),
                            ("OpenAI-compatible", "openai-compatible"),
                        ],
                        value=(
                            self._configured_provider.type if self._configured_provider else "vllm"
                        ),
                        allow_blank=False,
                        id="provider-type",
                    )
                with Vertical(classes="provider-field"):
                    yield Label("Base URL")
                    yield Input(
                        value=(
                            self._configured_provider.base_url if self._configured_provider else ""
                        ),
                        placeholder="http://127.0.0.1:8000/v1",
                        id="provider-base-url",
                    )
            with Grid(id="provider-detail-fields"):
                with Vertical(classes="provider-field provider-model-field"):
                    yield Label("Model")
                    yield Input(
                        value=(
                            self._configured_provider.model if self._configured_provider else ""
                        ),
                        placeholder="Model identifier",
                        id="provider-model",
                    )
                    yield Input(
                        placeholder="Search models by name or identifier",
                        id="provider-model-search",
                        classes="hidden",
                    )
                    yield Select(
                        [],
                        prompt="Select a model",
                        id="provider-model-select",
                        classes="hidden",
                    )
                with Vertical(classes="provider-field"):
                    yield Label("API key")
                    yield Input(
                        placeholder="Optional secret reference",
                        password=True,
                        id="provider-secret",
                    )
            with Horizontal(id="provider-actions"):
                yield Button("Use global settings", id="use-global-provider")
                yield Button(
                    "Refresh models",
                    id="refresh-provider-models",
                    classes="hidden",
                )
                yield Button("Validate + save", id="save-provider", variant="primary")
            yield Static(
                "Project provider settings. OpenRouter and Ollama list their own models.",
                id="provider-form-status",
            )
        yield DataTable(id="providers-table")
        yield Label("Models", classes="section-title")
        yield DataTable(id="models-table")

    def on_mount(self) -> None:
        providers = self.query_one("#providers-table", DataTable)
        providers.add_columns("Provider", "Type", "Base URL", "Model", "Status")
        models = self.query_one("#models-table", DataTable)
        models.add_columns("Profile", "Role", "Provider", "Model", "Context", "Cost")
        self.apply_provider_type(
            self._configured_provider.type if self._configured_provider else "vllm",
            fetch=False,
        )
        self.refresh_data()

    def set_scope(self, scope: str) -> None:
        if scope not in {"project", "global"}:
            raise ValueError(f"Unknown provider scope {scope}")
        self.scope = scope
        global_scope = scope == "global"
        self.query_one("#use-global-provider", Button).display = not global_scope
        self.query_one("#provider-form-status", Static).update(
            "Global provider settings shared by every project."
            if global_scope
            else "Project provider settings. Choose Use global settings to remove overrides."
        )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "provider-type":
            self.apply_provider_type(str(event.value))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "provider-model-search":
            self.filter_catalog_models(event.value)

    def apply_provider_type(self, provider_type: str, *, fetch: bool = True) -> None:
        openrouter = provider_type == "openrouter"
        listable = provider_type in CATALOG_PROVIDERS
        name = self.query_one("#provider-name", Input)
        base_url = self.query_one("#provider-base-url", Input)
        model_input = self.query_one("#provider-model", Input)
        model_search = self.query_one("#provider-model-search", Input)
        model_select = self.query_one("#provider-model-select", Select)
        secret = self.query_one("#provider-secret", Input)
        refresh = self.query_one("#refresh-provider-models", Button)
        known_names = {"", "openrouter", "local-ollama", "local-vllm", "openai-compatible"}
        known_urls = {
            "",
            "https://openrouter.ai/api/v1",
            "http://127.0.0.1:8000/v1",
            "http://127.0.0.1:11434/v1",
        }
        local_defaults = {
            "ollama": ("local-ollama", "http://127.0.0.1:11434/v1"),
            "vllm": ("local-vllm", "http://127.0.0.1:8000/v1"),
            "openai-compatible": ("openai-compatible", "http://127.0.0.1:8000/v1"),
        }
        if openrouter:
            if name.value in known_names:
                name.value = "openrouter"
            if base_url.value in known_urls:
                base_url.value = "https://openrouter.ai/api/v1"
            secret.placeholder = "Paste OpenRouter API key or secret reference"
        else:
            default_name, default_url = local_defaults.get(
                provider_type, local_defaults["openai-compatible"]
            )
            if name.value in known_names:
                name.value = default_name
            if base_url.value in known_urls:
                base_url.value = default_url
            secret.placeholder = "Secret reference (optional)"
        if listable:
            # Ollama names its models by tag — "qwen3.8:27b-mlx" — which nobody
            # recalls exactly, and a near miss only fails at the first request.
            # Listing what the provider actually has makes it a choice.
            model_input.add_class("hidden")
            model_search.remove_class("hidden")
            model_select.remove_class("hidden")
            refresh.remove_class("hidden")
            model_select.prompt = f"Select from {CATALOG_PROVIDERS[provider_type]}"
            self._catalog_models = []
            model_select.set_options([])
            self.set_save_state(f"Loading {CATALOG_PROVIDERS[provider_type]}…", busy=False)
            if fetch:
                self.load_catalog_models()
        else:
            model_search.add_class("hidden")
            model_select.add_class("hidden")
            model_input.remove_class("hidden")
            refresh.add_class("hidden")
            self.set_save_state(
                "Local and compatible providers accept a model identifier and optional "
                "secret reference.",
                busy=False,
            )

    @work(exclusive=True, group="catalog-models")
    async def load_catalog_models(self) -> None:
        provider_type = str(self.query_one("#provider-type", Select).value)
        label = CATALOG_PROVIDERS.get(provider_type)
        if label is None:
            return
        self.set_save_state(f"Loading {label}…", busy=True)
        try:
            if provider_type == "ollama":
                models = await self.service.ollama_models(
                    base_url=self.query_one("#provider-base-url", Input).value,
                )
            else:
                models = await self.service.openrouter_models(
                    api_key_input=self.query_one("#provider-secret", Input).value,
                    base_url=self.query_one("#provider-base-url", Input).value,
                )
        except Exception as exc:
            # A provider that cannot be listed must not leave the form with no
            # way to name a model, so the free-text field comes back.
            self.allow_typed_model(f"Could not load {label}: {exc}")
            return
        if str(self.query_one("#provider-type", Select).value) != provider_type:
            return
        self.set_catalog_models(models, selected=self._preferred_openrouter_model)
        self._preferred_openrouter_model = ""
        self.set_save_state(f"Loaded {len(models)} model(s).", busy=False)

    def allow_typed_model(self, reason: str) -> None:
        """Fall back to a typed identifier when the catalog is unreachable."""
        self.query_one("#provider-model-search", Input).add_class("hidden")
        self.query_one("#provider-model-select", Select).add_class("hidden")
        self.query_one("#provider-model", Input).remove_class("hidden")
        self.set_save_state(f"{reason} Enter the model identifier instead.", busy=False)

    # Kept as the name the provider screen's Refresh button and tests use.
    def load_openrouter_models(self) -> None:
        self.load_catalog_models()

    def ensure_openrouter_models(self) -> None:
        if not self._catalog_models:
            self.load_catalog_models()

    def set_catalog_models(
        self,
        models: list[OpenRouterModel],
        *,
        selected: str = "",
    ) -> None:
        self._catalog_models = models
        selector = self.query_one("#provider-model-select", Select)
        previous = selected or (str(selector.value) if selector.value is not Select.BLANK else "")
        query = self.query_one("#provider-model-search", Input).value
        filtered = self._matching_catalog_models(query)
        selector.set_options([(item.label, item.id) for item in filtered])
        available = {item.id for item in filtered}
        if previous in available:
            selector.value = previous

    def filter_catalog_models(self, query: str) -> None:
        selector = self.query_one("#provider-model-select", Select)
        previous = str(selector.value) if selector.value is not Select.BLANK else ""
        models = self._matching_catalog_models(query)
        selector.set_options([(item.label, item.id) for item in models])
        available = {item.id for item in models}
        if previous in available:
            selector.value = previous
        total = len(self._catalog_models)
        self.query_one("#provider-form-status", Static).update(
            f"Showing {len(models)} of {total} model(s)."
            if query.strip()
            else f"Loaded {total} model(s)."
        )

    def _matching_catalog_models(self, query: str) -> list[OpenRouterModel]:
        terms = query.casefold().split()
        if not terms:
            return self._catalog_models
        return [
            model
            for model in self._catalog_models
            if all(term in f"{model.name} {model.id}".casefold() for term in terms)
        ]

    def set_save_state(self, message: str, *, busy: bool) -> None:
        # Provider errors quote server responses, so this is untrusted text.
        self.query_one("#provider-form-status", Static).update(Content(message))
        self.query_one("#save-provider", Button).disabled = busy
        self.query_one("#refresh-provider-models", Button).disabled = busy

    def clear_secret(self) -> None:
        self.query_one("#provider-secret", Input).value = ""

    def provider_saved(self, item: ProviderStatus) -> None:
        self._configured_provider = item

    def refresh_data(self) -> None:
        providers = self.query_one("#providers-table", DataTable)
        providers.clear()
        for item in self.service.providers():
            providers.add_row(
                item.name,
                item.type,
                item.base_url,
                item.model,
                (
                    "Connected"
                    if item.connected
                    else "Unavailable"
                    if item.connected is False
                    else "Not tested"
                ),
            )
        models = self.query_one("#models-table", DataTable)
        models.clear()
        for model in self.service.models():
            models.add_row(
                model["name"],
                model["role"],
                model["provider"],
                model["model"],
                str(model["context_window"]),
                model["cost"],
            )

    def show_health(self, items: list[Any]) -> None:
        providers = self.query_one("#providers-table", DataTable)
        providers.clear()
        for item in items:
            providers.add_row(
                item.name,
                item.type,
                item.base_url,
                item.model,
                "Connected" if item.connected else f"Unavailable: {item.detail}",
            )

    def pending_provider(self) -> dict[str, str]:
        provider_type = str(self.query_one("#provider-type", Select).value)
        selector = self.query_one("#provider-model-select", Select)
        typed = self.query_one("#provider-model", Input).value.strip()
        model = (
            ("" if selector.value is Select.BLANK else str(selector.value))
            if provider_type in CATALOG_PROVIDERS and not selector.has_class("hidden")
            else typed
        )
        return {
            "name": self.query_one("#provider-name", Input).value.strip(),
            "provider_type": provider_type,
            "base_url": self.query_one("#provider-base-url", Input).value.strip(),
            "model": model,
            "api_key_input": self.query_one("#provider-secret", Input).value.strip(),
        }


class SettingsView(ViewPanel):
    title = "Settings"

    def __init__(self, service: SettingsApplicationService) -> None:
        super().__init__(id="settings-view")
        self.service = service

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield Static(
            "Settings are validated before save. Secret fields contain references only.",
            classes="hint",
        )
        with Horizontal(classes="toolbar"):
            yield Input(
                placeholder="Dotted key, e.g. runtime.default",
                id="setting-key",
            )
            yield Input(
                placeholder="YAML value or masked secret reference",
                id="setting-value",
                password=True,
            )
            yield Button("Apply", id="apply-setting", variant="primary")
        yield VerticalScroll(Static("", id="settings-content"))

    def on_mount(self) -> None:
        self.refresh_data()

    def refresh_data(self) -> None:
        self.query_one("#settings-content", Static).update(
            Syntax(
                yaml.safe_dump(self.service.safe_dump(), sort_keys=False),
                "yaml",
                theme="monokai",
            )
        )

    def pending_change(self) -> tuple[str, str]:
        return (
            self.query_one("#setting-key", Input).value.strip(),
            self.query_one("#setting-value", Input).value,
        )


class LogsView(ViewPanel):
    title = "Logs"

    def __init__(self, root: Path) -> None:
        super().__init__(id="logs-view")
        self.audit_log = AuditLog(root)
        self._live_label = "Idle — waiting for the next prompt"
        self._live_started = 0.0
        self._live_active = False

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield Label("Live activity", classes="section-title")
        yield Static(self._live_label, id="log-live-state")
        yield RichLog(id="log-live-content", wrap=True, markup=True, max_lines=120)
        yield Label("Recorded audit log", classes="section-title")
        with Horizontal(classes="toolbar"):
            yield Select(
                [("Summary", "summary"), ("Detailed", "detailed"), ("Raw", "raw")],
                value="summary",
                id="log-mode",
            )
            yield Input(placeholder="Filter mission, agent, tool, severity…", id="log-filter")
        yield RichLog(id="log-content", wrap=True, markup=True)

    def on_mount(self) -> None:
        self.set_interval(1.0, self._update_live_elapsed)
        self.refresh_data()

    def record_event(self, event: MissionEvent, *, verbose: bool = False) -> None:
        """Append one structured event to the live pane without exposing model thoughts."""
        if event.kind == "ModelReasoningChunk":
            # Reasoning chunks can arrive many times per second. The chat owns
            # their transient text; Logs exposes only one coalesced safe state
            # and never appends chunk content (or one line per chunk).
            label = "Model reasoning…" if verbose else "Working…"
            if label != self._live_label or not self._live_active:
                self._live_label = label
                self._live_started = monotonic()
                self._live_active = True
                self._update_live_elapsed()
            return
        summary = _live_event_summary(event)
        if not summary or not self.is_mounted:
            return
        summary = redact(summary)
        self._live_label = summary
        self._live_started = monotonic()
        self._live_active = event.kind not in {
            "MissionCompleted",
            "MissionFailed",
        }
        self._update_live_elapsed()
        timestamp = event.timestamp.strftime("%H:%M:%S")
        self.query_one("#log-live-content", RichLog).write(
            f"[dim]{timestamp}[/dim] {escape(summary)}"
        )

    def finish_activity(self, summary: str) -> None:
        """Mark a direct chat/answer worker complete when it has no mission terminal event."""
        if not self.is_mounted:
            return
        self._live_label = redact(summary)
        self._live_active = False
        self._live_started = monotonic()
        self._update_live_elapsed()
        self.query_one("#log-live-content", RichLog).write(escape(self._live_label))

    def _update_live_elapsed(self) -> None:
        if not self.is_mounted:
            return
        elapsed = (
            f" · {max(0, int(monotonic() - self._live_started))}s"
            if self._live_active and self._live_started
            else ""
        )
        self.query_one("#log-live-state", Static).update(f"{self._live_label}{elapsed}")

    def refresh_data(self, query: str = "") -> None:
        view = self.query_one("#log-content", RichLog)
        view.clear()
        mode = self.query_one("#log-mode", Select).value
        for event in self.audit_log.read()[-500:]:
            rendered = json.dumps(event, ensure_ascii=False, default=str)
            if query and query.lower() not in rendered.lower():
                continue
            if mode == "raw":
                view.write(escape(rendered))
            elif mode == "detailed":
                detail = {
                    key: value for key, value in event.items() if key not in {"timestamp", "event"}
                }
                view.write(
                    f"[dim]{escape(str(event.get('timestamp', '')))}[/dim] "
                    f"[b]{escape(str(event.get('event', 'event')))}[/b] "
                    f"{escape(json.dumps(detail, default=str))}"
                )
            else:
                view.write(
                    f"[dim]{escape(str(event.get('timestamp', ''))[11:19])}[/dim] "
                    f"{escape(str(event.get('event', 'event')))} "
                    f"{escape(str(event.get('mission_id', '') or ''))}"
                )

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "log-filter":
            self.refresh_data(event.value)

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "log-mode":
            self.refresh_data(self.query_one("#log-filter", Input).value)


def _live_event_summary(event: MissionEvent) -> str:
    """Render only safe operational facts from a mission event."""
    kind = event.kind
    if kind in {"ModelStreamChunk", "ModelReasoningChunk"}:
        return ""
    if kind == "MissionCreated":
        return "Understanding the prompt"
    if kind == "AgentRoleChanged":
        return f"{str(getattr(event, 'role', 'agent')).title()} preparing the next action"
    if kind == "ModelSelected":
        role = str(getattr(event, "role", "agent")).title()
        model = str(getattr(event, "model", "model"))
        return f"{role} using {model} to generate the next action"
    if kind in {"ToolStarted", "ToolProgress", "ToolCompleted"}:
        tool = str(getattr(event, "tool", "tool")).removeprefix("agent.")
        summary = str(getattr(event, "summary", ""))
        suffix = " completed; deciding the next action" if kind == "ToolCompleted" else ""
        return f"{tool}: {summary}{suffix}".strip()
    if kind == "ToolFailed":
        return f"{getattr(event, 'tool', 'tool')} failed: {getattr(event, 'error', '')}"
    if kind == "TaskStarted":
        return f"Task started: {getattr(event, 'title', '')}"
    if kind == "TaskCompleted":
        return f"Task completed: {getattr(event, 'title', '')}"
    if kind == "FileChanged":
        return f"Changed {getattr(event, 'path', '')}"
    if kind == "TestsStarted":
        commands = list(getattr(event, "commands", []))
        return f"Running {len(commands)} verification command(s)"
    if kind == "TestsCompleted":
        return (
            f"Verification {'passed' if getattr(event, 'passed', False) else 'failed'}: "
            f"{getattr(event, 'passed_count', 0)} passed, "
            f"{getattr(event, 'failed_count', 0)} failed"
        )
    if kind == "MissionCompleted":
        return "Prompt completed"
    if kind == "MissionFailed":
        return f"Prompt failed: {getattr(event, 'error', '')}"
    if kind == "TeamMemberStarted":
        return f"Team member {getattr(event, 'member', '')} started"
    if kind == "TeamMemberCompleted":
        return f"Team member {getattr(event, 'member', '')} completed"
    if kind.startswith("Deployment"):
        return f"Deployment: {getattr(event, 'stage', kind.replace('Deployment', ''))}"
    return kind.replace("_", " ")


class ExecutionMapView(ViewPanel):
    """Clickable prompt index and a chronological execution graph."""

    title = "Prompt execution map"

    def __init__(self, service: ExecutionMapApplicationService) -> None:
        super().__init__(id="map-view")
        self.service = service
        self.selected_mission_id = ""

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield Static(
            "Select a prompt to inspect models, tools, tests, timing, tokens, and cost. "
            "The map uses structured audit events and never private chain-of-thought.",
            classes="hint",
        )
        with Horizontal(id="execution-map-layout"):
            with Vertical(id="map-prompt-panel"):
                yield Label("Prompts", classes="section-title")
                yield DataTable(id="map-prompts", cursor_type="row")
            with Vertical(id="map-trace-panel"):
                yield Static("Select a prompt", id="map-trace-summary")
                yield VerticalScroll(Static("", id="map-graph"), id="map-graph-scroll")

    def on_mount(self) -> None:
        self.query_one("#map-prompts", DataTable).add_columns(
            "Started", "Prompt", "Status", "Tokens", "Steps"
        )
        self.refresh_data()

    def refresh_data(self) -> None:
        prompts = self.service.prompts()
        table = self.query_one("#map-prompts", DataTable)
        table.clear()
        for prompt in prompts:
            table.add_row(
                prompt.created_at.strftime("%m-%d %H:%M"),
                prompt.title,
                prompt.status.replace("_", " ").upper(),
                f"{prompt.total_tokens:,}",
                str(prompt.step_count),
                key=prompt.mission_id,
            )
        if not prompts:
            self.selected_mission_id = ""
            self.query_one("#map-trace-summary", Static).update("No prompts recorded yet")
            self.query_one("#map-graph", Static).update(
                "Send a prompt in Chat; its audited execution graph will appear here."
            )
            return
        available = {prompt.mission_id for prompt in prompts}
        if self.selected_mission_id not in available:
            self.selected_mission_id = prompts[0].mission_id
        self.refresh_selected()

    def refresh_selected(self) -> None:
        if not self.selected_mission_id:
            return
        try:
            trace = self.service.trace(self.selected_mission_id)
        except ValueError as exc:
            self.query_one("#map-trace-summary", Static).update(str(exc))
            return
        self.query_one("#map-trace-summary", Static).update(_trace_summary(trace))
        self.query_one("#map-graph", Static).update(_render_execution_graph(trace))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id != "map-prompts":
            return
        self._select_prompt(str(event.row_key.value))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id != "map-prompts" or event.row_key.value is None:
            return
        self._select_prompt(str(event.row_key.value))

    def _select_prompt(self, mission_id: str) -> None:
        if mission_id == self.selected_mission_id:
            return
        self.selected_mission_id = mission_id
        self.refresh_selected()
        self.query_one("#map-graph-scroll", VerticalScroll).scroll_home(animate=False)


def _trace_summary(trace: ExecutionTrace) -> Content:
    return Content.assemble(
        (trace.request, "bold"),
        "\n",
        f"{trace.status.replace('_', ' ').upper()}  •  "
        f"{len(trace.steps)} steps  •  {trace.model_call_count} model  •  "
        f"{trace.tool_count} tools  •  {trace.total_tokens:,} tokens  •  "
        f"{_trace_cost(trace.estimated_cost)}",
    )


def _render_execution_graph(trace: ExecutionTrace) -> Content:
    parts: list[str | tuple[str, str]] = [
        ("● PROMPT", "bold cyan"),
        f"  {trace.created_at.strftime('%H:%M:%S')}  {trace.request}\n",
    ]
    if not trace.steps:
        parts.extend(["│\n", ("└─ No execution steps recorded", "dim")])
        return Content.assemble(*parts)
    for index, step in enumerate(trace.steps, start=1):
        last = index == len(trace.steps)
        branch = "└─" if last else "├─"
        continuation = "  " if last else "│ "
        colour = _step_colour(step)
        parts.extend(
            [
                "│\n",
                (f"{branch} {index:02d} {_step_label(step)}", f"bold {colour}"),
                f"  [{step.status.upper()}]  {step.timestamp.strftime('%H:%M:%S')}\n",
            ]
        )
        if step.detail:
            parts.append(f"{continuation}   {step.detail}\n")
        if step.target and step.target != step.detail:
            parts.append(f"{continuation}   target {step.target}\n")
        if step.model_usage is not None:
            usage = step.model_usage
            parts.append(
                f"{continuation}   tokens {usage.input_tokens:,} in + "
                f"{usage.output_tokens:,} out = {usage.total_tokens:,}  •  "
                f"{_trace_cost(usage.estimated_cost)}  •  "
                f"{usage.latency_ms / 1000:.2f}s\n"
            )
        else:
            duration = f"{step.duration_seconds:.2f}s" if step.duration_seconds else "instant"
            parts.append(f"{continuation}   tokens —  •  {duration}\n")
    return Content.assemble(*parts)


def _step_label(step: ExecutionTraceStep) -> str:
    return f"{step.kind.upper()}  {step.title}"


def _step_colour(step: ExecutionTraceStep) -> str:
    if step.status.casefold() in {"failed", "error", "blocked"}:
        return "red"
    return {
        "model": "magenta",
        "tool": "cyan",
        "tests": "yellow",
        "verification": "yellow",
        "file": "green",
    }.get(step.kind.casefold(), "white")


def _trace_cost(value: float) -> str:
    if value <= 0:
        return "$0.0000"
    if value < 0.0001:
        return f"${value:.10f}".rstrip("0")
    return f"${value:.4f}"


class ApprovalsView(ViewPanel):
    title = "Approvals"

    def __init__(self, mission_service: MissionApplicationService) -> None:
        super().__init__(id="approvals-view")
        self.service = mission_service

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield DataTable(id="approvals-table")

    def on_mount(self) -> None:
        table = self.query_one("#approvals-table", DataTable)
        table.add_columns("Mission", "Category", "Subject", "Decision")
        self.refresh_data()

    def refresh_data(self) -> None:
        from sqlalchemy import select

        from daino.persistence.models import Approval

        table = self.query_one("#approvals-table", DataTable)
        table.clear()
        with self.service.context.database.session() as session:
            for item in session.scalars(
                select(Approval).order_by(Approval.created_at.desc()).limit(100)
            ):
                table.add_row(
                    item.mission_id or "",
                    item.category,
                    item.subject,
                    "Approved" if item.approved else "Rejected",
                )


class HelpView(ViewPanel):
    title = "Help"

    def __init__(self) -> None:
        super().__init__(id="help-view")

    def compose(self) -> ComposeResult:
        yield from super().compose()
        shortcut_text = "\n".join(f"[b]{key:<12}[/b] {text}" for key, text in SHORTCUTS)
        # Descriptions and usage strings are data, not markup: escape them so a
        # bracketed product name and a usage like "[title]" both survive intact.
        command_text = "\n".join(
            f"[b]{item.name:<14}[/b] {branding.escape_markup(item.description)} "
            f"[dim]{branding.escape_markup(item.usage)}[/dim]"
            for item in SLASH_COMMANDS
        )
        yield VerticalScroll(
            Static(
                "[b]Keyboard shortcuts[/b]\n\n"
                f"{shortcut_text}\n\n"
                "[b]Slash commands[/b]\n\n"
                f"{command_text}\n\n"
                "[b]Mission workflow[/b]\n\n"
                "Instruction → requirements and plan → explicit approval → isolated "
                "worktree → implementation → verification and bounded repair → "
                "independent review → evidence.\n\n"
                "[b]Security[/b]\n\n"
                "Secrets stay as references. Risky commands, infrastructure changes, "
                "production deployment, rollback, and checkpoint restore require approval.",
            )
        )
