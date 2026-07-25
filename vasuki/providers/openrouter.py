"""OpenRouter provider adapter."""

import httpx

from vasuki.providers.openai_compatible import OpenAICompatibleProvider


class OpenRouterProvider(OpenAICompatibleProvider):
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://openrouter.ai/api/v1",
        application_name: str | None = None,
        referring_url: str | None = None,
        timeout: float = 120,
        max_retries: int = 2,
        max_output_tokens: int = 4096,
        features: list[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers: dict[str, str] = {}
        if application_name:
            headers["X-Title"] = application_name
        if referring_url:
            headers["HTTP-Referer"] = referring_url
        super().__init__(
            name="openrouter",
            base_url=base_url,
            api_key=api_key,
            model=model,
            extra_headers=headers,
            timeout=timeout,
            max_retries=max_retries,
            max_output_tokens=max_output_tokens,
            features=features,
            transport=transport,
        )
