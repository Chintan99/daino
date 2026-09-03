# Configuration

Configuration is two layers. The **global** file at `~/.config/daino/config.yaml`
(`$XDG_CONFIG_HOME/daino`, or `$DAINO_CONFIG_HOME`) holds what follows you between
projects: providers, model profiles, routing, and interface preferences. Connect a model once and
it becomes available everywhere; new-project onboarding explicitly lets you inherit it or choose
project-specific model settings.

The **project** file at `.daino/config.yaml` holds what is genuinely local — its name, database,
verification commands, security policy, runtime — and overrides the global layer where the two
disagree, so one repository can still pin a different model deliberately. Values matching the
global layer are not copied into the project file: duplicating them would freeze today's choice
into each repository, so changing the global model later would silently fail to reach it.

API keys for a global provider are stored under `~/.config/daino/secrets/`, not inside a
checkout, so deleting a project cannot break every other one.

Use `/globalprovider` to configure the shared provider from any workspace. `/provider` edits the
current project's override and includes **Use global settings**, which removes those overrides and
immediately restores the shared provider, model profiles, and routing.

Memory policy is configured under `memory`. Durable project/task state uses the project database;
cross-project user preferences and global `DAINO.md` live privately under `~/.daino` (override
with `DAINO_HOME`). Embeddings default to `disabled`; lexical and metadata retrieval still work.
See the complete [memory architecture](memory.md#configuration) and `config.example.yaml`.

Model profiles accept `execution_mode` (`auto`, `compact`, or `standard`),
`initial_context_tokens`, `max_agent_steps`, `no_progress_limit`, and `staged_retrieval`.
Compact mode is designed for small/local coding models: it uses a bounded task packet and expands
source or memory only through tools. See [small-model execution profiles](model-routing.md#small-model-execution-profiles).


Project configuration is `.daino/config.yaml`, validated with Pydantic. `DATABASE_URL`,
`DAINO_RUNTIME`, `OPENROUTER_BASE_URL`, `OPENROUTER_MODEL`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL`,
`VLLM_BASE_URL`, and `VLLM_MODEL` override corresponding values. Each `*_BASE_URL`/`*_MODEL` pair
applies to the provider entry of that name (`providers.ollama`, `providers.vllm`,
`providers.openrouter`), so it only takes effect when the provider is named accordingly.
`OPENROUTER_API_KEY`, `OLLAMA_API_KEY`, and `VLLM_API_KEY` add secret references
(`env://OPENROUTER_API_KEY` and so on), not values.

Use `config.example.yaml` as the documented complete example:

```bash
daino config show
daino config set verification.total_attempts 3
daino config set runtime.default local
daino config validate
```

Provider configuration fields reject literal values. Configure `env://`, `keyring://`, or
`file://` references. The interactive Providers screen additionally accepts a pasted OpenRouter
key: it checks the key before saving and stores a valid key in the private `.daino/secrets`
directory while persisting only its reference. PostgreSQL is enabled by setting `database.url` or
`DATABASE_URL` to a SQLAlchemy PostgreSQL URL and installing the desired database driver.

## Budget

Every model call's cost has always been recorded. These are the settings that
make it *enforced*. Each defaults to `0`, meaning unlimited, so an existing
project behaves exactly as it did — the gap was never a missing default, it was
the absence of any way to set one.

```yaml
budget:
  max_cost_usd: 5.0        # per mission; 0 is unlimited
  max_total_tokens: 500000
  max_model_calls: 200
  warn_at_fraction: 0.8    # warn once at 80% of the tightest ceiling
```

The stall guard already ends an *unproductive* run in about a dozen actions.
These bound the other case: an agent making genuine, non-repeating progress on a
task it will never finish. A run that hits a ceiling stops the way a step limit
does — incomplete, with a reason, and with whatever already landed in the working
tree reported rather than discarded.

A mission's budget is shared by everything it spawns: a pinned session, a team's
nine members, and every delegated subagent draw on one account, not nine.

`max_cost_usd` binds only where the provider reports a charge. A local Ollama or
a self-hosted vLLM reports none and has none, so use `max_total_tokens` there.

## Web search

`web_search` scrapes DuckDuckGo by default, which needs no account and is the
option most likely to break — it is a scrape of an endpoint that discourages
scraping. The alternatives are documented APIs:

```yaml
web:
  provider: brave          # duckduckgo | brave | tavily | searxng | google-pse
  api_key: env://BRAVE_SEARCH_API_KEY
```

`searxng` needs only `base_url` and keeps queries inside your network; the
private-address block is lifted for exactly that hostname and nothing else, and
a redirect away from it is validated normally. `google-pse` needs `api_key` and
`engine_id`. Whichever backend answers, the SSRF validation, redirect
revalidation, byte ceiling and content-type check are the same single
implementation all network access passes through.

## Tracing

Set an OTLP endpoint and D[Ai]NO emits spans around every model call, loop step,
tool execution, and delegation:

```yaml
observability:
  otel_endpoint: http://localhost:4318/v1/traces
```

`OTEL_EXPORTER_OTLP_ENDPOINT` is honoured when the setting is absent, so a
deployment that configures every other service through the standard variable gets
these too. Spans carry identity and cost — role, model, profile, token counts,
latency — and never prompts, file contents, or command output. A trace collector
is usually the least access-controlled sink in a deployment, and shipping source
code to it would be a leak dressed as observability.

Tracing degrades to nothing when the OpenTelemetry SDK is absent or the collector
is unreachable. A mission must not fail because a collector went away.

## Browser IDE

`daino . --gui` starts the local browser IDE. It binds `127.0.0.1` by default (never `0.0.0.0`) on
port `4173`; override with `--host` / `--port` (port `0` picks a free port), and `--no-browser`
suppresses auto-opening a browser. Remote access requires deliberate configuration. See the
[browser IDE guide](gui.md).

## Migrating from Vasuki

D[Ai]NO was renamed from Vasuki. Configuration and state are resolved **read-legacy / write-new**, and
legacy data is never moved or deleted:

- Project state uses `.daino/`, but a checkout that already has `.vasuki/` keeps using it in place
  (its `config.yaml`, database, and sessions stay together).
- Global config/memory prefer `~/.config/daino` and `~/.daino`, falling back to `~/.config/vasuki`
  and `~/.vasuki` when only those exist.
- Environment variables `DAINO_CONFIG_HOME`, `DAINO_HOME`, and `DAINO_RUNTIME` are read first, with
  the `VASUKI_*` equivalents still honoured.
- Instruction files: `DAINO.md` is discovered first; a legacy `VASUKI.md` is still read where no
  `DAINO.md` exists at the same level.
- The default database is `.daino/daino.db`; an existing `.vasuki/vasuki.db` is opened in place.
