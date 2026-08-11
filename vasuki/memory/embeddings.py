"""Embedding abstraction used only after inexpensive metadata filtering."""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Protocol

import httpx


class EmbeddingProvider(Protocol):
    name: str
    model: str

    def embed(self, text: str) -> list[float] | None:
        """Return one vector, or ``None`` when semantic retrieval is unavailable."""
        ...


class DisabledEmbeddingProvider:
    name = "disabled"
    model = ""

    def embed(self, text: str) -> None:
        return None


class CallableEmbeddingProvider:
    """Adapter for a local model or OpenAI-compatible client supplied by the host."""

    def __init__(
        self,
        function: Callable[[str], list[float]],
        *,
        name: str = "local",
        model: str = "",
    ) -> None:
        self.function = function
        self.name = name
        self.model = model

    def embed(self, text: str) -> list[float] | None:
        vector = self.function(text)
        if not vector or not all(math.isfinite(item) for item in vector):
            return None
        return [float(item) for item in vector]


class OpenAICompatibleEmbeddingProvider:
    """Synchronous adapter for local or remote OpenAI-compatible embeddings."""

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str = "",
        name: str = "openai-compatible",
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.name = name
        self.timeout = timeout

    def embed(self, text: str) -> list[float] | None:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        response = httpx.post(
            f"{self.base_url}/embeddings",
            json={"model": self.model, "input": text},
            headers=headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        vector = data[0].get("embedding") if isinstance(data, list) and data else None
        if not isinstance(vector, list) or not vector:
            return None
        converted = [float(item) for item in vector]
        return converted if all(math.isfinite(item) for item in converted) else None


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        return 0.0
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(item * item for item in left))
    right_norm = math.sqrt(sum(item * item for item in right))
    if not left_norm or not right_norm:
        return 0.0
    return max(-1.0, min(1.0, numerator / (left_norm * right_norm)))
