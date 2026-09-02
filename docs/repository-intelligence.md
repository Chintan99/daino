# Repository intelligence

The incremental index records file digest, language, size, summary, imports, symbols, frameworks,
and entrypoints. Queries derive references, API routes, tests, database candidates, environment
variables, Compose services, and dependency maps.

Ignored directories include Git/D[Ai]NO state, virtual environments, dependency caches, and build
artifacts. Binary and oversized files are skipped. Exact source remains on disk; the index contains
compact summaries.

For each task, `ContextCompiler` ranks candidate files by **import distance** from what the task
actually names, and admits exact content until the configured token budget. The included path list
is stored with each model call.

Ranking works outward from a small set of seeds — the task's expected/allowed files, then any path
written out in the task text — along the import edges the index already records:

| Rank | Relationship to the task |
| --- | --- |
| nearest | defines a symbol the task names |
| | a seed imports it, or it imports a seed |
| | reached through a package re-export a seed imports |
| | sits beside a scoped file that does not exist yet |
| | imported by one of the seed's own dependencies |
| furthest | matches a word in the task text and nothing more |

The word match is the floor, not the primary signal. On D[Ai]NO's own repository it alone matches a
third of all files, so filling the budget from it in filesystem order gave a compact profile's four
slots to whichever unrelated files sorted first. A file's own tests follow it directly, so a budget
that admits a file admits its tests.

Only *internal* imports become edges — resolution is membership in the index, so retrieval can never
name a path that does not exist. Package entry points are followed through to what they re-export,
because a `__init__.py` that only re-exports carries no logic. Files imported by an unusually large
number of others are not expanded backwards: "everything that imports the schemas package" is the
repository rather than a signal. A language whose outline extraction records no imports simply has
no edges, and retrieval falls back to the word match.

Files the budget could not take are named in the bundle's `omitted_context` when they are direct
collaborators, and counted otherwise, so the agent knows to reach for `read_file` rather than
assuming a file is absent.

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
