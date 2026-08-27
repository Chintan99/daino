"""Local vLLM provider adapter."""

from __future__ import annotations

from typing import Any

import httpx

from daino.providers.base import DEFAULT_MAX_OUTPUT_TOKENS
from daino.providers.openai_compatible import OpenAICompatibleProvider


class VLLMProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://127.0.0.1:8000/v1",
        api_key: str = "",
        timeout: float = 120,
        max_retries: int = 2,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        features: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        concurrency: int = 0,
    ) -> None:
        super().__init__(
            name="vllm",
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=timeout,
            max_retries=max_retries,
            max_output_tokens=max_output_tokens,
            features=features,
            transport=transport,
            concurrency=concurrency,
        )

    def _constrain_payload(
        self, payload: dict[str, Any], schema_json: dict[str, Any], schema_name: str
    ) -> None:
        # vLLM's guided decoding constrains generation to the schema server-side.
        # Servers new enough to have dropped guided_* parameters reject this
        # request, and the caller retries once without the constraint.
        payload["guided_json"] = schema_json
