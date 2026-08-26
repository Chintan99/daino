"""Optional Playwright browser smoke verification."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class BrowserReport(BaseModel):
    passed: bool
    url: str
    status_code: int | None = None
    console_errors: list[str] = Field(default_factory=list)
    failed_requests: list[str] = Field(default_factory=list)
    screenshot: str | None = None
    error: str | None = None


class BrowserVerifier:
    """Opens a real browser, captures failures and a screenshot when Playwright is installed."""

    async def verify(
        self,
        url: str,
        *,
        artifact_dir: Path,
        selector: str | None = None,
        timeout_ms: int = 30_000,
    ) -> BrowserReport:
        try:
            from playwright.async_api import async_playwright  # type: ignore[import-not-found]
        except ImportError:
            return BrowserReport(
                passed=False,
                url=url,
                error="Install `daino[browser]` and run `playwright install chromium`",
            )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        screenshot = artifact_dir / "browser-verification.png"
        console_errors: list[str] = []
        failed_requests: list[str] = []
        try:
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(headless=True)
                page = await browser.new_page()
                page.on(
                    "console",
                    lambda message: (
                        console_errors.append(message.text) if message.type == "error" else None
                    ),
                )
                page.on(
                    "requestfailed",
                    lambda request: failed_requests.append(request.url),
                )
                response = await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                if selector:
                    await page.locator(selector).wait_for(timeout=timeout_ms)
                await page.screenshot(path=str(screenshot), full_page=True)
                await browser.close()
                status = response.status if response else None
                passed = bool(
                    response and response.ok and not console_errors and not failed_requests
                )
                return BrowserReport(
                    passed=passed,
                    url=url,
                    status_code=status,
                    console_errors=console_errors,
                    failed_requests=failed_requests,
                    screenshot=str(screenshot),
                )
        except Exception as exc:
            return BrowserReport(
                passed=False,
                url=url,
                console_errors=console_errors,
                failed_requests=failed_requests,
                error=str(exc),
            )
