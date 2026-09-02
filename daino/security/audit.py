"""Offline security audit of a checkout: secrets, insecure code, weak configuration.

This is the part of the Inspector that needs neither a network nor a model. It
reads the working tree once and applies a fixed, auditable rule table, so a
repository with no API key configured and no scanner installed still gets a
vulnerability assessment before a production push.

The rules are deliberately conservative. A pre-push gate that cries wolf gets
turned off, so every rule here either names a concrete weakness class (with its
CWE) or is not included. Where a pattern is inherently ambiguous — a credential
that might be a placeholder, a finding inside a test fixture — the rule keeps
the finding but lowers its severity and confidence rather than guessing.

Nothing in this module executes project code or shells out.
"""

from __future__ import annotations

import fnmatch
import math
import os
import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from daino.schemas import QAFinding, QAFindingCategory, QASeverity

#: Directories that are never the project's own source, or are too large to be
#: worth reading. Vendored dependencies get audited by the dependency scanners.
IGNORED_DIRECTORIES = frozenset(
    {
        ".daino",
        ".git",
        ".hg",
        ".idea",
        ".mypy_cache",
        ".next",
        ".nuxt",
        ".pytest_cache",
        ".ruff_cache",
        ".svelte-kit",
        ".svn",
        ".terraform",
        ".tox",
        ".vasuki",
        ".venv",
        ".vscode",
        "__pycache__",
        "bower_components",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "node_modules",
        "out",
        "site-packages",
        "target",
        "vendor",
        "venv",
    }
)

#: Binary and generated files. Reading them yields noise, not findings.
IGNORED_SUFFIXES = frozenset(
    {
        ".avif",
        ".bmp",
        ".bz2",
        ".class",
        ".dll",
        ".dylib",
        ".eot",
        ".exe",
        ".gif",
        ".gz",
        ".ico",
        ".jar",
        ".jpeg",
        ".jpg",
        ".map",
        ".mp3",
        ".mp4",
        ".o",
        ".otf",
        ".pdf",
        ".png",
        ".pyc",
        ".so",
        ".svg",
        ".tar",
        ".ttf",
        ".wasm",
        ".webp",
        ".whl",
        ".woff",
        ".woff2",
        ".zip",
    }
)

#: Lock files are machine-written, enormous, and full of hash-like strings that
#: trip every entropy heuristic. Their vulnerabilities come from the dependency
#: audits instead.
IGNORED_NAMES = frozenset(
    {
        "Cargo.lock",
        "composer.lock",
        "go.sum",
        "npm-shrinkwrap.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "yarn.lock",
    }
)

#: A file bigger than this is either generated or data; either way it is not
#: hand-written source that a reviewer would act on.
MAX_FILE_BYTES = 512_000
MAX_FILES = 6_000
#: One misconfigured rule must not bury the report; the rest are counted instead.
MAX_FINDINGS_PER_RULE = 20

#: Paths whose findings are real but rarely shippable risks on their own.
_NON_PRODUCTION = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs|e2e|fixtures?|examples?|samples?|docs?|"
    r"mocks?|testdata|benchmarks?)(/|$)",
    re.IGNORECASE,
)

_MINIFIED = re.compile(r"\.min\.(js|css)$", re.IGNORECASE)

#: Values that look like credentials but are obviously not one.
_PLACEHOLDER = re.compile(
    r"^(?:"
    r"x{3,}|y{3,}|\*{3,}|\.{3,}|0{4,}|1234\d*|"
    r"changeme|placeholder|redacted|dummy|sample|example|test|testing|secret|password|token|"
    r"none|null|nil|true|false|undefined|"
    r"your[-_ ].*|my[-_ ].*|some[-_ ].*|the[-_ ].*|insert[-_ ].*|"
    r"<.*>|\{\{.*\}\}|\$\{.*\}|%\(.*\)s|%s|\$[A-Z_]+|\{[a-z_]+\}"
    r")$",
    re.IGNORECASE,
)

