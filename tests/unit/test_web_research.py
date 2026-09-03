from __future__ import annotations

import httpx
import pytest

from daino.agents.loop import _detail
from daino.agents.tool_schemas import CHAT_TOOL_SPECS, tool_call_to_action
from daino.schemas import AgentAction, ToolCall
from daino.tools import ActionExecutor, EditTools, WebResearchTool
from daino.tools.search import (
    SearchBackendConfig,
    SearchConfigurationError,
    build_backend,
)


async def public_dns(_: str) -> list[str]:
    return ["93.184.216.34"]


def tool_names() -> set[str]:
    return {str(spec["function"]["name"]) for spec in CHAT_TOOL_SPECS}


def test_research_actions_are_available_to_native_and_structured_models() -> None:
    assert {"web_search", "fetch_url"} <= tool_names()
    action = tool_call_to_action(
        ToolCall(
            id="call-1",
            name="fetch_url",
            arguments={"thought": "read source", "url": "https://example.com/docs"},
        )
    )

    assert action.action == "fetch_url"
    assert action.url == "https://example.com/docs"


@pytest.mark.asyncio
async def test_search_asks_for_network_and_extracts_results(tmp_path) -> None:
    asked: list[tuple[str, str]] = []

    async def approve(subject: str, reason: str) -> tuple[bool, bool]:
        asked.append((subject, reason))
        return True, False

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["q"] == "Python release notes"
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                '<a class="result__a" href="//duckduckgo.com/l/?uddg='
                'https%3A%2F%2Fdocs.python.org%2F3%2Fwhatsnew%2F">Python docs</a>'
                '<a class="result__snippet">Official release documentation.</a>'
            ),
        )

    web = WebResearchTool(
        approve=approve,
        transport=httpx.MockTransport(handler),
        resolver=public_dns,
    )
    executor = ActionExecutor(EditTools(tmp_path), web=web)

    result, paths = await executor.execute(
        AgentAction(
            thought="find current docs",
            action="web_search",
            query="Python release notes",
            max_results=3,
        )
    )

    assert result.success
    assert paths == []
    assert asked and "network" in asked[0][1]
    assert result.data["results"] == [
        {
            "title": "Python docs",
            "url": "https://docs.python.org/3/whatsnew/",
            "snippet": "Official release documentation.",
        }
    ]
    assert "UNTRUSTED WEB SEARCH" in _detail(
        AgentAction(thought="search", action="web_search", query="Python"), result
    )


