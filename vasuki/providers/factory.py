"""Create provider adapters from validated configuration."""

from __future__ import annotations

from vasuki.config.models import ProviderConfig
from vasuki.providers.base import LLMProvider
from vasuki.providers.openai_compatible import OpenAICompatibleProvider
from vasuki.providers.openrouter import OpenRouterProvider
from vasuki.providers.vllm import VLLMProvider
from vasuki.security.secrets import resolve_secret


def create_provider(name: str, config: ProviderConfig) -> LLMProvider:
    api_key = resolve_secret(config.api_key) if config.api_key else ""
    if config.type == "openrouter":
        return OpenRouterProvider(
            base_url=config.base_url,
            api_key=api_key,
            model=config.model,
            timeout=config.timeout,
            max_retries=config.max_retries,
            max_output_tokens=config.max_output_tokens,
            features=config.features,
            application_name=config.application_name,
            referring_url=config.referring_url,
        )
    if config.type == "vllm":
        return VLLMProvider(
            base_url=config.base_url,
            api_key=api_key,
            model=config.model,
            timeout=config.timeout,
            max_retries=config.max_retries,
            max_output_tokens=config.max_output_tokens,
            features=config.features,
        )
    return OpenAICompatibleProvider(
        name=name,
        base_url=config.base_url,
        api_key=api_key,
        model=config.model,
        timeout=config.timeout,
        max_retries=config.max_retries,
        max_output_tokens=config.max_output_tokens,
        features=config.features,
    )