#: A credential read from somewhere at runtime is the fix, not the finding.
_RUNTIME_LOOKUP = re.compile(
    r"(os\.environ|os\.getenv|getenv|process\.env|import\.meta\.env|"
    r"env\[|ENV\[|secrets?\.|vault|keyring|config\.|settings\.|self\.|this\.)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class SecretRule:
    """A credential shape specific enough to name on sight."""

    id: str
    title: str
    pattern: re.Pattern[str]
    severity: QASeverity
    #: Capture group holding the value to sanity-check; 0 means the whole match.
    value_group: int = 0
    #: Generic rules match a lot of prose; they need entropy to be believed.
    require_entropy: bool = False
    remediation: str = (
        "Revoke and rotate the credential, remove it from the working tree and from git "
        "history, and load it from the environment or a secret manager instead."
    )


@dataclass(frozen=True, slots=True)
class CodeRule:
    """A weakness recognisable from one line of source or configuration."""

    id: str
    title: str
    pattern: re.Pattern[str]
    severity: QASeverity
    cwe: str
    remediation: str
    category: QAFindingCategory = "vulnerability"
    #: File suffixes the rule applies to; empty means "any text file".
    suffixes: tuple[str, ...] = ()
    #: Exact file names (or names ending with one of these) the rule applies to.
    names: tuple[str, ...] = ()
    #: A same-line match that means the weakness is already handled.
    unless: re.Pattern[str] | None = None


SECRET_RULES: tuple[SecretRule, ...] = (
    SecretRule(
        id="secret-aws-access-key",
        title="AWS access key id committed to the repository",
        pattern=re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[0-9A-Z]{16}\b"),
        severity="critical",
    ),
    SecretRule(
        id="secret-private-key",
        title="Private key committed to the repository",
        pattern=re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
        severity="critical",
    ),
    SecretRule(
        id="secret-github-token",
        title="GitHub token committed to the repository",
        pattern=re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
        severity="critical",
    ),
    SecretRule(
        id="secret-model-provider-key",
        title="Model provider API key committed to the repository",
        pattern=re.compile(r"\bsk-(?:ant-|proj-|or-)?[A-Za-z0-9_-]{24,}\b"),
        severity="critical",
    ),
    SecretRule(
        id="secret-stripe-key",
        title="Live Stripe key committed to the repository",
        pattern=re.compile(r"\b[sr]k_live_[0-9A-Za-z]{16,}\b"),
        severity="critical",
    ),
    SecretRule(
        id="secret-slack-token",
        title="Slack token committed to the repository",
        pattern=re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
        severity="high",
    ),
    SecretRule(
        id="secret-google-api-key",
        title="Google API key committed to the repository",
        pattern=re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
        severity="high",
    ),
    SecretRule(
        id="secret-connection-string",
        title="Database connection string with an inline password",
        pattern=re.compile(
            r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|redis|amqp)"
            r"://[^\s/@:'\"]+:([^\s/@'\"]{3,})@"
        ),
        severity="high",
        value_group=1,
    ),
    SecretRule(
        id="secret-jwt",
        title="Signed JSON Web Token committed to the repository",
        pattern=re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}\b"),
        severity="medium",
    ),
    SecretRule(
        id="secret-hardcoded-credential",
        title="Hard-coded credential assigned in source",
        pattern=re.compile(
            r"(?i)\b(?:api[_-]?key|secret[_-]?key|secret|passwd|password|access[_-]?token|"
            r"auth[_-]?token|client[_-]?secret|private[_-]?key)\s*[:=]\s*"
            r"[\"']([^\"'\s]{8,})[\"']"
        ),
        severity="high",
        value_group=1,
        require_entropy=True,
    ),
)


