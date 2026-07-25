# Repository intelligence

The incremental index records file digest, language, size, summary, imports, symbols, frameworks,
and entrypoints. Queries derive references, API routes, tests, database candidates, environment
variables, Compose services, and dependency maps.

Ignored directories include Git/Vasuki state, virtual environments, dependency caches, and build
artifacts. Binary and oversized files are skipped. Exact source remains on disk; the index contains
compact summaries.

For each task, `ContextCompiler` prioritizes expected/allowed files, request terms, and related
tests. Exact content is admitted until the configured token budget. The included path list is stored
with each model call.
