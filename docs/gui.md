# Browser IDE (GUI)

Daino ships a local, VS Code-style browser IDE that runs entirely on your
machine and is driven by the **same** agent runtime as the terminal UI. There is
no cloud component; the API server binds to `127.0.0.1` only.

```bash
daino .                 # terminal UI (default)
daino . --tui           # terminal UI, explicit
daino . --gui           # browser IDE at http://127.0.0.1:4173
daino . --gui --port 5000
daino . --gui --no-browser   # start the server without opening a browser
```

`--gui` resolves the project (initializing it like `daino init` on first run),
starts a local FastAPI + WebSocket server, opens your browser, and keeps backend
logs in the terminal. Press `Ctrl+C` to stop; child processes (terminals,
preview servers) are terminated on shutdown.

## Architecture

```
              DAINO CORE (one agent runtime)
   Agent · LLM · Tools · FS · Shell · Git · MCP · Memory · Sessions
                          │  structured events (EventBus)
             ┌────────────┴───────────────┐
        Daino TUI                     Local API (FastAPI)
                                           │ WebSocket
                                       React GUI
```

There is a single agent implementation. The TUI and GUI are both thin consumers
of `MissionApplicationService` and the `EventBus`; the GUI adds only a transport
(FastAPI + WebSocket) and a browser frontend.

## Workspaces

- **Code** — file explorer with Git indicators, a Monaco editor (tabs, dirty
  state, save, multi-language), an integrated PTY terminal (xterm.js), Git
  status/diff (Monaco diff editor), and the persistent Daino agent panel.
- **Design** — structured, editable, AI-generated diagrams (architecture,
  flowchart, database, API flow) on a React Flow canvas, plus UI/prototype
  artifacts. Manual edits and agent edits mutate the same stored document under
  `.daino/designs/<id>/design.json`.
- **Preview** — runs your project's dev server (detected from `package.json` /
  `pyproject.toml` / `compose.yaml`, started through the approval flow) and
  embeds the running app in an iframe.

The right-side Daino agent panel is available across all three workspaces and
shows streamed responses, tool executions, file edits, test runs, errors,
command approvals, and completion — never raw event JSON.

### Agent context

A context bar above the chat box lets you attach precise context — the active
file, a code selection (line range), a selected design node, terminal output, or
a Git diff — instead of dumping whole files into the prompt. For example, with a
selection attached you can say "Explain this" and Daino knows exactly which lines
you mean.

### Approvals

The GUI reuses the exact command permission model as the TUI. When the agent
wants to run a gated command you get an inline card:

```
Daino wants to run:  rm -rf build/
[Allow Once]  [Always Allow]  [Reject]
```

Never-approvable commands are refused, and nothing is committed automatically.

### Design → code

Design is deliberately separate from production code. A design change is never
silently turned into repository code; use **Implement Design**, which asks Daino
to inspect the repository and propose an implementation plan first.

## Sessions

The TUI and GUI share one session system. Work started with `daino .` can be
reopened with `daino . --gui` and vice versa, and switching between Code/Design/
Preview keeps the same session.

## Security

- Default bind is `127.0.0.1`; the file, shell, and terminal APIs are never
  exposed publicly by default. Remote access requires explicit configuration.
- File writes use optimistic concurrency (content hash) and reject stale writes
  with a conflict warning rather than clobbering out-of-band changes.

## Building the frontend

The React frontend lives in `daino/gui`. Build it once so `--gui` can serve it:

```bash
cd daino/gui
npm install
npm run build      # emits daino/gui/dist, served by the API at /
```

For frontend development, run the Vite dev server (it proxies the API):

```bash
cd daino/gui
npm run dev        # http://127.0.0.1:5173, proxying the backend on 4173
```

If `daino/gui/dist` is absent, the server still runs and the API is fully usable
(see `/docs`); it just returns a short notice at `/` until the frontend is built.
```
