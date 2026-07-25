# Playbooks

Playbooks are validated YAML documents discovered from the installed `builtin` directory and
`.vasuki/playbooks`. Project playbooks override built-ins by name.

```bash
vasuki playbooks list
vasuki playbooks show fix-failing-test
vasuki playbooks run fix-failing-test --request "Fix tests/test_invoice.py::test_rounding"
```

Each playbook declares purpose, preconditions, inputs, allowed tools, stages, approval points,
verification, and rollback. Ten built-ins cover API/frontend/database changes, failing tests,
Docker, Compose deployment, server inspection, latency, RabbitMQ backlog, and security review.
