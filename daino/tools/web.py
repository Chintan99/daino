"""Approval-gated, SSRF-resistant web research for the chat agent."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from html.parser import HTMLParser
from time import monotonic
from typing import Protocol, runtime_checkable
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx

from daino.schemas import ToolResult
from daino.tools.commands import ApprovalCallback

DEFAULT_RESULTS = 5
MAX_RESULTS = 10
DEFAULT_CONTENT_CHARS = 12_000
MAX_CONTENT_CHARS = 24_000
MAX_DOWNLOAD_BYTES = 1_500_000
MAX_REDIRECTS = 5
REQUEST_TIMEOUT_SECONDS = 20.0

_SEARCH_ENDPOINTS = (
    "https://html.duckduckgo.com/html/",
    "https://lite.duckduckgo.com/lite/",
)
_TEXT_CONTENT_TYPES = (
    "text/",
    "application/json",
    "application/ld+json",
    "application/xml",
    "application/xhtml+xml",
)
_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain"})
_USER_AGENT = "Daino/0.4 (+local coding-agent web research)"
#: DuckDuckGo's HTML/Lite endpoints return an empty challenge page to obvious bot
#: User-Agents. A browser-like UA (used only for the search request) is what makes
#: those endpoints actually return result links.
_SEARCH_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

Resolver = Callable[[str], Awaitable[list[str]]]


async def _resolve_host(host: str) -> list[str]:
    def resolve() -> list[str]:
        return sorted(
            {str(item[4][0]) for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
        )

    return await asyncio.to_thread(resolve)


def _address_is_public(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return address.is_global


async def _validated_url(url: str, resolver: Resolver) -> str:
    if len(url) > 2_048:
        raise ValueError("URL is too long")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http and https URLs are allowed")
    if not parsed.hostname:
        raise ValueError("URL has no hostname")
    if parsed.username or parsed.password:
        raise ValueError("Credentials in URLs are not allowed")
    host = parsed.hostname.rstrip(".").casefold()
    if host in _BLOCKED_HOSTNAMES or host.endswith((".localhost", ".local")):
        raise ValueError("Local and private network URLs are blocked")
    try:
        direct = ipaddress.ip_address(host)
    except ValueError:
        try:
            addresses = await resolver(host)
        except OSError as exc:
            raise ValueError(f"Could not resolve {host}: {exc}") from exc
        if not addresses:
            raise ValueError(f"Could not resolve {host}") from None
        if any(not _address_is_public(address) for address in addresses):
            raise ValueError("Local and private network URLs are blocked") from None
    else:
        if not direct.is_global:
            raise ValueError("Local and private network URLs are blocked")
    return url


class _ReadableHTML(HTMLParser):
    """Extract readable text, a title, and a small set of useful links."""

    BLOCKS = frozenset(
        {
            "article",
            "blockquote",
            "br",
            "div",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "li",
            "main",
            "p",
            "pre",
            "section",
            "table",
            "tr",
        }
    )
    IGNORED = frozenset({"script", "style", "noscript", "svg", "canvas", "template"})

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.parts: list[str] = []
        self.title_parts: list[str] = []
        self.links: list[dict[str, str]] = []
        self._ignored_depth = 0
        self._in_title = False
        self._link_url = ""
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in self.IGNORED:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag == "title":
            self._in_title = True
        if tag == "a":
            href = dict(attrs).get("href") or ""
            self._link_url = urljoin(self.base_url, href)
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self._ignored_depth:
            if tag in self.IGNORED:
                self._ignored_depth -= 1
            return
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._link_url:
            text = _one_line(" ".join(self._link_text))
            parsed = urlparse(self._link_url)
            if text and parsed.scheme in {"http", "https"} and len(self.links) < 30:
                self.links.append({"text": text[:160], "url": self._link_url})
            self._link_url = ""
            self._link_text = []
        if tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
        if self._link_url:
            self._link_text.append(data)
        self.parts.append(data)

    def result(self, limit: int) -> tuple[str, str, list[dict[str, str]]]:
        title = _one_line(" ".join(self.title_parts))
        lines = [_one_line(line) for line in "".join(self.parts).splitlines()]
        text = "\n".join(line for line in lines if line)
        return title, text[:limit], self.links


class _SearchHTML(HTMLParser):
    """Extract DuckDuckGo HTML/Lite result links without a browser dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._href = ""
        self._title: list[str] = []
        self._capture_title = False
        self._capture_snippet = False
        self._snippet: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if tag == "a" and classes & {"result__a", "result-link"}:
            self._capture_title = True
            self._href = values.get("href") or ""
            self._title = []
        if classes & {"result__snippet", "result-snippet"}:
            self._capture_snippet = True
            self._snippet = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._capture_title:
            title = _one_line(" ".join(self._title))
            url = _search_result_url(self._href)
            if title and url:
                self.results.append({"title": title[:240], "url": url, "snippet": ""})
            self._capture_title = False
        if self._capture_snippet and tag in {"a", "div", "td", "span"}:
            if self.results:
                self.results[-1]["snippet"] = _one_line(" ".join(self._snippet))[:500]
            self._capture_snippet = False

    def handle_data(self, data: str) -> None:
        if self._capture_title:
            self._title.append(data)
        if self._capture_snippet:
            self._snippet.append(data)


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _search_result_url(value: str) -> str:
    if not value:
        return ""
    if value.startswith("//"):
        value = "https:" + value
    parsed = urlparse(value)
    if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
        redirected = parse_qs(parsed.query).get("uddg", [])
        if redirected:
            value = unquote(redirected[0])
            parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} else ""