_PY = (".py",)
_JS = (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts")
_YAML = (".yml", ".yaml")


CODE_RULES: tuple[CodeRule, ...] = (
    # ---------------------------------------------------------------- Python
    CodeRule(
        id="py-shell-injection",
        title="Subprocess invoked through a shell",
        pattern=re.compile(r"\bshell\s*=\s*True\b"),
        severity="high",
        cwe="CWE-78",
        remediation=(
            "Pass the command as an argument list and drop shell=True so user input cannot "
            "become shell syntax."
        ),
        suffixes=_PY,
    ),
    CodeRule(
        id="py-os-system",
        title="Command executed with os.system",
        pattern=re.compile(r"\bos\.(?:system|popen)\s*\("),
        severity="high",
        cwe="CWE-78",
        remediation="Use subprocess.run with an argument list instead of a shell string.",
        suffixes=_PY,
    ),
    CodeRule(
        id="py-eval",
        title="Dynamic code execution with eval/exec",
        pattern=re.compile(r"(?<![\w.])(?:eval|exec)\s*\("),
        severity="high",
        cwe="CWE-95",
        remediation=(
            "Replace eval/exec with an explicit parser, a dispatch table, or ast.literal_eval."
        ),
        suffixes=_PY,
    ),
    CodeRule(
        id="py-insecure-deserialization",
        title="Untrusted data deserialised with pickle or marshal",
        pattern=re.compile(r"\b(?:pickle|cPickle|dill|marshal|shelve)\.(?:load|loads)\s*\("),
        severity="high",
        cwe="CWE-502",
        remediation="Use JSON or another data-only format for anything crossing a trust boundary.",
        suffixes=_PY,
    ),
    CodeRule(
        id="py-yaml-load",
        title="YAML parsed with the unsafe loader",
        pattern=re.compile(r"\byaml\.load\s*\("),
        severity="high",
        cwe="CWE-502",
        remediation="Call yaml.safe_load, or pass Loader=yaml.SafeLoader.",
        suffixes=_PY,
        unless=re.compile(r"SafeLoader|BaseLoader|safe_load"),
    ),
    CodeRule(
        id="py-tls-verification-disabled",
        title="TLS certificate verification disabled",
        pattern=re.compile(r"verify\s*=\s*False|_create_unverified_context|CERT_NONE"),
        severity="high",
        cwe="CWE-295",
        remediation=(
            "Keep certificate verification on; point the client at the internal CA bundle if the "
            "certificate is private."
        ),
        suffixes=_PY,
    ),
    CodeRule(
        id="py-weak-hash",
        title="Weak hash algorithm",
        pattern=re.compile(r"\bhashlib\.(?:md5|sha1)\s*\(|\busedforsecurity\s*=\s*True"),
        severity="medium",
        cwe="CWE-327",
        remediation=(
            "Use SHA-256 for integrity and a password hash (argon2/bcrypt/scrypt) for "
            "credentials; pass usedforsecurity=False when the digest is non-security."
        ),
        suffixes=_PY,
        unless=re.compile(r"usedforsecurity\s*=\s*False"),
    ),
    CodeRule(
        id="py-sql-string-building",
        title="SQL statement built by string interpolation",
        pattern=re.compile(
            r"(?i)^(?=.*\b(?:SELECT|INSERT|UPDATE|DELETE|ALTER|CREATE|DROP|TRUNCATE|WHERE)\b)"
            r".*\b(?:execute|executemany|executescript|raw|text|query|sql)\s*\(\s*"
            r"(?:f[\"']|[\"'][^\"']*[\"']\s*(?:%|\+|\.format\b))"
        ),
        severity="high",
        cwe="CWE-89",
        remediation="Use bound parameters instead of building the statement from strings.",
        suffixes=_PY,
    ),
    CodeRule(
        id="py-debug-server",
        title="Development server started with the debugger enabled",
        pattern=re.compile(r"\.run\s*\([^)]*debug\s*=\s*True"),
        severity="high",
        cwe="CWE-489",
        remediation=(
            "Drive debug from configuration and default it off; an exposed debug console is "
            "remote code execution, not a convenience."
        ),
        category="configuration",
        suffixes=_PY,
    ),
    CodeRule(
        id="py-debug-setting",
        title="DEBUG left on in settings",
        pattern=re.compile(r"^\s*DEBUG\s*(?::\s*bool\s*)?=\s*True\b"),
        severity="high",
        cwe="CWE-489",
        remediation="Read DEBUG from the environment and default it to False.",
        category="configuration",
        suffixes=_PY,
    ),
    CodeRule(
        id="py-bind-all-interfaces",
        title="Service bound to every network interface",
        pattern=re.compile(r"(?:host|bind|HOST)\s*[:=]\s*[\"']0\.0\.0\.0[\"']"),
        severity="medium",
        cwe="CWE-668",
        remediation=(
            "Bind to 127.0.0.1 and let the reverse proxy or orchestrator publish the port."
        ),
        category="configuration",
        suffixes=_PY,
    ),
    CodeRule(
        id="py-permissive-cors",
        title="CORS allows every origin",
        pattern=re.compile(r"allow_origins\s*=\s*\[\s*[\"']\*[\"']"),
        severity="medium",
        cwe="CWE-942",
        remediation=(
            "List the origins you actually serve; a wildcard with credentials is rejected by "
            "browsers and unsafe without them."
        ),
        category="configuration",
        suffixes=_PY,
    ),
    CodeRule(
        id="py-allowed-hosts-wildcard",
        title="Host header validation disabled",
        pattern=re.compile(r"ALLOWED_HOSTS\s*=\s*\[\s*[\"']\*[\"']"),
        severity="medium",
        cwe="CWE-20",
        remediation="Name the hostnames the deployment answers on.",
        category="configuration",
        suffixes=_PY,
    ),
    CodeRule(
        id="py-insecure-temp-file",
        title="Predictable temporary file name",
        pattern=re.compile(r"\btempfile\.mktemp\s*\(|\bos\.tmpnam\s*\("),
        severity="medium",
        cwe="CWE-377",
        remediation="Use tempfile.NamedTemporaryFile or mkstemp so the name cannot be guessed.",
        suffixes=_PY,
    ),
    # ------------------------------------------------------- JavaScript / TS
    CodeRule(
        id="js-eval",
        title="Dynamic code execution with eval or new Function",
        pattern=re.compile(r"(?<![\w.])eval\s*\(|\bnew\s+Function\s*\("),
        severity="high",
        cwe="CWE-95",
        remediation="Replace dynamic evaluation with an explicit parser or dispatch table.",
        suffixes=_JS,
    ),
    CodeRule(
        id="js-child-process-exec",
        title="Shell command built from a template string",
        pattern=re.compile(r"\bexecSync?\s*\(\s*`|\bexec\s*\(\s*[\"'][^\"']*[\"']\s*\+"),
        severity="high",
        cwe="CWE-78",
        remediation="Use execFile/spawn with an argument array so input cannot alter the command.",
        suffixes=_JS,
    ),
    CodeRule(
        id="js-dangerous-html",
        title="Unsanitised HTML injected into the DOM",
        pattern=re.compile(r"dangerouslySetInnerHTML|\.(?:innerHTML|outerHTML)\s*=|v-html"),
        severity="medium",
        cwe="CWE-79",
        remediation=(
            "Render text, or sanitise with a maintained sanitiser (DOMPurify) before injecting."
        ),
        suffixes=(*_JS, ".vue", ".svelte", ".html"),
    ),
    CodeRule(
        id="js-tls-verification-disabled",
        title="TLS certificate verification disabled",
        pattern=re.compile(r"rejectUnauthorized\s*:\s*false|NODE_TLS_REJECT_UNAUTHORIZED"),
        severity="high",
        cwe="CWE-295",
        remediation="Keep certificate verification on and trust the internal CA explicitly.",
        suffixes=(*_JS, ".json", ".env"),
    ),
    CodeRule(
        id="js-permissive-cors",
        title="CORS reflects or allows every origin",
        pattern=re.compile(
            r"(?i)Access-Control-Allow-Origin[\"']?\s*[:,]\s*[\"']\*[\"']|"
            r"origin\s*:\s*[\"']\*[\"']|cors\s*\(\s*\)"
        ),
        severity="medium",
        cwe="CWE-942",
        remediation="Allow only the origins the API is meant to serve.",
        category="configuration",
        suffixes=_JS,
    ),
    CodeRule(
        id="js-weak-randomness",
        title="Security value derived from Math.random",
        pattern=re.compile(
            r"(?i)(?:token|secret|nonce|salt|otp|session|password|key)\s*[:=][^;\n]*Math\.random"
        ),
        severity="medium",
        cwe="CWE-338",
        remediation=(
            "Use crypto.randomUUID or crypto.getRandomValues for anything security-bearing."
        ),
        suffixes=_JS,
    ),
    # ------------------------------------------------------ containers / IaC
    CodeRule(
        id="docker-latest-tag",
        title="Base image pinned to a floating tag",
        pattern=re.compile(r"(?i)^\s*FROM\s+\S+:latest\b"),
        severity="low",
        cwe="CWE-1104",
        remediation="Pin the base image to a digest or an immutable version tag.",
        category="configuration",
        names=("Dockerfile",),
    ),
    CodeRule(
        id="docker-pipe-to-shell",
        title="Remote script piped straight into a shell during build",
        pattern=re.compile(r"(?i)(?:curl|wget)\b[^\n]*\|\s*(?:ba|z|k)?sh\b"),
        severity="high",
        cwe="CWE-494",
        remediation="Download, verify a checksum or signature, then execute.",
        category="configuration",
        names=("Dockerfile",),
    ),
    CodeRule(
        id="docker-insecure-download",
        title="Certificate checks disabled during image build",
        pattern=re.compile(r"--no-check-certificate|--insecure\b|-k\s+https?://"),
        severity="high",
        cwe="CWE-295",
        remediation="Fetch build inputs over verified TLS.",
        category="configuration",
        names=("Dockerfile",),
    ),
    CodeRule(
        id="compose-privileged",
        title="Container granted privileged or host-level access",
        pattern=re.compile(
            r"(?i)privileged\s*:\s*true|hostNetwork\s*:\s*true|network_mode\s*:\s*[\"']?host|"
            r"/var/run/docker\.sock"
        ),
        severity="high",
        cwe="CWE-250",
        remediation=(
            "Drop privileged mode and host networking; grant only the specific capabilities the "
            "workload needs."
        ),
        category="configuration",
        suffixes=_YAML,
    ),
    CodeRule(
        id="k8s-run-as-root",
        title="Workload runs as root",
        pattern=re.compile(r"(?i)runAsUser\s*:\s*0\b|runAsNonRoot\s*:\s*false"),
        severity="medium",
        cwe="CWE-250",
        remediation="Run as a non-root uid and set runAsNonRoot: true.",
        category="configuration",
        suffixes=_YAML,
    ),
    CodeRule(
        id="iac-open-ingress",
        title="Network rule open to the whole internet",
        pattern=re.compile(r"0\.0\.0\.0/0|::/0"),
        severity="high",
        cwe="CWE-284",
        remediation="Restrict the CIDR to the networks that legitimately need access.",
        category="configuration",
        suffixes=(".tf", ".tfvars", ".hcl"),
    ),
    CodeRule(
        id="iac-public-bucket",
        title="Object storage exposed publicly",
        pattern=re.compile(
            r"(?i)acl\s*[:=]\s*[\"']?public-read(-write)?[\"']?|"
            r"public[_-]?access(?:[_-]?block)?\s*[:=]\s*[\"']?true"
        ),
        severity="high",
        cwe="CWE-284",
        remediation="Keep the bucket private and serve through a signed URL or CDN origin policy.",
        category="configuration",
        suffixes=(".tf", ".tfvars", ".hcl", *_YAML, ".json"),
    ),
    # ------------------------------------------------------------------- CI
    CodeRule(
        id="ci-pull-request-target",
        title="Workflow runs untrusted code with repository secrets",
        pattern=re.compile(r"(?i)^\s*(?:-\s*)?pull_request_target\s*:?"),
        severity="high",
        cwe="CWE-829",
        remediation=(
            "Use pull_request, or keep pull_request_target free of any checkout of the fork's "
            "head and of any secret."
        ),
        category="configuration",
        names=(".github/workflows",),
    ),
    CodeRule(
        id="ci-unpinned-action",
        title="Third-party action referenced by a moving ref",
        pattern=re.compile(
            r"(?i)^\s*(?:-\s*)?uses\s*:\s*(?!actions/|github/)\S+@(?:main|master)\b"
        ),
        severity="medium",
        cwe="CWE-829",
        remediation="Pin third-party actions to a full commit SHA.",
        category="configuration",
        names=(".github/workflows",),
    ),
)


def audit_repository(root: Path) -> list[QAFinding]:
    """Return every offline finding for a checkout, newest rules included.

    One pass over the tree drives all of the rule tables: a large repository is
    slow to read, not slow to match.
    """
    findings: list[QAFinding] = []
    for relative, text in iter_text_files(root):
        findings.extend(scan_secrets(relative, text))
        findings.extend(scan_patterns(relative, text))
    findings.extend(scan_repository_hygiene(root))
    return cap_per_rule(deduplicate(findings))


def iter_text_files(root: Path, *, limit: int = MAX_FILES) -> Iterator[tuple[str, str]]:
    """Yield ``(repository-relative path, text)`` for auditable source files.

    The walk prunes ignored directories rather than filtering afterwards: a
    checkout with ``node_modules`` in it is otherwise dominated by the cost of
    listing files this audit will never read.
    """
    read = 0
    for directory, subdirectories, names in os.walk(root, followlinks=False):
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if name not in IGNORED_DIRECTORIES and not os.path.islink(os.path.join(directory, name))
        )
        for name in sorted(names):
            if read >= limit:
                return
            if name in IGNORED_NAMES or _MINIFIED.search(name):
                continue
            path = Path(directory) / name
            if path.suffix.casefold() in IGNORED_SUFFIXES or path.is_symlink():
                continue
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            read += 1
            yield path.relative_to(root).as_posix(), text


