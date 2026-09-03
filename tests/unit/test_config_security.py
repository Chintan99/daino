from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from daino.config import load_settings, save_settings, set_value
from daino.config.models import ProviderConfig, SecurityConfig, Settings
from daino.security import PolicyEngine, redact, resolve_secret, store_project_secret
from daino.security.commands import CommandGate, Verdict


def test_config_round_trip_and_dotted_update(tmp_path: Path) -> None:
    settings = Settings(project={"name": "test"})
    save_settings(settings, tmp_path)
    set_value(tmp_path, "runtime.command_timeout_seconds", "42")
    loaded = load_settings(tmp_path)
    assert loaded.project.name == "test"
    assert loaded.runtime.command_timeout_seconds == 42


def test_provider_rejects_literal_secret() -> None:
    with pytest.raises(ValidationError):
        ProviderConfig(
            type="openrouter",
            base_url="https://example.invalid/v1",
            model="test",
            api_key="literal-secret",
        )


def test_secret_resolution_and_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VASUKI_TEST_KEY", "sk-supersecretvalue123")
    assert resolve_secret("env://VASUKI_TEST_KEY") == "sk-supersecretvalue123"
    output = redact("api_key=abc123 token: qwerty sk-supersecretvalue123")
    assert "abc123" not in output
    assert "qwerty" not in output
    assert "supersecret" not in output


def test_project_secret_is_private_and_only_returns_reference(tmp_path: Path) -> None:
    reference = store_project_secret(tmp_path, "openrouter", "sk-or-valid-test-key")
    secret_path = Path(reference.removeprefix("file://"))

    assert reference.startswith("file://")
    assert resolve_secret(reference) == "sk-or-valid-test-key"
    assert secret_path.stat().st_mode & 0o777 == 0o600
    assert secret_path.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf build",
        "mkfs /dev/sdb",
        "shutdown now",
        "DROP TABLE users",
        "docker system prune",
        "terraform destroy",
    ],
)
def test_dangerous_commands_require_approval(command: str) -> None:
    decision = PolicyEngine().command_decision(command)
    assert not decision.allowed
    assert decision.requires_approval


def test_install_and_network_require_approval() -> None:
    policy = PolicyEngine()
    assert policy.command_decision("pip install httpx").requires_approval
    assert policy.command_decision("curl https://example.com").requires_approval
    assert policy.command_decision("pytest").allowed


def test_bare_env_is_no_longer_unattended() -> None:
    """A full environment dump reaches the transcript, so it has to be asked about.

    ``redact`` masks the secrets Daino manages; it cannot mask an API key the
    user exported into their own shell, and the dump is persisted, rendered in
    two clients, and replayed to the model as context.
    """
    gate = CommandGate()
    decision = gate.decide("env")
    assert decision.verdict is Verdict.ASK
    assert decision.signature == "env"


def test_env_prefix_is_judged_by_the_program_it_runs() -> None:
    """``env VAR=value pytest`` is an ordinary test run, not an environment dump."""
    gate = CommandGate()
    assert gate.decide("env PYTHONPATH=src pytest -q").verdict is Verdict.ALLOW
    assert gate.decide("env -i FOO=bar python -m pytest").verdict is Verdict.ALLOW
    # The approval memory keys on the real program, not on the wrapper.
    assert gate.signature("env FOO=bar pip install httpx") == "pip install"


def test_env_prefix_does_not_launder_an_unsafe_program() -> None:
    gate = CommandGate()
    assert gate.decide("env FOO=bar curl https://example.com").verdict is not Verdict.ALLOW


def test_env_split_string_is_not_unwrapped() -> None:
    """``-S`` re-parses its argument as a command line; do not try to model that."""
    gate = CommandGate()
    assert gate.decide("env -S 'pytest -q'").verdict is Verdict.ASK


def test_denying_env_is_not_bypassed_by_the_prefix() -> None:
    gate = CommandGate(SecurityConfig(denied_commands=["env"]))
    assert gate.decide("env FOO=bar pytest").verdict is not Verdict.ALLOW
