from __future__ import annotations

import httpx
import pytest

from vasuki.agents.loop import _detail
from vasuki.agents.tool_schemas import CHAT_TOOL_SPECS, tool_call_to_action
from vasuki.schemas import AgentAction, ToolCall
from vasuki.tools import ActionExecutor, EditTools, WebResearchTool


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
                text=(
                    '<a class="result__a" href="https://example.com/report">Report</a>'
                ),
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
    assert fetched.data["links"] == [
        {"text": "Appendix", "url": "https://example.com/appendix"}
    ]
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