def scan_secrets(relative: str, text: str) -> list[QAFinding]:
    """Match credential shapes, discarding placeholders and runtime lookups."""
    findings: list[QAFinding] = []
    non_production = bool(_NON_PRODUCTION.search(relative))
    for number, line in enumerate(text.splitlines(), start=1):
        if len(line) > 2_000:
            continue
        for rule in SECRET_RULES:
            match = rule.pattern.search(line)
            if match is None:
                continue
            value = match.group(rule.value_group) if rule.value_group else match.group(0)
            if _is_placeholder(value):
                continue
            if rule.value_group and _RUNTIME_LOOKUP.search(line):
                continue
            if rule.require_entropy and _entropy(value) < 3.0:
                continue
            # A credential in a fixture is usually fabricated. Keep the finding,
            # but two steps down so it can never be what blocks a release.
            severity = _demote(_demote(rule.severity)) if non_production else rule.severity
            findings.append(
                QAFinding(
                    id=f"{rule.id}:{relative}:{number}",
                    title=rule.title,
                    severity=severity,
                    category="secrets",
                    source="built-in secret scan",
                    location=relative,
                    line=number,
                    detail=_evidence(line, value),
                    remediation=rule.remediation,
                    cwe="CWE-798",
                    reference=rule.id,
                    confidence="low" if non_production else "high",
                )
            )
            break
    return findings


