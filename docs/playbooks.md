# Playbooks

Playbooks are validated YAML documents discovered from the installed `builtin` directory and
`.daino/playbooks`. Project playbooks override built-ins by name.

```bash
daino playbooks list
daino playbooks show fix-failing-test
daino playbooks run fix-failing-test --request "Fix tests/test_invoice.py::test_rounding"
```

Each playbook declares purpose, preconditions, inputs, allowed tools, stages, approval points,
verification, and rollback. Ten built-ins cover API/frontend/database changes, failing tests,
Docker, Compose deployment, server inspection, latency, RabbitMQ backlog, and security review.

## Built-in playbooks

The installed set includes:

- `add-fastapi-endpoint`
- `add-react-component`
- `create-database-migration`
- `fix-failing-test`
- `debug-docker-build`
- `deploy-docker-compose`
- `inspect-remote-server`
- `investigate-api-latency`
- `debug-rabbitmq-backlog`
- `review-security-sensitive-change`

Use `daino playbooks show <name>` to inspect the exact stages and gates in your installed version.

## Add a project playbook

Create `.daino/playbooks/<name>.yaml` with these fields:

```yaml
name: verify-api-contract
version: "1.0"
purpose: Verify that an API change preserves the published contract.
preconditions:
  - The project contains an OpenAPI document.
required_inputs:
  - The endpoint or schema being changed.
allowed_tools:
  - repository.read
  - runtime.test
execution_stages:
  - Inspect the current contract and implementation.
  - Apply the smallest compatible change.
approval_points:
  - Approve implementation after the compatibility plan.
verification_steps:
  - Run API tests and schema validation.
rollback_steps:
  - Restore the pre-change checkpoint.
```

Project definitions override an installed playbook with the same `name`. Loading is strict: a
missing or wrongly typed field fails validation instead of starting a partially defined workflow.
Running a playbook creates a normal specification-mode mission, so worktree isolation, approvals,
verification, review, evidence, and non-push behavior are unchanged.
