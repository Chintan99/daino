"""Create provider adapters from validated configuration."""

from __future__ import annotations

from daino.config.models import ProviderConfig
from daino.providers.base import LLMProvider
from daino.providers.ollama import OllamaProvider
from daino.providers.openai_compatible import OpenAICompatibleProvider
from daino.providers.openrouter import OpenRouterProvider
from daino.providers.vllm import VLLMProvider
from daino.security.secrets import resolve_secret


def create_provider(name: str, config: ProviderConfig) -> LLMProvider:
    """Build a provider, resolving the configured secret reference."""
    return build_provider(
        name, config, api_key=resolve_secret(config.api_key) if config.api_key else ""
    )


def build_provider(name: str, config: ProviderConfig, *, api_key: str = "") -> LLMProvider:
    """Build a provider from an already-resolved key.

    Separate from :func:`create_provider` so a key that has not been stored yet —
    one typed into the GUI's provider form and being tested before saving — can
    be used without first writing it to disk.
    """
    if config.type == "openrouter":
        return OpenRouterProvider(
            base_url=config.base_url,
            api_key=api_key,
            model=config.model,
            timeout=config.timeout,
            max_retries=config.max_retries,
            concurrency=config.concurrency,
            max_output_tokens=config.max_output_tokens,
            features=config.features,
            application_name=config.application_name,
            referring_url=config.referring_url,
            reasoning_effort=config.reasoning_effort,
        )
    if config.type == "ollama":
        return OllamaProvider(
            base_url=config.base_url,
            api_key=api_key,
            model=config.model,
            timeout=config.timeout,
            max_retries=config.max_retries,
            concurrency=config.concurrency,
            max_output_tokens=config.max_output_tokens,
            features=config.features,
            reasoning_effort=config.reasoning_effort,
        )
    if config.type == "vllm":
        return VLLMProvider(
            base_url=config.base_url,
            api_key=api_key,
            model=config.model,
            timeout=config.timeout,
            max_retries=config.max_retries,
            concurrency=config.concurrency,
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
        reasoning_effort=config.reasoning_effort,
        concurrency=config.concurrency,
    )