def scan_patterns(relative: str, text: str) -> list[QAFinding]:
    """Apply the insecure-code and weak-configuration rules to one file."""
    applicable = [rule for rule in CODE_RULES if _rule_applies(rule, relative)]
    if not applicable:
        return []
    non_production = bool(_NON_PRODUCTION.search(relative))
    findings: list[QAFinding] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or len(line) > 2_000 or is_non_executable(stripped, relative):
            continue
        for rule in applicable:
            if not rule.pattern.search(line):
                continue
            if rule.unless is not None and rule.unless.search(line):
                continue
            findings.append(
                QAFinding(
                    id=f"{rule.id}:{relative}:{number}",
                    title=rule.title,
                    severity=_demote(rule.severity) if non_production else rule.severity,
                    category=rule.category,
                    source="built-in code audit",
                    location=relative,
                    line=number,
                    detail=stripped[:240],
                    remediation=rule.remediation,
                    cwe=rule.cwe,
                    reference=rule.id,
                    confidence="low" if non_production else "medium",
                )
            )
    return findings


def scan_repository_hygiene(root: Path) -> list[QAFinding]:
    """Whole-repository checks that no single line can express."""
    findings: list[QAFinding] = []
    ignore_text = _read(root / ".gitignore")
    env_files = [
        path
        for path in root.glob(".env*")
        if path.is_file() and path.suffix not in {".example", ".sample", ".template"}
    ]
    for path in env_files:
        name = path.name
        if name.endswith((".example", ".sample", ".template", ".dist")):
            continue
        ignored = _gitignore_covers(ignore_text, name)
        if ignored:
            continue
        findings.append(
            QAFinding(
                id=f"env-not-ignored:{name}",
                title=f"{name} is not excluded by .gitignore",
                severity="high",
                category="secrets",
                source="built-in repository hygiene",
                location=name,
                detail=(
                    "Environment files hold live credentials. This one is not covered by "
                    ".gitignore, so it can be committed and published with the repository."
                ),
                remediation=(
                    f"Add `{name}` to .gitignore, confirm it is untracked with "
                    f"`git ls-files {name}`, and rotate anything it already contains."
                ),
                cwe="CWE-538",
                reference="env-not-ignored",
                confidence="high",
            )
        )
    if (root / ".git").is_dir() and not ignore_text:
        findings.append(
            QAFinding(
                id="missing-gitignore",
                title="Repository has no .gitignore",
                severity="low",
                category="configuration",
                source="built-in repository hygiene",
                location=".gitignore",
                detail="Without an ignore list, local env files and build output can be committed.",
                remediation=(
                    "Add a .gitignore covering environment files, build output, and caches."
                ),
                cwe="CWE-538",
                reference="missing-gitignore",
                confidence="high",
            )
        )
    dockerfile = root / "Dockerfile"
    if dockerfile.is_file():
        text = _read(dockerfile)
        if text and not re.search(r"(?im)^\s*USER\s+(?!root\b)\S+", text):
            findings.append(
                QAFinding(
                    id="docker-runs-as-root",
                    title="Container image runs as root",
                    severity="medium",
                    category="configuration",
                    source="built-in repository hygiene",
                    location="Dockerfile",
                    detail="No USER instruction switches away from root before the entrypoint.",
                    remediation=(
                        "Create an unprivileged user in the image and add a USER instruction "
                        "before CMD/ENTRYPOINT."
                    ),
                    cwe="CWE-250",
                    reference="docker-runs-as-root",
                    confidence="high",
                )
            )
    return findings


