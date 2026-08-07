"""Local Ollama provider adapter."""

from __future__ import annotations

from typing import Any

import httpx

from vasuki.providers.base import DEFAULT_MAX_OUTPUT_TOKENS
from vasuki.providers.openai_compatible import OpenAICompatibleProvider


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
        transport: httpx.AsyncBaseTransport | None = None,
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
            transport=transport,
        )

    def _constrain_payload(
        self, payload: dict[str, Any], schema_json: dict[str, Any], schema_name: str
    ) -> None:
        # Ollama's grammar-constrained decoding accepts a full JSON Schema in
        # the top-level ``format`` field on both the native and OpenAI endpoints.
        payload["format"] = schema_json