@pytest.mark.asyncio
async def test_search_posts_a_form_query_with_a_browser_user_agent() -> None:
    """DuckDuckGo returns an empty challenge page to bare GETs / bot UAs.

    The tool must POST the query as form data with a browser-like User-Agent, or
    live searches silently come back with no results.
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["user_agent"] = request.headers.get("user-agent", "")
        seen["body"] = request.content.decode()
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text='<a class="result__a" href="https://example.com/hit">Hit</a>',
        )

    web = WebResearchTool(
        require_approval=False,
        transport=httpx.MockTransport(handler),
        resolver=public_dns,
    )
    result = await web.search("book cover images")

    assert result.success
    assert seen["method"] == "POST"
    assert "Mozilla" in str(seen["user_agent"])
    assert "q=book" in str(seen["body"])


@pytest.mark.asyncio
async def test_session_approval_covers_search_and_followup_fetch() -> None:
    asked: list[str] = []

    async def approve(subject: str, _: str) -> tuple[bool, bool]:
        asked.append(subject)
        return True, True

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.endswith("duckduckgo.com"):
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=('<a class="result__a" href="https://example.com/report">Report</a>'),
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=(
                "<html><head><title>Primary report</title><script>ignore me</script></head>"
                "<body><main><h1>Findings</h1><p>Verified information.</p>"
                '<a href="/appendix">Appendix</a></main></body></html>'
            ),
        )

    web = WebResearchTool(
        approve=approve,
        transport=httpx.MockTransport(handler),
        resolver=public_dns,
    )

    searched = await web.search("report")
    fetched = await web.fetch("https://example.com/report")

    assert searched.success and fetched.success
    assert len(asked) == 1
    assert fetched.data["title"] == "Primary report"
    assert "Verified information" in fetched.data["content"]
    assert "ignore me" not in fetched.data["content"]
    assert fetched.data["links"] == [{"text": "Appendix", "url": "https://example.com/appendix"}]
    detail = _detail(
        AgentAction(
            thought="read",
            action="fetch_url",
            url="https://example.com/report",
        ),
        fetched,
    )
    assert "UNTRUSTED WEB PAGE" in detail
    assert "Verified information" in detail


@pytest.mark.asyncio
async def test_declined_network_access_sends_no_request() -> None:
    requests: list[httpx.Request] = []

    async def decline(_: str, __: str) -> tuple[bool, bool]:
        return False, False

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text="must not run")

    web = WebResearchTool(
        approve=decline,
        transport=httpx.MockTransport(handler),
        resolver=public_dns,
    )

    result = await web.fetch("https://example.com")

    assert not result.success
    assert "declined" in (result.error or "")
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/admin",
        "http://127.0.0.1:8000/private",
        "http://[::1]/private",
        "http://169.254.169.254/latest/meta-data/",
        "https://user:password@example.com/private",
    ],
)
async def test_fetch_blocks_non_public_urls(url: str) -> None:
    web = WebResearchTool(require_approval=False, resolver=public_dns)

    result = await web.fetch(url)

    assert not result.success
    assert result.error


@pytest.mark.asyncio
async def test_dns_answers_cannot_point_at_a_private_address() -> None:
    async def private_dns(_: str) -> list[str]:
        return ["10.0.0.4"]

    web = WebResearchTool(require_approval=False, resolver=private_dns)

    result = await web.fetch("https://apparently-public.example/report")

    assert not result.success
    assert "private" in (result.error or "")


@pytest.mark.asyncio
async def test_redirects_are_revalidated_before_the_next_request() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    web = WebResearchTool(
        require_approval=False,
        transport=httpx.MockTransport(handler),
        resolver=public_dns,
    )

    result = await web.fetch("https://example.com/redirect")

    assert not result.success
    assert "private" in (result.error or "")
    assert requests == ["https://example.com/redirect"]


@pytest.mark.asyncio
async def test_fetch_rejects_binary_and_oversized_responses() -> None:
    def binary(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=b"binary",
        )

    binary_result = await WebResearchTool(
        require_approval=False,
        transport=httpx.MockTransport(binary),
        resolver=public_dns,
    ).fetch("https://example.com/archive")

    assert not binary_result.success
    assert "content type" in (binary_result.error or "")

    def huge(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"x" * 1_500_001,
        )

    huge_result = await WebResearchTool(
        require_approval=False,
        transport=httpx.MockTransport(huge),
        resolver=public_dns,
    ).fetch("https://example.com/huge")

    assert not huge_result.success
    assert "download limit" in (huge_result.error or "")


async def private_dns(_: str) -> list[str]:
    return ["127.0.0.1"]


def json_handler(expected: str, payload: dict[str, object], seen: list[httpx.Request]) -> object:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert str(request.url).startswith(expected)
        return httpx.Response(200, json=payload)

    return handler


@pytest.mark.asyncio
async def test_brave_backend_reads_the_documented_json_shape() -> None:
    seen: list[httpx.Request] = []
    payload = {
        "web": {
            "results": [
                {
                    "title": "Python 3.13 release notes",
                    "url": "https://docs.python.org/3/whatsnew/3.13.html",
                    "description": "What is new in 3.13.",
                }
            ]
        }
    }
    web = WebResearchTool(
        require_approval=False,
        transport=httpx.MockTransport(
            json_handler("https://api.search.brave.com", payload, seen)  # type: ignore[arg-type]
        ),
        resolver=public_dns,
        search_backend=SearchBackendConfig(provider="brave", api_key="token-123"),
    )

    result = await web.search("python 3.13")

    assert result.success
    assert result.data["provider"] == "brave"
    assert result.data["results"][0]["url"].endswith("3.13.html")
    assert seen[0].headers["X-Subscription-Token"] == "token-123"


@pytest.mark.asyncio
async def test_tavily_backend_posts_a_json_body() -> None:
    seen: list[httpx.Request] = []
    payload = {
        "results": [
            {"title": "Agent patterns", "url": "https://example.com/a", "content": "Snippet."}
        ]
    }
    web = WebResearchTool(
        require_approval=False,
        transport=httpx.MockTransport(
            json_handler("https://api.tavily.com", payload, seen)  # type: ignore[arg-type]
        ),
        resolver=public_dns,
        search_backend=SearchBackendConfig(provider="tavily", api_key="tvly-1"),
    )

    result = await web.search("agent patterns")

    assert result.success
    assert result.data["results"][0]["snippet"] == "Snippet."
    assert seen[0].method == "POST"
    assert b"tvly-1" in seen[0].content


@pytest.mark.asyncio
async def test_a_backend_missing_its_key_says_what_to_set() -> None:
    web = WebResearchTool(
        require_approval=False,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        resolver=public_dns,
        search_backend=SearchBackendConfig(provider="brave"),
    )

    result = await web.search("anything")

    assert not result.success
    assert "web.api_key" in (result.error or "")
    assert "web.provider: duckduckgo" in (result.error or "")


@pytest.mark.asyncio
async def test_searxng_may_reach_the_instance_the_user_configured() -> None:
    """The private-address block would otherwise make a self-hosted instance useless."""
    seen: list[httpx.Request] = []
    payload = {"results": [{"title": "Local hit", "url": "https://example.com/x", "content": "s"}]}
    web = WebResearchTool(
        require_approval=False,
        transport=httpx.MockTransport(
            json_handler("http://127.0.0.1:8888", payload, seen)  # type: ignore[arg-type]
        ),
        resolver=private_dns,
        search_backend=SearchBackendConfig(provider="searxng", base_url="http://127.0.0.1:8888"),
    )

    result = await web.search("local query")

    assert result.success
    assert result.data["results"][0]["title"] == "Local hit"


@pytest.mark.asyncio
async def test_the_searxng_allowance_does_not_extend_to_fetching() -> None:
    """Only the configured search endpoint is exempt — not the whole private network."""
    web = WebResearchTool(
        require_approval=False,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="secret")),
        resolver=private_dns,
        search_backend=SearchBackendConfig(provider="searxng", base_url="http://127.0.0.1:8888"),
    )

    result = await web.fetch("http://192.168.1.1/admin")

    assert not result.success
    assert "private network" in (result.error or "").casefold()


def test_an_unknown_provider_is_rejected_at_construction() -> None:
    with pytest.raises(SearchConfigurationError) as raised:
        build_backend(SearchBackendConfig(provider="askjeeves"))
    assert "duckduckgo" in str(raised.value)


def test_duckduckgo_remains_the_keyless_default() -> None:
    backend = build_backend(None)
    assert backend.name == "duckduckgo"
    assert backend.unavailable() == ""
