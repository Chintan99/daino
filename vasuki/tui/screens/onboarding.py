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

from vasuki.application import initialize_project, open_project
from vasuki.config import load_settings, save_settings
from vasuki.config.globals import save_global
from vasuki.config.models import ModelProfileConfig, ProviderConfig
from vasuki.model_router import ModelRole


class OnboardingScreen(Screen[None]):
    BINDINGS = [("ctrl+q", "app.quit", "Quit")]

    def __init__(self, root: Path, *, error: str = "") -> None:
        super().__init__()
        self.root = root.resolve()
        self.error = error

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="onboarding"):
            yield Label("VASUKI", id="onboarding-logo")
            yield Label("Interactive AI engineering workspace", classes="subtitle")
            yield Static(
                f"[b]Repository[/b]\n{self.root}\n\n"
                f"Git: {'detected' if (self.root / '.git').exists() else 'not detected'}\n"
                f"Docker: {'detected' if shutil.which('docker') else 'not detected'}\n"
                f"Python: "
                f"{'detected' if any(self.root.glob('pyproject.toml')) else 'not detected'}"
                "\n\n"
                "Initialization creates .vasuki/config.yaml, the local mission database, "
                "and a repository index. Provider credentials can be configured later.",
                id="onboarding-summary",
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
                yield Button("Initialize Vasuki", id="initialize", variant="primary")
                yield Button("Quit", id="quit")

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
                    "ollama": (
                        "http://127.0.0.1:11434/v1",
                        "llama3.2",
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
                model = self.query_one("#provider-model", Input).value or default_model
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
                # Written globally, not into this project: the model you just
                # chose is the model you want everywhere, and repeating this
                # form in the next directory is not a decision anyone wants to
                # make twice.
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