@runtime_checkable
class WebResearch(Protocol):
    """What a caller needs from web research: search, and fetch.

    Named as a protocol so a workspace can wrap the real tool to record its
    sources without subclassing it — the SSRF checks, redirect revalidation and
    byte ceilings below stay the single implementation everything goes through.
    """

    async def search(self, query: str, *, max_results: int = ...) -> ToolResult: ...

    async def fetch(self, url: str, *, max_chars: int = ...) -> ToolResult: ...


class WebResearchTool:
    """Search and fetch public web content after the active mode allows it."""

    def __init__(
        self,
        *,
        approve: ApprovalCallback | None = None,
        require_approval: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.approve = approve
        self.require_approval = require_approval
        self.transport = transport
        self.resolver = resolver or _resolve_host
        self._approved_for_session = not require_approval

    async def _authorized(self, subject: str) -> str:
        if self._approved_for_session:
            return ""
        if self.approve is None:
            return "Internet access needs approval, but no approver is attached."
        approved, remember = await self.approve(
            subject,
            "internet research requires network access",
        )
        if not approved:
            return "The user declined internet access."
        if remember:
            self._approved_for_session = True
        return ""

    async def search(self, query: str, *, max_results: int = DEFAULT_RESULTS) -> ToolResult:
        started = monotonic()
        query = _one_line(query)
        if not query:
            return ToolResult(tool="web_search", success=False, error="Search query is empty.")
        denied = await self._authorized(f"Search the web for: {query[:160]}")
        if denied:
            return ToolResult(tool="web_search", success=False, error=denied)
        count = min(max(max_results or DEFAULT_RESULTS, 1), MAX_RESULTS)
        last_error = (
            "Web search returned no results — the search backend may be rate-limiting or "
            "blocking automated queries. If you already know a source URL, use fetch_url on it "
            "directly instead of searching."
        )
        for endpoint in _SEARCH_ENDPOINTS:
            try:
                # DuckDuckGo's HTML/Lite endpoints expect a POSTed form query from a
                # browser-like client; a bare GET returns an empty challenge page. The
                # query is also kept in the URL so mocked transports and logs see it.
                _, body, _ = await self._get(
                    endpoint,
                    params={"q": query},
                    method="POST",
                    data={"q": query, "kl": "wt-wt"},
                    extra_headers={
                        "User-Agent": _SEARCH_USER_AGENT,
                        "Referer": endpoint,
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                )
                parser = _SearchHTML()
                parser.feed(body)
                results = _deduplicate(parser.results)[:count]
                if results:
                    return ToolResult(
                        tool="web_search",
                        success=True,
                        data={"query": query, "results": results},
                        duration_seconds=monotonic() - started,
                    )
            except (httpx.HTTPError, ValueError) as exc:
                last_error = str(exc)
        return ToolResult(
            tool="web_search",
            success=False,
            error=last_error,
            duration_seconds=monotonic() - started,
        )

    async def fetch(self, url: str, *, max_chars: int = DEFAULT_CONTENT_CHARS) -> ToolResult:
        started = monotonic()
        denied = await self._authorized(f"Fetch web page: {url[:180]}")
        if denied:
            return ToolResult(tool="fetch_url", success=False, error=denied)
        limit = min(max(max_chars or DEFAULT_CONTENT_CHARS, 1_000), MAX_CONTENT_CHARS)
        try:
            final_url, body, content_type = await self._get(url)
            if "html" in content_type or body.lstrip().startswith(("<!DOCTYPE", "<html")):
                parser = _ReadableHTML(final_url)
                parser.feed(body)
                title, content, links = parser.result(limit)
            else:
                title, content, links = "", body[:limit], []
            if not content.strip():
                raise ValueError("The page contained no readable text")
            return ToolResult(
                tool="fetch_url",
                success=True,
                data={
                    "url": final_url,
                    "title": title,
                    "content": content,
                    "links": links,
                },
                duration_seconds=monotonic() - started,
            )
        except (httpx.HTTPError, ValueError) as exc:
            return ToolResult(
                tool="fetch_url",
                success=False,
                error=str(exc),
                duration_seconds=monotonic() - started,
            )

    async def _get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        method: str = "GET",
        data: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[str, str, str]:
        current = str(httpx.URL(url, params=params))
        headers = {"User-Agent": _USER_AGENT, "Accept": "text/html,text/plain,application/json"}
        if extra_headers:
            headers.update(extra_headers)
        async with httpx.AsyncClient(
            transport=self.transport,
            follow_redirects=False,
            timeout=REQUEST_TIMEOUT_SECONDS,
            trust_env=False,
            headers=headers,
        ) as client:
            hop_method, hop_data = method.upper(), data
            for _ in range(MAX_REDIRECTS + 1):
                await _validated_url(current, self.resolver)
                async with client.stream(hop_method, current, data=hop_data) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise ValueError("Redirect response had no destination")
                        current = urljoin(current, location)
                        # Follow redirects as a bodyless GET, per normal HTTP behaviour.
                        hop_method, hop_data = "GET", None
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").casefold()
                    media_type = content_type.split(";", 1)[0].strip()
                    if media_type and not media_type.startswith(_TEXT_CONTENT_TYPES):
                        raise ValueError(f"Unsupported web content type: {media_type}")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise ValueError("Web response exceeded the download limit")
                        chunks.append(chunk)
                    encoding = response.encoding or "utf-8"
                    return current, b"".join(chunks).decode(encoding, errors="replace"), media_type
            raise ValueError("Web page redirected too many times")


def _deduplicate(results: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for result in results:
        url = result["url"]
        if url in seen:
            continue
        seen.add(url)
        unique.append(result)
    return unique
