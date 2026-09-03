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

The public documentation is `docs/index.html` — a hand-written landing page — plus one generated
page per Markdown file beside it. `scripts/build_docs_site.py` renders each `docs/*.md` into
`docs/<name>.html` with the site's header, sidebar, contents rail, and stylesheet; rewrites
internal `.md` links to the generated pages; and writes `docs/search-index.json`, which
`docs/docs.js` loads on first use to power the `/` search dialog.

```bash
python scripts/build_docs_site.py          # build pages + search index
python scripts/build_docs_site.py --check  # what CI runs: fail if the build is stale
python scripts/validate_docs_site.py       # every page's links, anchors, and assets

python -m http.server 8000 --directory docs
# open http://127.0.0.1:8000 — search needs a server, not file://
```

**The build output is committed.** Pages may be serving this branch directly rather than the
workflow's artifact, and in that mode there is nothing to build the pages into. `docs/.nojekyll`
is what stops Pages rendering the Markdown itself with its default theme — remove it and every
page reverts to unstyled Jekyll output. Rebuild whenever you touch a `.md` file, `styles.css`, or
`docs.js`; `--check` fails the workflow otherwise.

Adding a page means adding its stem to `GROUPS` in the builder — that tuple is the sidebar and the
previous/next order, and the build refuses to run if a Markdown file is missing from it. The
builder also stamps a content hash onto the `styles.css` and `script.js` links (`styles.css?v=…`);
without it a returning reader gets new markup against the stylesheet their browser already cached,
which is how a screenshot ends up rendering at full size inside a narrow column.

Keep the site styles in `docs/styles.css` and its small interactive layer in `docs/script.js`.
Check the default dark-green palette, the light-theme toggle, installation tabs, copy buttons,
anchor navigation, keyboard controls, and narrow-screen layout. The Markdown files remain the
detailed source for D[Ai]NO's in-app documentation reader.

The `Documentation` GitHub Actions workflow validates pull requests and deploys the `docs/`
directory on pushes to `v2`. For the first deployment, a repository administrator must select
**GitHub Actions** under **Settings → Pages → Build and deployment → Source**.
