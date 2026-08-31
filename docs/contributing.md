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

## Documentation site

The public documentation is a dependency-free single-page site in `docs/index.html`. Preview and
validate it locally:

```bash
python -m http.server 8000 --directory docs
# open http://127.0.0.1:8000

python scripts/validate_docs_site.py
```

Keep the site styles in `docs/styles.css` and its small interactive layer in `docs/script.js`.
Check the default dark-green palette, the light-theme toggle, installation tabs, copy buttons,
anchor navigation, keyboard controls, and narrow-screen layout. The Markdown files remain the
detailed source for D[Ai]NO's in-app documentation reader.

The `Documentation` GitHub Actions workflow validates pull requests and deploys the `docs/`
directory on pushes to `v2`. For the first deployment, a repository administrator must select
**GitHub Actions** under **Settings → Pages → Build and deployment → Source**.
