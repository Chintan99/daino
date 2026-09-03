"""Where web search results come from.

Search was one hardcoded route: scrape DuckDuckGo's HTML endpoint with a
browser User-Agent, because the real one returns an empty challenge page to an
honest client. That works until it doesn't — the markup changes, the endpoint
rate-limits, the challenge tightens — and when it stops working the agent simply
stops being able to look anything up, with no configuration that could fix it.

So the route becomes a choice. The scraper stays as the default, because it is
the only one that needs no account and a local-first tool should work out of the
box. Alongside it are four backends that are *contracts* rather than scrapes:

* **Brave Search API** — a paid key, a documented JSON response.
* **Tavily** — an answer-oriented search API built for agents.
* **SearXNG** — self-hosted metasearch, so the queries never leave the network.
* **Google Programmable Search** — a key plus an engine id.

Every backend returns the same ``{title, url, snippet}`` shape and goes through
the caller's fetcher, so the SSRF validation, redirect revalidation, byte ceiling
and content-type check in :mod:`daino.tools.web` remain the single implementation
that all network access passes through. A backend here decides *what to ask*; it
never decides what is safe to connect to.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

#: Signature of the caller's HTTP getter. Returns ``(final_url, body, media_type)``.
Fetcher = Callable[..., Awaitable[tuple[str, str, str]]]

#: Result cap any backend may be asked for, mirroring ``web.MAX_RESULTS``.
MAX_RESULTS = 10

#: The plain client identity. Only the DuckDuckGo scraper overrides it, and only
#: because that endpoint refuses to answer anything that identifies itself.
USER_AGENT = "Daino/0.4 (+local coding-agent web research)"

#: DuckDuckGo's HTML/Lite endpoints return an empty challenge page to obvious bot
#: User-Agents. A browser-like UA (used only for the search request) is what makes
#: those endpoints actually return result links.
SEARCH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

DUCKDUCKGO_ENDPOINTS = (
    "https://html.duckduckgo.com/html/",
    "https://lite.duckduckgo.com/lite/",
)


class SearchConfigurationError(ValueError):
    """A backend was selected without the credentials or URL it needs."""


@dataclass(frozen=True, slots=True)
class SearchBackendConfig:
    """Resolved search settings: the provider, and whatever it needs to run."""

    provider: str = "duckduckgo"
    #: Already resolved from its ``env://``/``keyring://`` reference by the caller.
    api_key: str = ""
    #: SearXNG instance root, or an override for an API-compatible proxy.
    base_url: str = ""
    #: Google Programmable Search engine id (``cx``).
    engine_id: str = ""


class SearchBackend(ABC):
    """One way of turning a query into ``{title, url, snippet}`` rows."""

    #: Configuration value that selects this backend.
    name: str = ""
    #: Shown when the backend cannot run, so the user is told what to set.
    requirement: str = ""

    def __init__(self, config: SearchBackendConfig) -> None:
        self.config = config

    def unavailable(self) -> str:
        """Why this backend cannot run, or ``""`` when it can."""
        return ""

    def private_hosts(self) -> frozenset[str]:
        """Hosts this backend is allowed to reach despite the private-address block.

        Empty for every hosted backend. A self-hosted SearXNG is the one case
        where the user has deliberately pointed Daino at their own network, and
        refusing the host they configured would make the option useless — so the
        exemption is granted, narrowly, to exactly that hostname and nothing else.
        """
        return frozenset()

    @abstractmethod
    async def search(self, query: str, count: int, fetch: Fetcher) -> list[dict[str, str]]: ...


class DuckDuckGoBackend(SearchBackend):
    """Scrape the HTML/Lite endpoints. No account, and no stability guarantee."""

    name = "duckduckgo"
    requirement = ""

    async def search(self, query: str, count: int, fetch: Fetcher) -> list[dict[str, str]]:
        from daino.tools.web import parse_duckduckgo_results

        last_error: Exception | None = None
        for endpoint in DUCKDUCKGO_ENDPOINTS:
            try:
                # These endpoints expect a POSTed form query from a browser-like
                # client; a bare GET returns an empty challenge page. The query is
                # also kept in the URL so mocked transports and logs see it.
                _, body, _ = await fetch(
                    endpoint,
                    params={"q": query},
                    method="POST",
                    data={"q": query, "kl": "wt-wt"},
                    extra_headers={
                        "User-Agent": SEARCH_USER_AGENT,
                        "Referer": endpoint,
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                )
            except Exception as exc:  # noqa: BLE001 - try the other endpoint first
                last_error = exc
                continue
            results = parse_duckduckgo_results(body)[:count]
            if results:
                return results
        if last_error is not None:
            raise last_error
        return []


class BraveBackend(SearchBackend):
    """Brave Search API — a documented JSON contract behind a subscription key."""

    name = "brave"
    requirement = "web.api_key must reference a Brave Search subscription token"
    endpoint = "https://api.search.brave.com/res/v1/web/search"

    def unavailable(self) -> str:
        return "" if self.config.api_key else self.requirement

    async def search(self, query: str, count: int, fetch: Fetcher) -> list[dict[str, str]]:
        _, body, _ = await fetch(
            self.config.base_url or self.endpoint,
            params={"q": query, "count": str(count)},
            extra_headers={
                "Accept": "application/json",
                "X-Subscription-Token": self.config.api_key,
            },
        )
        payload = _json(body)
        rows = _dig(payload, "web", "results") or []
        return [
            {
                "title": _text(row.get("title")),
                "url": _text(row.get("url")),
                "snippet": _text(row.get("description")),
            }
            for row in rows
            if isinstance(row, dict) and row.get("url")
        ][:count]


class TavilyBackend(SearchBackend):
    """Tavily — a search API built for agents, so snippets are already extracted."""

    name = "tavily"
    requirement = "web.api_key must reference a Tavily API key"
    endpoint = "https://api.tavily.com/search"

    def unavailable(self) -> str:
        return "" if self.config.api_key else self.requirement

    async def search(self, query: str, count: int, fetch: Fetcher) -> list[dict[str, str]]:
        _, body, _ = await fetch(
            self.config.base_url or self.endpoint,
            method="POST",
            json_body={
                "api_key": self.config.api_key,
                "query": query,
                "max_results": count,
                "search_depth": "basic",
            },
            extra_headers={"Accept": "application/json"},
        )
        payload = _json(body)
        rows = payload.get("results") or []
        return [
            {
                "title": _text(row.get("title")),
                "url": _text(row.get("url")),
                "snippet": _text(row.get("content")),
            }
            for row in rows
            if isinstance(row, dict) and row.get("url")
        ][:count]


class SearXNGBackend(SearchBackend):
    """A self-hosted metasearch instance, so queries stay inside the network."""

    name = "searxng"
    requirement = "web.base_url must point at a SearXNG instance"

    def unavailable(self) -> str:
        return "" if self.config.base_url else self.requirement

    def private_hosts(self) -> frozenset[str]:
        host = urlparse(self.config.base_url).hostname
        return frozenset({host.rstrip(".").casefold()}) if host else frozenset()

    async def search(self, query: str, count: int, fetch: Fetcher) -> list[dict[str, str]]:
        root = self.config.base_url.rstrip("/")
        _, body, _ = await fetch(
            f"{root}/search",
            params={"q": query, "format": "json"},
            extra_headers={"Accept": "application/json"},
        )
        payload = _json(body)
        rows = payload.get("results") or []
        return [
            {
                "title": _text(row.get("title")),
                "url": _text(row.get("url")),
                "snippet": _text(row.get("content")),
            }
            for row in rows
            if isinstance(row, dict) and row.get("url")
        ][:count]


class GoogleProgrammableSearchBackend(SearchBackend):
    """Google Programmable Search — an API key plus a configured engine id."""

    name = "google-pse"
    requirement = "web.api_key and web.engine_id are both required for google-pse"
    endpoint = "https://www.googleapis.com/customsearch/v1"

    def unavailable(self) -> str:
        if not self.config.api_key or not self.config.engine_id:
            return self.requirement
        return ""

    async def search(self, query: str, count: int, fetch: Fetcher) -> list[dict[str, str]]:
        _, body, _ = await fetch(
            self.config.base_url or self.endpoint,
            params={
                "key": self.config.api_key,
                "cx": self.config.engine_id,
                "q": query,
                # The API caps a single page at ten, which is also MAX_RESULTS.
                "num": str(min(count, 10)),
            },
            extra_headers={"Accept": "application/json"},
        )
        payload = _json(body)
        rows = payload.get("items") or []
        return [
            {
                "title": _text(row.get("title")),
                "url": _text(row.get("link")),
                "snippet": _text(row.get("snippet")),
            }
            for row in rows
            if isinstance(row, dict) and row.get("link")
        ][:count]


BACKENDS: dict[str, type[SearchBackend]] = {
    DuckDuckGoBackend.name: DuckDuckGoBackend,
    BraveBackend.name: BraveBackend,
    TavilyBackend.name: TavilyBackend,
    SearXNGBackend.name: SearXNGBackend,
    GoogleProgrammableSearchBackend.name: GoogleProgrammableSearchBackend,
}


def resolved_search_config(settings: Any) -> SearchBackendConfig:
    """Turn a ``WebSearchConfig`` into a backend config, resolving its secret.

    Takes the settings object loosely rather than importing the config model, so
    this module stays a leaf: the tools package is imported by the config-free
    parts of the agent, and a cycle back into configuration would be felt there.
    """
    from daino.security.secrets import resolve_secret

    reference = getattr(settings, "api_key", "") or ""
    return SearchBackendConfig(
        provider=getattr(settings, "provider", "duckduckgo") or "duckduckgo",
        api_key=resolve_secret(reference) if reference else "",
        base_url=getattr(settings, "base_url", "") or "",
        engine_id=getattr(settings, "engine_id", "") or "",
    )


def build_backend(config: SearchBackendConfig | None) -> SearchBackend:
    """Resolve configuration to a backend, defaulting to the keyless scraper."""
    resolved = config or SearchBackendConfig()
    backend_type = BACKENDS.get(resolved.provider)
    if backend_type is None:
        raise SearchConfigurationError(
            f"Unknown web search provider {resolved.provider!r}. "
            f"Choose one of: {', '.join(sorted(BACKENDS))}."
        )
    return backend_type(resolved)


def _json(body: str) -> dict[str, Any]:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"The search backend returned non-JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("The search backend returned an unexpected JSON shape")
    return payload


def _dig(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _text(value: Any) -> str:
    return " ".join(str(value).split()) if value else ""
