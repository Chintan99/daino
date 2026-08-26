# Contributing

Use Python 3.12 or newer.

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev,indexing]'
pytest
ruff check .
ruff format --check .
mypy daino
bandit -r daino
```

Public interfaces require type hints and docstrings. Keep model/provider/runtime behavior behind
its adapter. New tool calls must return `ToolResult`, be scope-validated, policy-gated where
applicable, and produce auditable output. Tests must be credential-free and deterministic.

Never add secret values, unbounded repair loops, implicit production mutations, automatic pushing,
or completion paths which bypass verification evidence.

## Browser IDE frontend

The React frontend lives in `daino/gui` (Vite + TypeScript). It talks to the FastAPI backend in
`daino/server` and consumes the same `EventBus` over WebSockets — never add agent logic to the
server or frontend; go through `daino/application` services.

```bash
cd daino/gui
npm install
npm run dev        # Vite dev server on :5173, proxying /api and /ws to the backend on :4173
npm run build      # type-check + build to daino/gui/dist (served by the API)
```

Run the backend it talks to with `daino <path> --gui` in another terminal. Backend GUI tests live in
`tests/integration/test_gui_server.py` (FastAPI `TestClient`, no browser needed).
