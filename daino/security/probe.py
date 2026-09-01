"""Non-destructive probing of a running application.

Static analysis cannot see what a deployment actually returns: whether the
session cookie is ``HttpOnly``, whether ``/.env`` is served, whether an
unhandled route prints a stack trace. This module answers those questions by
talking to the app the user started in the Inspector's Live view, using only
``GET``, ``HEAD``, and ``OPTIONS``. It never sends a payload, never mutates
state, and never follows a redirect off the target's own origin.

Two guardrails are structural rather than advisory:

* **Loopback by default.** A target that is not loopback or private address
  space is refused unless the caller passes ``authorized=True``, which the GUI
  only sets after the user confirms they own the host. Scanning someone else's
  server is not a feature.
* **Bounded work.** A fixed path list, one request each, short timeouts, and no
  fuzzing. This is a pre-push sanity check, not a scanner.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import httpx

from daino.schemas import QAFinding, QASeverity

#: Per-request ceiling. A dev server that hangs must not hold up the report.
REQUEST_TIMEOUT_SECONDS = 6.0
#: Ceiling for the whole probe, so a slow target cannot stall an inspection.
TOTAL_TIMEOUT_SECONDS = 90.0
#: How many probe requests may be in flight at once against a dev server.
CONCURRENCY = 4


@dataclass(frozen=True, slots=True)
class ExposurePath:
    """A path whose presence is itself the finding."""

    path: str
    title: str
    severity: QASeverity
    cwe: str
    remediation: str
    #: Text that must appear in the body before the path counts as exposed.
    #: Single-page apps answer 200 with index.html for everything.
    signature: re.Pattern[str] | None = None


EXPOSED_PATHS: tuple[ExposurePath, ...] = (
    ExposurePath(
        path="/.env",
        title="Environment file served over HTTP",
        severity="critical",
        cwe="CWE-538",
        remediation="Stop serving dotfiles from the web root and rotate every value in the file.",
        signature=re.compile(r"^\s*[A-Z_][A-Z0-9_]*\s*=", re.MULTILINE),
    ),
    ExposurePath(
        path="/.git/config",
        title="Git metadata served over HTTP",
        severity="critical",
        cwe="CWE-527",
        remediation="Block /.git from the web server; the whole source history is downloadable.",
        signature=re.compile(r"\[core\]|repositoryformatversion"),
    ),
    ExposurePath(
        path="/.aws/credentials",
        title="Cloud credentials file served over HTTP",
        severity="critical",
        cwe="CWE-538",
        remediation="Remove the file from the served directory and rotate the credentials.",
        signature=re.compile(r"aws_access_key_id", re.IGNORECASE),
    ),
    ExposurePath(
        path="/id_rsa",
        title="Private key served over HTTP",
        severity="critical",
        cwe="CWE-538",
        remediation="Remove the key from the served directory and rotate it.",
        signature=re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"),
    ),
    ExposurePath(
        path="/actuator/env",
        title="Spring Actuator environment endpoint exposed",
        severity="high",
        cwe="CWE-200",
        remediation="Restrict actuator endpoints to an internal port with authentication.",
        signature=re.compile(r"propertySources|activeProfiles"),
    ),
    ExposurePath(
        path="/debug/pprof/",
        title="Go profiling endpoint exposed",
        severity="high",
        cwe="CWE-200",
        remediation="Register pprof only on an internal listener.",
        signature=re.compile(r"pprof|goroutine", re.IGNORECASE),
    ),
    ExposurePath(
        path="/server-status",
        title="Web server status page exposed",
        severity="medium",
        cwe="CWE-200",
        remediation="Restrict the status handler to localhost.",
        signature=re.compile(r"Apache Server Status|Server uptime"),
    ),
    ExposurePath(
        path="/.DS_Store",
        title="macOS directory index served over HTTP",
        severity="low",
        cwe="CWE-527",
        remediation="Exclude .DS_Store from deployment artifacts.",
        signature=re.compile(r"Bud1"),
    ),
    ExposurePath(
        path="/openapi.json",
        title="API schema published without authentication",
        severity="info",
        cwe="CWE-200",
        remediation=(
            "Confirm the published schema is meant to be public; it is a complete map of the "
            "API's attack surface."
        ),
        signature=re.compile(r'"openapi"\s*:|"swagger"\s*:'),
    ),
)

#: Response headers a production deployment is expected to set, and what their
#: absence actually costs.
_HEADER_RULES: tuple[tuple[str, str, QASeverity, str, str], ...] = (
    (
        "content-security-policy",
        "No Content-Security-Policy header",
        "medium",
        "CWE-693",
        "Add a CSP; it is the control that turns an injected script into a blocked script.",
    ),
    (
        "x-content-type-options",
        "No X-Content-Type-Options header",
        "low",
        "CWE-693",
        "Send `X-Content-Type-Options: nosniff` so uploads cannot be re-interpreted as scripts.",
    ),
    (
        "referrer-policy",
        "No Referrer-Policy header",
        "low",
        "CWE-200",
        "Send `Referrer-Policy: strict-origin-when-cross-origin` to keep URLs out of third-party "
        "logs.",
    ),
)

#: Headers that tell an attacker exactly what to look up.
_DISCLOSING_HEADERS = ("server", "x-powered-by", "x-aspnet-version", "x-generator")

_STACK_TRACE = re.compile(
    r"Traceback \(most recent call last\)|Werkzeug Debugger|"
    r"at [a-z]+\.[a-z]+\.[A-Za-z]+\(|"
    r"org\.springframework|System\.NullReferenceException|"
    r"<b>Fatal error</b>|Whoops, looks like something went wrong",
)
_DIRECTORY_LISTING = re.compile(r"<title>Index of /|Directory listing for /")

#: An origin that cannot exist, so reflecting it proves the policy is unbounded.
_PROBE_ORIGIN = "https://inspector-probe.invalid"


def target_is_local(url: str) -> bool:
    """Whether a URL points at this machine or private address space.

    Hostname resolution is part of the answer: ``app.local`` pointing at a LAN
    address is fine, and a public name that happens to end in ``.dev`` is not.
    A name that does not resolve is treated as non-local, because the safe
    failure here is to ask the user rather than to probe.
    """
    host = urlparse(url).hostname
    if not host:
        return False
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:  # noqa: S104 - comparison, not a bind
        return True
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError, ValueError):
        return False
    if not addresses:
        return False
    for address in addresses:
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return False
        if not (parsed.is_loopback or parsed.is_private or parsed.is_link_local):
            return False
    return True


async def probe_target(
    url: str,
    *,
    authorized: bool = False,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> tuple[list[QAFinding], str]:
    """Probe a running application and return ``(findings, evidence log)``.

    ``authorized`` is the caller's assertion that the user owns the target. It
    is only consulted for non-local hosts; a loopback target never needs it.
    """
    target = _normalise(url)
    if not target:
        return [], f"Not a usable target URL: {url!r}"
    if not target_is_local(target) and not authorized:
        return [], (
            f"Refused to probe {target}: it is not a loopback or private-network address. "
            "Live probing is limited to hosts you have confirmed you own."
        )
    log: list[str] = [f"Target: {target}"]
    findings: list[QAFinding] = []
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            verify=True,
            headers={"User-Agent": "Daino-Inspector/1.0 (non-destructive pre-release check)"},
        ) as client:
            async with asyncio.timeout(TOTAL_TIMEOUT_SECONDS):
                root = await _get(client, target, log)
                if root is None:
                    return [], "\n".join(
                        [*log, "The target did not answer; start the app before probing it."]
                    )
                findings.extend(_check_headers(target, root))
                findings.extend(_check_cookies(target, root))
                findings.extend(_check_body(target, root))
                findings.extend(await _check_error_handling(client, target, log))
                findings.extend(await _check_cors(client, target, log))
                findings.extend(await _check_methods(client, target, log))
                findings.extend(await _check_exposed_paths(client, target, log))
    except TimeoutError:
        log.append(f"Probe stopped after {TOTAL_TIMEOUT_SECONDS:.0f}s; partial results reported.")
    except httpx.HTTPError as exc:
        log.append(f"Probe error: {type(exc).__name__}: {exc}")
    if not findings:
        log.append("No live-traffic weaknesses were observed on the probed surface.")
    return findings, "\n".join(log)


# ------------------------------------------------------------------ probes


def _check_headers(target: str, response: httpx.Response) -> list[QAFinding]:
    findings: list[QAFinding] = []
    headers = response.headers
    for name, title, severity, cwe, remediation in _HEADER_RULES:
        if name in headers:
            continue
        findings.append(
            _finding(
                identifier=f"header-{name}",
                title=title,
                severity=severity,
                target=target,
                detail=f"{response.status_code} response from {target} did not set `{name}`.",
                remediation=remediation,
                cwe=cwe,
            )
        )
    csp = headers.get("content-security-policy", "")
    if "x-frame-options" not in headers and "frame-ancestors" not in csp:
        findings.append(
            _finding(
                identifier="header-frame-options",
                title="Page can be framed by any site",
                severity="medium",
                target=target,
                detail="Neither X-Frame-Options nor a CSP frame-ancestors directive was sent.",
                remediation="Send `frame-ancestors 'none'` in the CSP (or X-Frame-Options: DENY).",
                cwe="CWE-1021",
            )
        )
    if target.startswith("https://") and "strict-transport-security" not in headers:
        findings.append(
            _finding(
                identifier="header-hsts",
                title="No HTTP Strict-Transport-Security header",
                severity="medium",
                target=target,
                detail="An HTTPS deployment that omits HSTS can be downgraded on first contact.",
                remediation=(
                    "Send `Strict-Transport-Security: max-age=31536000; includeSubDomains`."
                ),
                cwe="CWE-319",
            )
        )
    disclosed = {name: headers[name] for name in _DISCLOSING_HEADERS if name in headers}
    if disclosed:
        rendered = ", ".join(f"{key}: {value}" for key, value in disclosed.items())
        findings.append(
            _finding(
                identifier="header-version-disclosure",
                title="Response advertises the server software and version",
                severity="low",
                target=target,
                detail=f"Disclosed: {rendered}",
                remediation=(
                    "Strip these headers at the proxy; they hand an attacker the exact "
                    "advisory list to try."
                ),
                cwe="CWE-200",
            )
        )
    return findings


def _check_cookies(target: str, response: httpx.Response) -> list[QAFinding]:
    findings: list[QAFinding] = []
    https = target.startswith("https://")
    for raw in response.headers.get_list("set-cookie"):
        name = raw.split("=", 1)[0].strip()
        attributes = raw.casefold()
        missing: list[str] = []
        if "httponly" not in attributes:
            missing.append("HttpOnly")
        if https and "secure" not in attributes:
            missing.append("Secure")
        if "samesite" not in attributes:
            missing.append("SameSite")
        if not missing:
            continue
        findings.append(
            _finding(
                identifier=f"cookie-{name}",
                title=f"Cookie `{name}` is missing {', '.join(missing)}",
                severity="medium" if "HttpOnly" in missing else "low",
                target=target,
                detail=(
                    "A session cookie readable from JavaScript turns any XSS into full account "
                    "takeover, and one without SameSite is available to cross-site requests."
                ),
                remediation=f"Set {', '.join(missing)} on this cookie.",
                cwe="CWE-1004" if "HttpOnly" in missing else "CWE-1275",
            )
        )
    return findings


def _check_body(target: str, response: httpx.Response) -> list[QAFinding]:
    body = _text(response)
    findings: list[QAFinding] = []
    if _DIRECTORY_LISTING.search(body):
        findings.append(
            _finding(
                identifier="directory-listing",
                title="Directory listing is enabled",
                severity="medium",
                target=target,
                detail="The root response is an auto-generated file index.",
                remediation="Disable auto-indexing and serve an explicit document root.",
                cwe="CWE-548",
            )
        )
    if _STACK_TRACE.search(body):
        findings.append(
            _finding(
                identifier="stack-trace-root",
                title="Application stack trace returned to the client",
                severity="high",
                target=target,
                detail="The response body contains a framework stack trace.",
                remediation=(
                    "Return a generic error page in production and log the detail server-side."
                ),
                cwe="CWE-209",
            )
        )
    return findings


async def _check_error_handling(
    client: httpx.AsyncClient, target: str, log: list[str]
) -> list[QAFinding]:
    """A route that cannot exist is the cheapest way to see the error page."""
    probed = urljoin(target, f"/daino-inspector-{uuid4().hex[:10]}")
    response = await _get(client, probed, log)
    if response is None:
        return []
    body = _text(response)
    findings: list[QAFinding] = []
    if _STACK_TRACE.search(body):
        findings.append(
            _finding(
                identifier="stack-trace-404",
                title="Unhandled route returns a stack trace",
                severity="high",
                target=probed,
                detail=f"{response.status_code} response exposed internal frames and file paths.",
                remediation=(
                    "Turn the debugger off outside development and return a generic error body."
                ),
                cwe="CWE-209",
            )
        )
    if "Werkzeug Debugger" in body or "console-lock" in body:
        findings.append(
            _finding(
                identifier="interactive-debugger",
                title="Interactive debugger reachable over HTTP",
                severity="critical",
                target=probed,
                detail="The Werkzeug interactive debugger executes arbitrary Python on request.",
                remediation="Never run with debug=True on a reachable interface.",
                cwe="CWE-489",
            )
        )
    return findings


async def _check_cors(client: httpx.AsyncClient, target: str, log: list[str]) -> list[QAFinding]:
    response = await _get(client, target, log, headers={"Origin": _PROBE_ORIGIN})
    if response is None:
        return []
    allowed = response.headers.get("access-control-allow-origin", "")
    credentials = response.headers.get("access-control-allow-credentials", "").casefold() == "true"
    if allowed == _PROBE_ORIGIN and credentials:
        return [
            _finding(
                identifier="cors-reflected-credentials",
                title="CORS reflects any origin and allows credentials",
                severity="high",
                target=target,
                detail=(
                    f"The server echoed `{_PROBE_ORIGIN}` and set "
                    "Access-Control-Allow-Credentials: true, so any site can read authenticated "
                    "responses."
                ),
                remediation="Match the Origin against an allow-list before echoing it back.",
                cwe="CWE-942",
            )
        ]
    if allowed == "*" and credentials:
        return [
            _finding(
                identifier="cors-wildcard-credentials",
                title="CORS wildcard combined with credentials",
                severity="medium",
                target=target,
                detail="A wildcard origin with credentials is rejected by browsers and signals "
                "an unbounded policy.",
                remediation="Name the origins the API serves.",
                cwe="CWE-942",
            )
        ]
    if allowed == _PROBE_ORIGIN:
        return [
            _finding(
                identifier="cors-reflected",
                title="CORS reflects any requesting origin",
                severity="low",
                target=target,
                detail=f"The server echoed `{_PROBE_ORIGIN}` back as an allowed origin.",
                remediation="Match the Origin against an allow-list before echoing it back.",
                cwe="CWE-942",
            )
        ]
    return []


async def _check_methods(client: httpx.AsyncClient, target: str, log: list[str]) -> list[QAFinding]:
    try:
        response = await client.options(target)
    except httpx.HTTPError as exc:
        log.append(f"OPTIONS {target} → {type(exc).__name__}")
        return []
    log.append(f"OPTIONS {target} → {response.status_code}")
    allowed = {
        method.strip().upper()
        for header in ("allow", "access-control-allow-methods")
        for method in response.headers.get(header, "").split(",")
        if method.strip()
    }
    risky = allowed & {"TRACE", "TRACK", "PUT", "DELETE", "PATCH", "CONNECT"}
    if not risky:
        return []
    severity: QASeverity = "medium" if allowed & {"TRACE", "TRACK", "CONNECT"} else "low"
    return [
        _finding(
            identifier="http-methods",
            title=f"Server advertises {', '.join(sorted(risky))}",
            severity=severity,
            target=target,
            detail=f"OPTIONS reported: {', '.join(sorted(allowed))}.",
            remediation=(
                "Disable TRACE/TRACK entirely and confirm the state-changing methods are "
                "authenticated."
            ),
            cwe="CWE-16",
        )
    ]


async def _check_exposed_paths(
    client: httpx.AsyncClient, target: str, log: list[str]
) -> list[QAFinding]:
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def probe(entry: ExposurePath) -> QAFinding | None:
        async with semaphore:
            response = await _get(client, urljoin(target, entry.path), log)
        if response is None or response.status_code != 200:
            return None
        body = _text(response)
        if entry.signature is not None and not entry.signature.search(body):
            return None
        return _finding(
            identifier=f"exposed{entry.path.replace('/', '-')}",
            title=entry.title,
            severity=entry.severity,
            target=urljoin(target, entry.path),
            detail=f"200 response of {len(response.content)} bytes matched the expected content.",
            remediation=entry.remediation,
            cwe=entry.cwe,
        )

    results = await asyncio.gather(*(probe(entry) for entry in EXPOSED_PATHS))
    return [item for item in results if item is not None]


# --------------------------------------------------------------- utilities


async def _get(
    client: httpx.AsyncClient,
    url: str,
    log: list[str],
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response | None:
    try:
        response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        log.append(f"GET {url} → {type(exc).__name__}: {exc}")
        return None
    log.append(f"GET {url} → {response.status_code} ({len(response.content)} bytes)")
    return response


def _text(response: httpx.Response) -> str:
    """Decode a bounded prefix; a probe must not buffer a large download."""
    try:
        return response.text[:20_000]
    except (UnicodeDecodeError, ValueError):
        return ""


def _normalise(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if "://" not in url:
        url = f"http://{url}"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path or '/'}"


def _finding(
    *,
    identifier: str,
    title: str,
    severity: QASeverity,
    target: str,
    detail: str,
    remediation: str,
    cwe: str,
) -> QAFinding:
    return QAFinding(
        id=f"live-{identifier}",
        title=title,
        severity=severity,
        category="runtime",
        source="live target probe",
        location=target,
        detail=detail,
        remediation=remediation,
        cwe=cwe,
        reference=f"live-{identifier}",
        confidence="high",
    )
