# Configuration

Project configuration is `.vasuki/config.yaml`, validated with Pydantic. `DATABASE_URL`,
`VASUKI_RUNTIME`, `OPENROUTER_BASE_URL`, `OPENROUTER_MODEL`, `VLLM_BASE_URL`, and `VLLM_MODEL`
override corresponding values. API key environment variables add secret references, not values.

Use `config.example.yaml` as the documented complete example:

```bash
vasuki config show
vasuki config set verification.total_attempts 3
vasuki config set runtime.default local
vasuki config validate
```

Provider key fields reject literal values. Configure `env://`, `keyring://`, or `file://`
references. PostgreSQL is enabled by setting `database.url` or `DATABASE_URL` to a SQLAlchemy
PostgreSQL URL and installing the desired database driver.
