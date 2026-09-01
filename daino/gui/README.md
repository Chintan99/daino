# Daino GUI

The browser IDE frontend for **Daino** — a local, VS Code-style coding agent workspace.
Built with Vite + React + TypeScript.

## Requirements

Node.js 18+ and npm. (Not available in every environment — the sources are written
to compile cleanly with a standard toolchain.)

## Install & build

```bash
npm install
npm run build
```

This type-checks (`tsc`) and produces a static bundle in `daino/gui/dist/`.
The Daino FastAPI backend serves that `dist/` directory at `/` and mounts `/assets`,
so the build is configured with `base: './'` and uses relative `/api` URLs plus a
`window.location`-derived WebSocket URL — no runtime configuration is needed.

## Develop

```bash
npm run dev
```

Starts the Vite dev server on <http://localhost:5173>. `/api` and `/ws` requests are
proxied to the backend at `http://127.0.0.1:4173`, so start the backend first:

```bash
# from the repo root, in another terminal
daino <path-to-project> --gui   # serves the API on 127.0.0.1:4173
```

## Preview a production build

```bash
npm run preview
```

## Architecture

- `src/api/` — typed `fetch` client (`client.ts`), API types (`types.ts`), and
  TanStack Query hooks (`hooks.ts`) for all server state.
- `src/store/` — small, focused Zustand stores (UI layout, editor buffers, agent
  session, design selection, terminals). No single global store.
- `src/ws/` — WebSocket hooks: one shared session socket and per-terminal sockets.
- `src/tabs/` — a tab registry so new workspace tabs (e.g. a future **PLAN** tab)
  can be added without refactoring.
- `src/components/` — `AppShell` and the CODE / DESIGN / INSPECTOR / INSIGHTS workspaces, the
  persistent right-side Agent panel, the bottom panel, and the status/top bars.