# --------------------------------------------------------------- utilities


def deduplicate(findings: list[QAFinding]) -> list[QAFinding]:
    """Collapse identical findings, keeping the first occurrence."""
    seen: dict[tuple[str, str, int | None], QAFinding] = {}
    for finding in findings:
        key = (finding.reference or finding.id, finding.location, finding.line)
        seen.setdefault(key, finding)
    return list(seen.values())


def cap_per_rule(findings: list[QAFinding], limit: int = MAX_FINDINGS_PER_RULE) -> list[QAFinding]:
    """Keep at most ``limit`` findings per rule and record what was elided.

    A single bad pattern in a generated file can match hundreds of times. Losing
    the report to one rule is worse than truncating it, so the overflow becomes
    one summary finding that still says how much was left out.
    """
    counts: Counter[str] = Counter()
    kept: list[QAFinding] = []
    overflow: Counter[str] = Counter()
    titles: dict[str, str] = {}
    for finding in findings:
        rule = finding.reference or finding.id
        counts[rule] += 1
        if counts[rule] <= limit:
            kept.append(finding)
        else:
            overflow[rule] += 1
            titles[rule] = finding.title
    for rule, extra in overflow.items():
        kept.append(
            QAFinding(
                id=f"{rule}:overflow",
                title=f"{titles[rule]} — {extra} further occurrence(s)",
                severity="info",
                category="quality",
                source="built-in scan",
                detail=(
                    f"Only the first {limit} matches for `{rule}` are listed individually; "
                    f"{extra} more were found."
                ),
                remediation="Fix the pattern at its source, then re-run the inspection.",
                reference=rule,
                confidence="high",
            )
        )
    return kept


