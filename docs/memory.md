# Memory architecture

Vasuki memory is local-first and selective. It does not make the model remember every prior
message; `ContextBuilder` decides which instructions, task state, memories, source files, and recent
conversation are useful for the current call. The agent still works when embeddings are disabled.

## Layers and storage

| Layer | Lifetime | Storage | Contents |
|---|---|---|---|
| Working | Current process/task | `WorkingMemory`, checkpointed incrementally | Goal, plan, current/completed/pending steps, inspected/changed files, commands, important output, tests, errors, questions and hypotheses |
| Persistent task | Restarts and disconnects | Project SQLite `persistent_task_states` | Crash-safe copy of working state, repository, branch, mission/session IDs and timestamps |
| Procedural | Until the user edits it | `~/.vasuki/VASUKI.md` and repository `VASUKI.md` files | Explicit human instructions scoped by directory |
| Project semantic | Across sessions in one repository | Project SQLite `memory_records` | Small atomic facts with source, confidence, importance and staleness metadata |
| Decision | Across sessions in one repository | Typed `memory_records` | The decision, reason, alternatives, source and active/superseded state |
| Failure/solution | Across relevant future errors | Typed `memory_records` | Normalized error/fingerprint, cause, successful fix and useful failed attempts |
| Episodic | Task/milestone boundaries | `memory_episodes` plus a retrievable episode summary | Goal, major work, discoveries, decisions, changes, commands, tests, outcome and unresolved work |
| User | Across repositories | Private `~/.vasuki/memory.db` | Conservative, explicit cross-project preferences |

`VASUKI_HOME` overrides `~/.vasuki`. A configured `VASUKI_CONFIG_HOME` is also honored so portable
and test installations keep all user state together. User-memory directories and databases are
created with private permissions. Project operational state remains under `<repo>/.vasuki/` and is
ignored by Git.

Project IDs use a normalized `origin` remote when one exists and a resolved path otherwise. They
are hashes, so repositories with the same folder name do not share memory; remote-backed checkouts
retain identity when moved. Git branch/revision and source-file digests are recorded where useful,
but Git is not required.

## SQLite schema and migration

Alembic revision `0005_memory_system` adds:

- `memory_records`: type, scope, project/task/session IDs, atomic content and summary, importance,
  confidence, source/source type, tags, lifecycle status, access/verification timestamps and count,
  repository revision, source digest, replacement ID, rationale, and legacy compatibility fields;
- `memory_embeddings`: provider/model/dimensions/vector, keyed by memory ID and kept separate so an
  embedding implementation or storage format can change independently;
- `persistent_task_states`: the complete incrementally written task/working-state envelope and its
  latest compacted context;
- `memory_episodes`: structured session/task outcomes rather than transcripts.

Run normal Alembic upgrades with `alembic upgrade head`. Startup also performs an idempotent,
additive bridge for older SQLite `memory_records` tables. This matters after an abrupt update:
`create_all()` alone cannot add columns, and restart recovery must not depend on a separate manual
step. New tables are still versioned by Alembic.

All access goes through `MemoryManager`; agents never receive a database path or SQL tool. The
legacy `MemoryStore` remains compatible for existing architecture-decision callers.

## VASUKI.md hierarchy

Vasuki resolves:

1. `~/.vasuki/VASUKI.md` (global),
2. `<repository>/VASUKI.md` (repository),
3. every `VASUKI.md` from the repository root down to the directory containing each target file.

For `backend/auth/service.py`, `backend/VASUKI.md` applies; `frontend/VASUKI.md` does not. Parent
layers are rendered before closer layers and every block is labeled with its scope. An explicit
`key: value` or `key = value` directive in a closer file replaces the broader value rather than
putting both conflicting values in the prompt. Other prose remains in its labeled layer, with the
closest applicable file authoritative on conflict. Files outside the repository are never read as
project instructions, and oversized or undecodable instruction files are ignored safely.

Current explicit user instructions have higher procedural priority than every VASUKI.md. They are
placed in a final, labeled layer so the resolution is inspectable.

## Authority and conflict handling

The effective authority order is:

```text
current repository/source code
> current explicit user instruction
> closest scoped VASUKI.md
> repository VASUKI.md
> global VASUKI.md
> active explicit project decisions
> verified project semantic memory
> episodic/session memory
> global automatically learned memory
```

Repository facts are not overridden by memory. A fact derived from a file records that file's
SHA-256 digest. Retrieval checks the current file cheaply; if it changed or disappeared, the
memory becomes `stale`, is omitted by default, and remains inspectable for historical reasoning.
Verification refreshes its digest. A corrected fact can supersede the old one, preserving the old
record as `superseded` and linking it to the replacement. Archived and superseded records do not
enter normal context. An agent must surface an active conflicting user decision instead of silently
replacing it.

