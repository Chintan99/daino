from __future__ import annotations

from pathlib import Path

import pytest

from vasuki.application import (
    MissionApplicationService,
    ProviderApplicationService,
    open_project,
)


def test_session_effort_supports_current_ollama_reasoning_levels(
    project: tuple[Path, object, object],
) -> None:
    root, _, _ = project
    context = open_project(root)
    service = ProviderApplicationService(context)
    service.add(
        name="local-ollama",
        provider_type="ollama",
        base_url="http://127.0.0.1:11434/v1",
        model="qwen3.8:27b-mlx",
    )
    session_id = MissionApplicationService(context).create_session()

    for effort in ("none", "low", "medium", "high", "max"):
        profile, selected = service.set_session_effort(session_id, effort)
        assert profile == "local-ollama"
        assert selected == effort
        assert service.session_effort(session_id) == effort

    service.set_session_effort(session_id, "auto")
    assert context.settings.models["local-ollama"].reasoning_effort is None
    context.close()


@pytest.mark.parametrize("effort", ["minimal", "xhigh"])
def test_session_effort_rejects_levels_ollama_does_not_standardize(
    project: tuple[Path, object, object],
    effort: str,
) -> None:
    root, _, _ = project
    context = open_project(root)
    service = ProviderApplicationService(context)
    service.add(
        name="local-ollama",
        provider_type="ollama",
        base_url="http://127.0.0.1:11434/v1",
        model="qwen3.8:27b-mlx",
    )
    session_id = MissionApplicationService(context).create_session()

    with pytest.raises(ValueError, match="Ollama effort"):
        service.set_session_effort(session_id, effort)

    context.close()
