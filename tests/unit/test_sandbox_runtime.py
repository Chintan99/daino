"""The middle tier: host toolchain, scrubbed environment, OS confinement if any."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from daino.application.settings_service import SettingsApplicationService
from daino.config.models import Settings
from daino.runtimes import LocalRuntime, SandboxedLocalRuntime, scrub_environment
from daino.runtimes.sandbox import (
    CREDENTIAL_MARKERS,
    _bwrap_arguments,
    _seatbelt_profile,
    describe_command,
)


def test_credentials_are_dropped_and_the_toolchain_is_kept() -> None:
    scrubbed = scrub_environment(
        {
            "PATH": "/usr/bin",
            "HOME": "/home/dev",
            "PYTHONPATH": "src",
            "OPENROUTER_API_KEY": "sk-live-1",
            "AWS_SECRET_ACCESS_KEY": "abc",
            "GITHUB_TOKEN": "ghp_x",
            "DATABASE_PASSWORD": "hunter2",
            "SOME_RANDOM_VAR": "whatever",
        }
    )
    assert scrubbed == {"PATH": "/usr/bin", "HOME": "/home/dev", "PYTHONPATH": "src"}


def test_the_allowlist_is_what_hides_a_variable_nobody_anticipated() -> None:
    """A denylist would miss the service nobody thought of; this cannot."""
    scrubbed = scrub_environment({"PATH": "/usr/bin", "ACME_INTERNAL_HANDLE": "x"})
    assert "ACME_INTERNAL_HANDLE" not in scrubbed


def test_widening_the_passthrough_cannot_readmit_a_credential() -> None:
    scrubbed = scrub_environment(
        {"PATH": "/usr/bin", "MY_BUILD_FLAG": "on", "MY_BUILD_TOKEN": "secret"},
        passthrough={"MY_BUILD_FLAG", "MY_BUILD_TOKEN"},
    )
    assert scrubbed["MY_BUILD_FLAG"] == "on"
    assert "MY_BUILD_TOKEN" not in scrubbed


def test_every_credential_marker_is_actually_filtered() -> None:
    for marker in CREDENTIAL_MARKERS:
        name = f"ACME_{marker}"
        assert name not in scrub_environment({"PATH": "/x", name: "value"}, passthrough={name})


@pytest.mark.asyncio
async def test_a_command_cannot_see_the_user_s_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point, exercised through a real subprocess."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-live-should-not-appear")
    runtime = SandboxedLocalRuntime(tmp_path, mechanism="")
    result = await runtime.execute(
        f"{sys.executable} -c "
        "\"import os;print(os.environ.get('OPENROUTER_API_KEY','ABSENT'))\"",
        approved=True,
    )
    assert result.exit_code == 0
    assert "ABSENT" in result.stdout
    assert "sk-live" not in result.stdout


@pytest.mark.asyncio
async def test_the_unsandboxed_runtime_still_inherits_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``local`` is the no-isolation tier and must keep saying so honestly."""
    monkeypatch.setenv("DAINO_TEST_MARKER", "present")
    runtime = LocalRuntime(tmp_path)
    result = await runtime.execute(
        f"{sys.executable} -c "
        "\"import os;print(os.environ.get('DAINO_TEST_MARKER','ABSENT'))\"",
        approved=True,
    )
    assert "present" in result.stdout


@pytest.mark.asyncio
async def test_the_project_toolchain_still_works(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "greeter.py").write_text("VALUE = 7\n", encoding="utf-8")
    runtime = SandboxedLocalRuntime(tmp_path, mechanism="")
    result = await runtime.execute(
        f"{sys.executable} -c \"import greeter;print(greeter.VALUE)\"", approved=True
    )
    assert result.exit_code == 0
    assert "7" in result.stdout


def test_confinement_reports_environment_only_honestly(tmp_path: Path) -> None:
    """A sandbox that silently degrades to nothing is worse than no sandbox."""
    runtime = SandboxedLocalRuntime(tmp_path, mechanism="")
    confinement = runtime.confinement()
    assert confinement.mechanism == ""
    assert confinement.filesystem is False
    assert confinement.network is False
    assert "environment-only" in confinement.describe
    assert "unrestricted" in confinement.describe


def test_confinement_reports_what_an_os_sandbox_adds(tmp_path: Path) -> None:
    runtime = SandboxedLocalRuntime(tmp_path, mechanism="bubblewrap")
    confinement = runtime.confinement()
    assert confinement.filesystem is True
    assert confinement.network is True
    assert "network denied" in confinement.describe


def test_seatbelt_confines_writes_and_denies_the_network(tmp_path: Path) -> None:
    profile = _seatbelt_profile(tmp_path, network=False)
    assert "(deny file-write*)" in profile
    assert f'(allow file-write* (subpath "{tmp_path.resolve()}"))' in profile
    assert "(deny network*)" in profile
    # Reads stay open: a build reads the toolchain and the package cache.
    assert "(allow default)" in profile


def test_seatbelt_keeps_the_network_when_a_project_needs_it(tmp_path: Path) -> None:
    assert "(deny network*)" not in _seatbelt_profile(tmp_path, network=True)


def test_bubblewrap_binds_the_project_writable_and_the_host_read_only(
    tmp_path: Path,
) -> None:
    arguments = _bwrap_arguments(tmp_path, network=False)
    assert arguments[:4] == ["bwrap", "--ro-bind", "/", "/"]
    assert "--unshare-net" in arguments
    assert "--die-with-parent" in arguments
    index = arguments.index("--bind")
    assert arguments[index + 1] == str(tmp_path.resolve())


def test_the_wrapper_prefixes_the_real_command(tmp_path: Path) -> None:
    runtime = SandboxedLocalRuntime(tmp_path, mechanism="bubblewrap")
    rendered = describe_command(runtime, "pytest -q")
    assert rendered.startswith("bwrap ")
    assert rendered.endswith("pytest -q")


def test_no_mechanism_leaves_the_command_untouched(tmp_path: Path) -> None:
    runtime = SandboxedLocalRuntime(tmp_path, mechanism="")
    assert describe_command(runtime, "pytest -q") == "pytest -q"


@pytest.mark.asyncio
async def test_inspect_says_what_is_actually_enforced(tmp_path: Path) -> None:
    runtime = SandboxedLocalRuntime(tmp_path, mechanism="sandbox-exec")
    report = await runtime.inspect()
    assert report["type"] == "sandbox"
    assert report["mechanism"] == "sandbox-exec"
    assert report["network_denied"] is True
    assert "OPENROUTER_API_KEY" not in report["environment_keys"]


def test_sandbox_is_a_selectable_runtime(tmp_path: Path) -> None:
    settings = Settings()
    assert settings.runtime.sandbox_passthrough_env == []
    settings.runtime.default = "sandbox"

    class Context:
        def __init__(self) -> None:
            self.settings = settings
            self.root = tmp_path

    service = SettingsApplicationService(Context())  # type: ignore[arg-type]
    service.set_runtime("sandbox")
    assert service.context.settings.runtime.default == "sandbox"
    with pytest.raises(ValueError, match="local, sandbox, docker, or ssh"):
        service.set_runtime("jail")