def _rule_applies(rule: CodeRule, relative: str) -> bool:
    if rule.names and any(part in relative for part in rule.names):
        return True
    if rule.suffixes and relative.casefold().endswith(rule.suffixes):
        return True
    return not rule.names and not rule.suffixes


def is_non_production(relative: str) -> bool:
    """Whether a path is a test, fixture, example, or doc.

    Such a file legitimately contains the very patterns a scanner looks for —
    a security test has to write ``shell=True`` to assert it is caught. The
    finding is kept, because a real credential can live in a fixture too, but
    it is demoted and marked low confidence so it can never be the thing that
    blocks a release.
    """
    return bool(_NON_PRODUCTION.search(relative))


def demote(severity: QASeverity) -> QASeverity:
    """One step down the severity ladder."""
    return _demote(severity)


def is_non_executable(stripped: str, relative: str) -> bool:
    """Whether a line cannot itself be the weakness the rules look for.

    Comments are the obvious case. The others matter for repositories that
    contain security tooling or documentation: a line that *builds* a pattern,
    or a bare Python string that *describes* a weakness, would otherwise be
    reported as the weakness itself.
    """
    if stripped.startswith(("#", "//", "*", "/*", "<!--")):
        return True
    if "re.compile(" in stripped or "new RegExp(" in stripped:
        return True
    return relative.endswith(".py") and stripped.startswith(('"', "'", 'r"', "r'"))


