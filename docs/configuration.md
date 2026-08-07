# Configuration

Configuration is two layers. The **global** file at `~/.config/vasuki/config.yaml`
(`$XDG_CONFIG_HOME/vasuki`, or `$VASUKI_CONFIG_HOME`) holds what follows you between
projects: providers, model profiles, routing, and interface preferences. Connect a model once and
every project uses it — a new directory opens straight to work instead of asking again.

The **project** file at `.vasuki/config.yaml` holds what is genuinely local — its name, database,
verification commands, security policy, runtime — and overrides the global layer where the two
disagree, so one repository can still pin a different model deliberately. Values matching the
global layer are not copied into the project file: duplicating them would freeze today's choice
into each repository, so changing the global model later would silently fail to reach it.

API keys for a global provider are stored under `~/.config/vasuki/secrets/`, not inside a
checkout, so deleting a project cannot break every other one.


Project configuration is `.vasuki/config.yaml`, validated with Pydantic. `DATABASE_URL`,
`VASUKI_RUNTIME`, `OPENROUTER_BASE_URL`, `OPENROUTER_MODEL`, `OLLAMA_BASE_URL`, `OLLAMA_MODEL`,
`VLLM_BASE_URL`, and `VLLM_MODEL` override corresponding values. Each `*_BASE_URL`/`*_MODEL` pair
applies to the provider entry of that name (`providers.ollama`, `providers.vllm`,
`providers.openrouter`), so it only takes effect when the provider is named accordingly.
`OPENROUTER_API_KEY`, `OLLAMA_API_KEY`, and `VLLM_API_KEY` add secret references
(`env://OPENROUTER_API_KEY` and so on), not values.

Use `config.example.yaml` as the documented complete example:

```bash
vasuki config show
vasuki config set verification.total_attempts 3
vasuki config set runtime.default local
vasuki config validate
```

Provider configuration fields reject literal values. Configure `env://`, `keyring://`, or
`file://` references. The interactive Providers screen additionally accepts a pasted OpenRouter
key: it checks the key before saving and stores a valid key in the private `.vasuki/secrets`
directory while persisting only its reference. PostgreSQL is enabled by setting `database.url` or
`DATABASE_URL` to a SQLAlchemy PostgreSQL URL and installing the desired database driver.
