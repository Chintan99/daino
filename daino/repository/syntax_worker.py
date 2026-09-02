"""Tree-sitter extraction, in a process that is allowed to die.

Run as ``python -m daino.repository.syntax_worker <root>``. Reads one
repository-relative path per line on stdin and writes one JSON result per line
on stdout.

Why this exists: the grammars are third-party native code loaded with dlopen,
and on some interpreter/library combinations they corrupt the heap after a few
hundred files. The symptom is the process dying with SIGSEGV or SIGBUS — no
exception, no traceback, nothing a ``try`` can catch, and if it happens in the
server process the server is simply gone.

A crash here costs one file and one restart. The parent
(:func:`daino.repository.syntax.extract_outlines`) notices the closed pipe,
records that file as unparsed so the regex fallback covers it, and starts a
fresh worker. That is the whole point of the split: not performance, not
parallelism — survivability.

The protocol is deliberately line-based and one-request-at-a-time. Batching
would be faster and would also mean a crash lost the whole batch, which is
exactly the property being bought here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

#: Matches the indexer's own ceiling. A file past it is generated or data.
MAX_BYTES = 400_000


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: syntax_worker <root>", file=sys.stderr)
        return 2
    root = Path(argv[1]).resolve()
    # Imported after the argument check so a usage error does not pay for
    # loading the grammars.
    from daino.repository.syntax import extract_outline

    for line in sys.stdin:
        relative = line.strip()
        if not relative:
            continue
        result: dict[str, object] = {"path": relative, "symbols": [], "parser": ""}
        try:
            target = root / relative
            data = target.read_bytes()
            if len(data) <= MAX_BYTES:
                outline = extract_outline(relative, data)
                if outline is not None:
                    result["parser"] = outline.parser
                    result["symbols"] = [
                        {
                            "name": symbol.name,
                            "kind": symbol.kind,
                            "path": symbol.path,
                            "line": symbol.line,
                        }
                        for symbol in outline.symbols
                    ]
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the run
            result["error"] = f"{type(exc).__name__}: {exc}"
        sys.stdout.write(json.dumps(result) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
