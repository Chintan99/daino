"""Research inside a workspace: every page read is recorded as a source.

The web tool already fetches safely; what it does not do is remember. Citation
today is prompt instruction only — the model is asked to cite and may or may
not — and nothing in the product can show a reader which pages a claim rests on.

This wrapper closes that gap without asking the model to do anything: a fetch
that succeeds in a workspace becomes a row in the Sources panel and a cached
copy of the text on disk. A bibliography that depends on the model's diligence
is a bibliography with holes in it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from daino.schemas import ToolResult
from daino.tools.web import WebResearchTool

if TYPE_CHECKING:
    from daino.workbench.service import WorkbenchService


class SourceRecordingWeb:
    """A :class:`WebResearchTool` that files what it reads.

    Deliberately a wrapper rather than a subclass: the security-critical fetch
    path — the SSRF checks, the redirect revalidation, the byte ceilings — is
    untouched and stays the single implementation everything uses.
    """

    def __init__(
        self,
        inner: WebResearchTool,
        *,
        workbench: WorkbenchService | None = None,
        workspace_id: str = "",
    ) -> None:
        self.inner = inner
        self.workbench = workbench
        self.workspace_id = workspace_id

    async def search(self, query: str, *, max_results: int = 5) -> ToolResult:
        """Searching is not reading; only a fetched page becomes a source."""
        return await self.inner.search(query, max_results=max_results)

    async def fetch(self, url: str, *, max_chars: int = 12_000) -> ToolResult:
        result = await self.inner.fetch(url, max_chars=max_chars)
        self._record(result)
        return result

    def _record(self, result: ToolResult) -> None:
        if not result.success or self.workbench is None or not self.workspace_id:
            return
        data: dict[str, Any] = result.data or {}
        url = str(data.get("url", "")).strip()
        if not url:
            return
        text = str(data.get("content", ""))
        try:
            self.workbench.record_source(
                self.workspace_id,
                url=url,
                title=str(data.get("title", "")),
                snippet=" ".join(text.split())[:300],
                text=text,
            )
        except Exception:  # noqa: BLE001 - bookkeeping must never fail a turn
            # The page was fetched and the agent has it; failing to file it is a
            # missing bibliography entry, not a failed research step.
            return