def _is_placeholder(value: str) -> bool:
    value = value.strip().strip("\"'")
    if len(value) < 6:
        return True
    if _PLACEHOLDER.match(value):
        return True
    return len(set(value)) <= 2


def _entropy(value: str) -> float:
    """Shannon entropy per character; a real key is far from English prose."""
    if not value:
        return 0.0
    counts = Counter(value)
    total = len(value)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def _evidence(line: str, value: str) -> str:
    """Show where the credential is without reprinting the credential."""
    masked = value[:4] + "…" + value[-2:] if len(value) > 10 else "…"
    return f"{line.strip()[:160].replace(value, masked)}"


def _demote(severity: QASeverity) -> QASeverity:
    order: list[QASeverity] = ["critical", "high", "medium", "low", "info"]
    return order[min(order.index(severity) + 1, len(order) - 1)]


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _gitignore_covers(ignore_text: str, name: str) -> bool:
    """Whether a root-level file name matches any non-negated ignore pattern."""
    if not ignore_text:
        return False
    for raw in ignore_text.splitlines():
        pattern = raw.strip()
        if not pattern or pattern.startswith(("#", "!")):
            continue
        pattern = pattern.lstrip("/").rstrip("/")
        if pattern and fnmatch.fnmatch(name, pattern):
            return True
    return False
