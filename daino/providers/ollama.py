"""Local Ollama provider adapter."""

from __future__ import annotations

from typing import Any

import httpx

from daino.exceptions import ProviderError
from daino.providers.base import DEFAULT_MAX_OUTPUT_TOKENS
from daino.providers.openai_compatible import OpenAICompatibleProvider


class OllamaProvider(OpenAICompatibleProvider):
    """OpenAI-compatible adapter for Ollama (base URL ends in ``/v1``).

    Ollama runs fully offline and parses tool calls itself, so chat,
    schema-constrained output, and native tool calling are enabled by default.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://127.0.0.1:11434/v1",
        api_key: str = "",
        timeout: float = 300,
        max_retries: int = 2,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        features: list[str] | None = None,
        reasoning_effort: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        concurrency: int = 0,
    ) -> None:
        super().__init__(
            name="ollama",
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=timeout,
            max_retries=max_retries,
            max_output_tokens=max_output_tokens,
            features=(features if features is not None else ["chat", "structured", "tools"]),
            reasoning_effort=reasoning_effort,
            transport=transport,
            concurrency=concurrency,
        )

    async def list_models(self) -> list[dict[str, Any]]:
        """Return the models installed on this Ollama, richest source first.

        ``/api/tags`` carries the on-disk size, parameter size and capability
        list that make a model choosable; the OpenAI-compatible ``/v1/models``
        reports only identifiers. The native endpoint sits beside the ``/v1``
        base URL rather than under it, so it is addressed from the host.
        """
        base = str(self.client.base_url)
        root = base[: -len("/v1/")] if base.rstrip("/").endswith("/v1") else base.rstrip("/")
        for path, key in ((f"{root}/api/tags", "models"), (f"{base}models", "data")):
            try:
                response = await self.client.get(path)
            except httpx.HTTPError:
                continue
            if response.status_code != 200:
                continue
            try:
                payload = response.json()
            except ValueError:
                continue
            items = payload.get(key) if isinstance(payload, dict) else None
            if isinstance(items, list) and items:
                return [item for item in items if isinstance(item, dict)]
        raise ProviderError(
            f"Ollama at {root} listed no models. Start it with `ollama serve` and "
            "pull one with `ollama pull <model>`."
        )

    def _constrain_payload(
        self, payload: dict[str, Any], schema_json: dict[str, Any], schema_name: str
    ) -> None:
        # Ollama's grammar-constrained decoding accepts a full JSON Schema in
        # the top-level ``format`` field on both the native and OpenAI endpoints.
        payload["format"] = schema_json
