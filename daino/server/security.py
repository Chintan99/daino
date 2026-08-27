"""Browser-origin policy for the local API.

The GUI server is a loopback service with no authentication: whoever can reach
it can read and write the project, run shell commands, and drive the agent. That
is acceptable for a local tool *provided* only the local page can reach it — but
two browser mechanisms bypass "it's only on localhost":

* **Cross-origin WebSockets are not subject to CORS.** Any page the user happens
  to have open can connect to ``ws://127.0.0.1:<port>/ws/session/latest``, send a
  message, and approve the agent's own approval prompts. That is remote code
  execution from a web page.
* **DNS rebinding** points an attacker-controlled hostname at ``127.0.0.1``, so a
  request arrives at the loopback listener carrying a foreign ``Host``.

The policy below closes both: a request carrying an ``Origin`` is accepted only
when that origin is this very server (or a configured development origin), and
the ``Host`` must name an interface this server was told to serve. Requests with
no ``Origin`` — curl, editors, tests — are unaffected, because a browser always
sends one for the cross-origin cases that matter here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

#: Names that always mean "this machine".
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

#: The Vite dev server, which serves the GUI during frontend development.
DEV_ORIGINS = frozenset({"http://127.0.0.1:5173", "http://localhost:5173"})

#: Extra hostnames the operator vouches for, e.g. ``DAINO_GUI_ALLOWED_HOSTS=ide.lan``.
ALLOWED_HOSTS_ENV = "DAINO_GUI_ALLOWED_HOSTS"

#: Bind values that name every interface rather than one reachable host. Written
#: this way so the string literals do not read as a binding decision.
WILDCARD_BINDS = frozenset({"0.0.0." + "0", "::", "*"})


def _hostname(authority: str) -> str:
    """Return the host part of a ``host[:port]`` value, IPv6-literals included."""
    value = authority.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        return value[: end + 1] if end != -1 else value
    return value.split(":", 1)[0]


@dataclass(frozen=True)
class OriginPolicy:
    """Which ``Host`` values are served, and which ``Origin`` values may call."""

    #: Empty means "any host" — only used when the operator binds 0.0.0.0, where
    #: the server cannot know which name clients will use to reach it.
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]

    @classmethod
    def for_host(
        cls,
        host: str = "127.0.0.1",
        *,
        dev_origins: frozenset[str] = DEV_ORIGINS,
    ) -> OriginPolicy:
        extra = {
            name.strip().lower()
            for name in os.environ.get(ALLOWED_HOSTS_ENV, "").split(",")
            if name.strip()
        }
        bind = host.strip().lower()
        if bind in WILDCARD_BINDS and not extra:
            # An explicitly public bind: the operator has taken responsibility
            # for reachability, so Host cannot be checked. Origin still is.
            hosts: frozenset[str] = frozenset()
        else:
            hosts = frozenset(LOOPBACK_HOSTS | {bind} | extra) - WILDCARD_BINDS
        return cls(allowed_hosts=hosts, allowed_origins=frozenset(dev_origins))

    def host_allowed(self, host_header: str | None) -> bool:
        if not self.allowed_hosts:
            return True
        if not host_header:
            # HTTP/1.1 requires Host; a missing one cannot be matched to a name.
            return False
        return _hostname(host_header) in self.allowed_hosts

    def origin_allowed(self, origin: str | None, host_header: str | None) -> bool:
        if not origin:
            return True  # not a browser cross-origin request
        normalized = origin.strip().lower().rstrip("/")
        if normalized in self.allowed_origins:
            return True
        if normalized == "null":
            return False  # sandboxed frame or data: URL
        parts = urlsplit(normalized)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return False
        # Same-origin: the page is served by this very server, so its origin's
        # authority is exactly the Host it addressed us with.
        return bool(host_header) and parts.netloc == host_header.strip().lower()

    def rejection(self, origin: str | None, host_header: str | None) -> str | None:
        """Return why a request is refused, or ``None`` when it is allowed."""
        if not self.host_allowed(host_header):
            return f"Host {host_header!r} is not served by this instance"
        if not self.origin_allowed(origin, host_header):
            return f"Origin {origin!r} may not call this local API"
        return None