## Retrieval algorithm

Retrieval is two-stage and bounded. SQLite first filters at most a small candidate set by project,
scope, type, task/session association and lifecycle status. Ranking then combines:

- lexical term coverage and exact-phrase match;
- optional cosine similarity from the configured embedding adapter;
- project and current-task affinity;
- a decision boost, or a failure/fingerprint boost when current output resembles an error;
- importance, confidence, recency/decay, access history and source reliability;
- a strong stale penalty and an unrelated-memory penalty.

Metadata quality orders relevant candidates but cannot make an unrelated high-importance memory
relevant by itself. Project memories normally outrank global ones. Only the configured top N that
fit `memory.max_context_tokens` reach `ContextBuilder`. Debug retrieval records the score
components and emits a redacted `memory_retrieved` audit event explaining selection.

`EmbeddingProvider` is a small protocol. The built-in disabled provider returns no vectors;
`CallableEmbeddingProvider` adapts a local model or OpenAI-compatible client. Lexical and metadata
ranking remain fully operational without either.

## Compaction, promotion, and extraction

When loop context reaches `memory.compaction_threshold` of the selected model input budget, old
tool exchanges become one structured state block while the original task and complete recent tool
groups remain. The block preserves goal and requirements, plan/progress, files, decisions,
instructions, tests, errors, unresolved work, hypotheses and next action. A `ContextCompacted`
event is persisted. Persistent task compaction stores the same contract for restart recovery.

For models using a compact execution profile, `ContextBuilder` also produces a `TaskPacket`. It is
a model-independent handoff, not another memory store: objective, acceptance checks, explicit
constraints, active decisions, relevant files, completed/pending steps, current errors,
verification commands, and one next action. Initial retrieval is deliberately shallow. The model
can request omitted source with `read_file`/`grep` and omitted durable context with
`memory_search`; those validated tools never expose the database itself. This lets a small model
resume the same persisted task as a larger model without inheriting an oversized transcript.

Memory lifecycle is `scratch -> session -> project -> global`. `promote()` and `demote()` move a
record through the service and emit audit events. Automatic extraction is deliberately
conservative: explicit durable cross-project preferences and clear project architecture decisions
are candidates; transient values, speculation, raw tool output and ordinary conversation are not.
Episodes are created at meaningful task completion boundaries. Failure memories are created only
when a cause and successful fix are known.

Before every write, likely API keys, passwords, tokens, authorization headers, JWTs, private keys,
AWS access keys and credentialed database URLs are redacted. `.env` source content is omitted.
Stored command output is short, sanitized and structured rather than copied wholesale.

## Configuration

```yaml
memory:
  enabled: true
  auto_save: true
  auto_extract: true
  auto_resume: false
  max_retrieved_items: 8
  max_context_tokens: 2000
  compaction_threshold: 0.80
  embedding_provider: disabled # disabled, local, openai-compatible
  embedding_model: ""
  embedding_base_url: ""
  embedding_api_key: "" # env://, keyring://, or file:// reference
  decay_enabled: true
  decay_half_life_days: 180
  user_memory_enabled: true
  failure_memory_enabled: true
```

## User and agent controls

TUI commands:

```text
/memory
/memory search websocket disconnect
/memory project
/memory decisions
/memory failures
/memory user
/memory forget memory-...
/memory verify memory-...
/memory clear-session
/memory clear-project
/tasks
/resume mission-...
```

Headless equivalents include `vasuki memory list`, `search`, `forget`, `verify`, and
`clear-project`. Listings show type, scope, lifecycle status, source and confidence, so users can
understand what is remembered and why. The validated agent surface provides `memory_search`,
`memory_save`, `memory_update`, `memory_forget`, `memory_list`, and `memory_verify`; global saves are
rejected unless their text is an explicit cross-project preference.

## Multi-session example

```text
Session 1
  Learns: "The backend framework is Flask."
  Source: backend.toml (digest A)
  Starts mission-migration and persists its first inspected file.

Restart / Session 2
  Startup offers "Migrate Flask to FastAPI" as resumable.
  Retrieval finds the Flask fact for a backend-framework question.

Repository changes
  backend.toml now says framework = "fastapi" (digest B).

Session 3
  Retrieval notices B != A, marks the Flask fact stale, and excludes it.
  Repository content wins. A verified FastAPI fact supersedes the old record.
```

This scenario is executable in `tests/integration/test_memory_multisession.py`.
