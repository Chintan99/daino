# Contributing

Use Python 3.12 or newer.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,indexing]'
pytest
ruff check .
ruff format --check .
mypy vasuki
bandit -r vasuki
```

Public interfaces require type hints and docstrings. Keep model/provider/runtime behavior behind
its adapter. New tool calls must return `ToolResult`, be scope-validated, policy-gated where
applicable, and produce auditable output. Tests must be credential-free and deterministic.

Never add secret values, unbounded repair loops, implicit production mutations, automatic pushing,
or completion paths which bypass verification evidence.
