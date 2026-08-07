"""OpenRouter provider adapter."""

from __future__ import annotations

from typing import Any, cast

import httpx

from vasuki.exceptions import ProviderError
from vasuki.providers.base import DEFAULT_MAX_OUTPUT_TOKENS
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
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
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

    @staticmethod
    def _error_reason(response: httpx.Response) -> str:
        reason = ""
        try:
            payload = response.json()
            error = payload.get("error", payload) if isinstance(payload, dict) else payload
            if isinstance(error, dict):
                reason = str(error.get("message") or error.get("code") or "")
            elif error:
                reason = str(error)
        except ValueError:
            reason = response.text.strip()
        return reason[:300] or response.reason_phrase or "Unknown OpenRouter error"

    async def validate_key(self) -> dict[str, Any]:
        """Validate the current bearer token using OpenRouter's authenticated key endpoint."""
        try:
            response = await self.client.get("key")
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not reach OpenRouter to validate the key: {exc}") from exc
        if response.status_code != 200:
            reason = self._error_reason(response)
            raise ProviderError(
                f"OpenRouter API key rejected (HTTP {response.status_code}): {reason}"
            )
        try:
            payload = response.json()
            data = payload["data"]
            if not isinstance(data, dict):
                raise TypeError("data is not an object")
            return cast(dict[str, Any], data)
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("OpenRouter returned malformed API key details") from exc

    async def list_models(self) -> list[dict[str, Any]]:
        """Return the complete OpenRouter text-model catalog."""
        try:
            response = await self.client.get("models")
        except httpx.HTTPError as exc:
            raise ProviderError(f"Could not fetch OpenRouter models: {exc}") from exc
        if response.status_code != 200:
            reason = self._error_reason(response)
            raise ProviderError(
                f"OpenRouter model lookup failed (HTTP {response.status_code}): {reason}"
            )
        try:
            payload = response.json()
            data = payload["data"]
            if not isinstance(data, list):
                raise TypeError("data is not a list")
            return [cast(dict[str, Any], item) for item in data if isinstance(item, dict)]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError("OpenRouter returned a malformed model catalog") from exc

    async def health_check(self) -> dict[str, object]:
        try:
            details = await self.validate_key()
            return {
                "healthy": True,
                "model": self.model,
                "key_label": str(details.get("label") or ""),
            }
        except ProviderError as exc:
            return {"healthy": False, "error": str(exc), "model": self.model}
