"""First-run repository onboarding."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.content import Content, Span
from textual.screen import Screen
from textual.widgets import Button, Input, Label, LoadingIndicator, Select, Static

from daino.application import initialize_project, open_project
from daino.application.provider_service import list_ollama_models
from daino.config import load_settings, save_settings
from daino.config.globals import has_global_provider, save_global
from daino.config.models import ModelProfileConfig, ProviderConfig
from daino.model_router import ModelRole


class OnboardingScreen(Screen[None]):
    BINDINGS = [("ctrl+q", "app.quit", "Quit")]

    def __init__(self, root: Path, *, error: str = "") -> None:
        super().__init__()
        self.root = root.resolve()
        self.error = error

    def compose(self) -> ComposeResult:
        global_available = has_global_provider()
        with VerticalScroll(id="onboarding"):
            yield Label("DAINO", id="onboarding-logo")
            yield Label("Interactive AI engineering workspace", classes="subtitle")
            yield Static(
                f"[b]Repository[/b]\n{self.root}\n\n"
                f"Git: {'detected' if (self.root / '.git').exists() else 'not detected'}\n"
                f"Docker: {'detected' if shutil.which('docker') else 'not detected'}\n"
                f"Python: "
                f"{'detected' if any(self.root.glob('pyproject.toml')) else 'not detected'}"
                "\n\n"
                "Initialization creates .daino/config.yaml, the local mission database, "
                "and a repository index. Provider credentials can be configured later.",
                id="onboarding-summary",
            )
            yield Select(
                [
                    ("Global settings (shared across projects)", "global"),
                    ("Project-specific settings", "project"),
                ],
                value="global" if global_available else "project",
                id="settings-scope",
            )
            yield Static(
                (
                    "Global settings are available. Choose whether this project inherits them "
                    "or uses its own provider/model selection."
                    if global_available
                    else "No global model is configured yet. Provider details can be saved "
                    "globally for future projects or only for this project."
                ),
                id="settings-scope-help",
                classes="hint",
            )
            yield Select(
                [
                    ("Configure later", "later"),
                    ("Local vLLM", "vllm"),
                    ("Local Ollama", "ollama"),
                    ("OpenRouter", "openrouter"),
                    ("Generic OpenAI-compatible", "openai-compatible"),
                ],
                value="later",
                id="provider-choice",
            )
            yield Input(
                placeholder="Provider base URL (optional)",
                id="provider-url",
            )
            yield Input(
                placeholder="Model identifier (optional)",
                id="provider-model",
            )
            yield Select(
                [],
                prompt="Select an installed Ollama model",
                id="provider-model-select",
                classes="hidden",
            )
            yield Static("", id="provider-model-hint", classes="hint")
            yield Input(
                placeholder="Secret reference, e.g. env://OPENROUTER_API_KEY",
                password=True,
                id="provider-secret",
            )
            yield Select(
                [("Docker", "docker"), ("Local", "local"), ("Remote SSH", "ssh")],
                value="docker",
                id="runtime-choice",
            )
            yield Static(
                Content(self.error, spans=[Span(0, len(self.error), "bold")]),
                id="onboarding-error",
            )
            yield LoadingIndicator(id="onboarding-loading", classes="hidden")
            with Horizontal(classes="onboarding-actions"):
                yield Button("Initialize Daino", id="initialize", variant="primary")
                yield Button("Quit", id="quit")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "provider-choice":
            self.apply_provider_choice(str(event.value))

    def apply_provider_choice(self, provider_type: str) -> None:
        """Offer a local Ollama's installed models rather than a typed guess."""
        selector = self.query_one("#provider-model-select", Select)
        typed = self.query_one("#provider-model", Input)
        hint = self.query_one("#provider-model-hint", Static)
        if provider_type != "ollama":
            selector.add_class("hidden")
            typed.remove_class("hidden")
            hint.update("")
            return
        typed.add_class("hidden")
        selector.set_options([])
        selector.remove_class("hidden")
        hint.update("Looking for models on this machine…")
        self.load_ollama_models()

    @work(exclusive=True, group="onboarding-ollama-models")
    async def load_ollama_models(self) -> None:
        base_url = (
            self.query_one("#provider-url", Input).value.strip() or "http://127.0.0.1:11434/v1"
        )
        try:
            models = await list_ollama_models(base_url)
        except Exception as exc:
            # Ollama may simply not be running yet. Falling back to the typed
            # field keeps onboarding completable instead of dead-ending.
            self.query_one("#provider-model-select", Select).add_class("hidden")
            self.query_one("#provider-model", Input).remove_class("hidden")
            self.query_one("#provider-model-hint", Static).update(
                Content(f"{exc} Enter a model identifier instead.")
            )
            return
        if str(self.query_one("#provider-choice", Select).value) != "ollama":
            return
        selector = self.query_one("#provider-model-select", Select)
        selector.set_options([(item.label, item.id) for item in models])
        if models:
            selector.value = models[0].id
        self.query_one("#provider-model-hint", Static).update(
            f"{len(models)} model(s) installed on this machine."
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "initialize":
            self.initialize()
        elif event.button.id == "quit":
            self.app.exit()

    @work(exclusive=True)
    async def initialize(self) -> None:
        loading = self.query_one("#onboarding-loading", LoadingIndicator)
        error = self.query_one("#onboarding-error", Static)
        loading.remove_class("hidden")
        error.update("")
        try:
            result = await asyncio.to_thread(initialize_project, self.root)
            settings = load_settings(self.root)
            provider_type = str(self.query_one("#provider-choice", Select).value)
            settings_scope = str(self.query_one("#settings-scope", Select).value)
            runtime = str(self.query_one("#runtime-choice", Select).value)
            settings.runtime.default = runtime  # type: ignore[assignment]
            if provider_type != "later":
                defaults = {
                    "vllm": (
                        "http://127.0.0.1:8000/v1",
                        "local-coder",
                        "",
                    ),
                    "openrouter": (
                        "https://openrouter.ai/api/v1",
                        "openrouter/auto",
                        "env://OPENROUTER_API_KEY",
                    ),
                    # No hardcoded tag: the installed models are listed above
                    # and this is only reached when none could be read.
                    "ollama": (
                        "http://127.0.0.1:11434/v1",
                        "",
                        "",
                    ),
                    "openai-compatible": (
                        "http://127.0.0.1:8000/v1",
                        "default",
                        "",
                    ),
                }
                default_url, default_model, default_secret = defaults[provider_type]
                url = self.query_one("#provider-url", Input).value or default_url
                picker = self.query_one("#provider-model-select", Select)
                picked = (
                    ""
                    if picker.has_class("hidden") or picker.value is Select.BLANK
                    else str(picker.value)
                )
                model = (
                    picked or self.query_one("#provider-model", Input).value or default_model
                )
                if not model:
                    raise ValueError(
                        "No model was chosen. Pull one with `ollama pull <model>` and "
                        "reopen this screen, or type an identifier."
                    )
                secret = self.query_one("#provider-secret", Input).value or default_secret
                name = (
                    "local-vllm"
                    if provider_type == "vllm"
                    else "local-ollama"
                    if provider_type == "ollama"
                    else provider_type
                )
                settings.providers[name] = ProviderConfig(
                    type=provider_type,
                    base_url=url,
                    model=model,
                    api_key=secret,
                    timeout=300 if provider_type in {"ollama", "vllm"} else 120,
                )
                settings.models[name] = ModelProfileConfig(
                    provider=name,
                    model=model,
                    local=provider_type in {"ollama", "vllm"},
                )
                settings.routing = {role.value: name for role in ModelRole}
                if settings_scope == "global":
                    save_global(settings)
            save_settings(settings, self.root)
            context = await asyncio.to_thread(open_project, self.root)
        except Exception as exc:
            error.update(
                Content.assemble(
                    ("Initialization failed: ", "bold"),
                    f"{exc}\nCheck directory permissions, then retry.",
                )
            )
            loading.add_class("hidden")
            return
        self.notify(
            f"Indexed {result['files']} files. Opening {self.root.name}…",
            severity="information",
        )
        await self.app.open_workspace(context)  # type: ignore[attr-defined]
