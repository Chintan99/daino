# Vasuki

Vasuki is a CLI-first, local-first autonomous software-engineering control plane. It turns a
requirement into persisted requirements, a dependency-aware task plan, isolated Git changes,
verification evidence, an independent review, commits, and an exportable audit bundle. Its model
provider, runtime, repository intelligence, persistence, and deployment layers are independent so
a web control plane or distributed worker can reuse the engine later.

Vasuki is not a code-snippet chatbot. A mission is complete only after its configured commands
pass and its independent review approves the diff.

## Install

Python 3.12 or newer and Git are required. Docker is recommended.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
vasuki --help
```

For development:

```bash
python -m pip install -e '.[dev,indexing]'
pytest
ruff check .
mypy vasuki
bandit -r vasuki
```

## Quick start

```bash
cd your-repository
vasuki init
vasuki doctor
vasuki repo map
vasuki plan "Add validation to the document upload endpoint"
```

The default sandbox is Docker. To explicitly use the local subprocess runtime:

```bash
vasuki config set runtime.default local
```

Local commands are still checked by the policy engine and run without a shell.

## Configure OpenRouter

Secret values stay in the process environment; configuration stores only a reference.

```bash
export OPENROUTER_API_KEY='your-key'
vasuki providers add openrouter \
  --type openrouter \
  --base-url https://openrouter.ai/api/v1 \
  --model anthropic/claude-sonnet-4 \
  --api-key-ref env://OPENROUTER_API_KEY
vasuki providers test openrouter
vasuki models route architect openrouter
vasuki models route planner openrouter
vasuki models route builder openrouter
vasuki models route reviewer openrouter
```

## Configure local vLLM

Start any OpenAI-compatible vLLM server, then:

```bash
export VLLM_API_KEY=''
vasuki providers add local-coder \
  --type vllm \
  --base-url http://127.0.0.1:8000/v1 \
  --model Qwen/Qwen2.5-Coder-7B-Instruct \
  --local
vasuki providers test local-coder
vasuki models route builder local-coder --fallback openrouter
vasuki models route reviewer local-coder
vasuki models route tester local-coder
```

An empty vLLM key is supported. Do not pass a secret value to `--api-key-ref`.

## Run a coding mission

```bash
vasuki run "Add a paginated GET /documents endpoint with unit tests"
vasuki missions list
vasuki missions show <mission-id> --diff
vasuki missions export <mission-id> --format markdown
```

Vasuki records pre-existing uncommitted state, creates
`vasuki/<mission-id>/<description>` in a worktree, creates a checkpoint, applies validated patches,
runs bounded repairs, reviews with fresh context, and commits verified task groups. It does not
merge, push, or modify the original checkout.

## Repository intelligence

```bash
vasuki repo index
vasuki repo status
vasuki repo symbols
vasuki repo references DocumentService
vasuki repo routes
vasuki repo databases
vasuki repo tests
vasuki repo dependencies
```

The index is incremental. Python symbols use the standard AST; supported non-Python languages use
syntax-aware declaration extraction. Only relevant exact files are included in model context.

## Deploy to a remote Compose server

Add a target to `.vasuki/config.yaml`:

```yaml
deployment:
  targets:
    production:
      type: ssh
      host: app.example.com
      port: 22
      username: deployer
      auth:
        key_path: ~/.ssh/id_ed25519
        known_hosts: ~/.ssh/known_hosts
      deployment_path: /opt/apps/example
      strategy: docker-compose
      compose_file: compose.yaml
      environment: production
      health_url: https://app.example.com/health
```

Then inspect, plan, explicitly approve, and verify:

```bash
vasuki deploy inspect --target production
vasuki deploy plan --target production
vasuki deploy apply --target production --approve
vasuki deploy verify --target production
vasuki deploy status --target production
vasuki deploy rollback --target production --approve
```

Inspection is read-only. Releases are uploaded under `releases/<release-id>`. Promotion occurs only
after Compose state and the configured health endpoint pass. A failed release is stopped and the
previous recorded healthy release is restored.

## Security model

- Secret configuration accepts only `env://`, `keyring://`, and `file://` references.
- Secret values are resolved at the provider/SSH boundary and redacted from command output.
- Commands execute as argument vectors locally. Dangerous deletion, database, host, firewall, and
  infrastructure patterns require explicit approval or are denied.
- Network, dependency installation, production deployment, migration, destroy, and rollback are
  permission categories.
- Git worktrees and checkpoints make code changes reversible.
- SSH uses agent/key-path authentication; private-key contents are never read into model context.
- Evidence records model selection reasons, included files, verification, review, commits, and
  rollback points.

See [security documentation](docs/security.md) for the trust boundaries.

## Docker development

```bash
docker build -t vasuki .
docker compose up -d postgres mock-llm
docker compose run --rm vasuki vasuki --help
```

## Current limitations

- The first scheduler intentionally executes one task at a time.
- Repository parsing is deepest for Python and common JavaScript/TypeScript patterns; an LSP adapter
  boundary is reserved but no language server is bundled.
- Compose deployment assumes an existing Linux host, Docker, Compose, target directory permissions,
  and externally managed environment files/TLS/reverse proxy.
- Native cloud architecture generation and Kubernetes are outside this release.
- Browser verification is available as an optional dependency but is only invoked when a project
  supplies browser checks.
- Mission approval is CLI-user attribution in this release; multi-user identity belongs in the
  future control plane.

## Documentation

- [Architecture](docs/architecture.md)
- [Configuration](docs/configuration.md)
- [Providers](docs/providers.md)
- [Model routing](docs/model-routing.md)
- [Repository intelligence](docs/repository-intelligence.md)
- [Runtimes](docs/runtimes.md)
- [Deployment](docs/deployment.md)
- [Playbooks](docs/playbooks.md)
- [Security](docs/security.md)
- [Contributing](docs/contributing.md)
