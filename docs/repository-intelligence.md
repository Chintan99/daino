# Repository intelligence

The incremental index records file digest, language, size, summary, imports, symbols, frameworks,
and entrypoints. Queries derive references, API routes, tests, database candidates, environment
variables, Compose services, and dependency maps.

Ignored directories include Git/D[Ai]NO state, virtual environments, dependency caches, and build
artifacts. Binary and oversized files are skipped. Exact source remains on disk; the index contains
compact summaries.

For each task, `ContextCompiler` prioritizes expected/allowed files, request terms, and related
tests. Exact content is admitted until the configured token budget. The included path list is stored
with each model call.

## Build and query the index

Initialization builds the first index. Refresh it after large out-of-band changes and inspect what
D[Ai]NO discovered:

```bash
daino repo index
daino repo status
daino repo map
daino repo symbols
daino repo symbols DocumentService
daino repo references DocumentService
daino repo routes
daino repo databases
daino repo tests
daino repo dependencies
```

Python declarations use the standard AST. Other supported languages use tree-sitter declaration
extraction. The repository map is compact context, not a copy of the code; when a task needs exact
implementation details, the agent reads the ranked files or symbol windows from disk.

The index is also used by `@` completion in the terminal UI, symbol navigation in the browser IDE,
test/source pairing, and the context compiler. Ignored, binary, and oversized files never become
silent model context.
