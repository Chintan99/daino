from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from vasuki.config import load_settings, save_settings, set_value
from vasuki.config.models import ProviderConfig, Settings
from vasuki.security import PolicyEngine, redact, resolve_secret, store_project_secret


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
