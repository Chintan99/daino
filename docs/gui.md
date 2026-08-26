# Browser IDE (GUI)

D[Ai]NO ships a local, VS Code-style browser IDE that runs entirely on your
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
             D[Ai]NO CORE (one agent runtime)
   Agent · LLM · Tools · FS · Shell · Git · MCP · Memory · Sessions
                          │  structured events (EventBus)
             ┌────────────┴───────────────┐
       D[Ai]NO TUI                    Local API (FastAPI)
                                           │ WebSocket
                                       React GUI
```

There is a single agent implementation. The TUI and GUI are both thin consumers
of `MissionApplicationService` and the `EventBus`; the GUI adds only a transport
(FastAPI + WebSocket) and a browser frontend.

## Workspaces

The GUI uses the same palette as the terminal client — a near-black field,
hairline rules, and one jade accent — so the two clients read as one product.
Surfaces stay neutral; the accent marks state (the active tab, a selected row,
the primary action) rather than colouring whole panels.

- **Code** — file explorer with Git indicators, a Monaco editor (tabs, dirty
  state, save, multi-language), an integrated PTY terminal (xterm.js), and the
  panel for output, problems, and test runs.
- **Design** — a blank canvas. Drop an `.html`, `.svg`, image, note, or an
  exported design straight onto it and the file itself lives there. Importing a
  page opens it **full screen** straight away (see below). Architecture diagrams
  (nodes and edges) live on the same canvas, so a sketch and a mock-up can share
  one sheet. Manual edits and agent edits mutate the same stored document under
  `.daino/designs/<id>/design.json`.
- **Preview** — runs your project's dev server (detected from `package.json` /
  `pyproject.toml` / `compose.yaml`, started through the approval flow) and
  embeds the running app in an iframe.
- **Insights** — the browser counterpart of the TUI's workspace views, behind
  one segmented control: the per-prompt **execution map** (models, tools, tests,
  timing, tokens, cost), live and recorded **logs**, comprehensive **QA** scans,
  **missions** and their evidence, **checkpoints**, **approvals**, and
  **repository** intelligence. Each reads through the very same application
  service the TUI renders, so the two clients can never disagree about what
  happened.

The right-side D[Ai]NO agent panel is available in every workspace and shows
streamed responses, tool executions, file edits, test runs, errors, command
approvals, and completion — never raw event JSON. It collapses to a labelled
rail (`⌘I` / `Ctrl+I`) when the canvas or editor needs the width; `⌘B` toggles
the sidebar and `⌘J` the bottom panel. The **?** button in the top right opens
D[Ai]NO's documentation at `/docs` in a new tab.

### Reviewing changes

Source Control lists staged, modified, and untracked paths with stage, unstage,
and discard actions. Selecting one opens a **diff tab beside your code**, not a
drawer beneath it: a side-by-side Monaco diff over the whole file — working tree
against the index, or the index against HEAD for a staged change — so you can
scroll through surrounding context the way an editor shows it. The GUI never
commits or pushes; the agent's own workflow owns that.

### Canvas import and export

Dropping a file keeps it verbatim rather than summarising it. On the canvas,
HTML and SVG render in `sandbox="allow-scripts"` frames that stay pass-through
until you hand them the mouse, so panning still works.

Opening a page — by importing it, double-clicking its card, or pressing ⛶ —
gives it the whole workspace:

- **Preview / Design / Split / Code** switches between the rendered page, the
  visual editor, a stacked preview-over-source layout, and the source alone.
- A **viewport menu** previews at Responsive, Desktop (1440), Laptop (1280),
  Tablet (834), or Mobile (390).
- A **zoom bar** sets the scale yourself: drag the slider, use − / +, press the
  percentage to snap back to **Fit**, or **1:1** for actual size. `⌘`/`Ctrl` +
  scroll over the page zooms too. Whatever the zoom, the frame is laid out to
  fill the window rather than shrinking into a corner of it.
- The **blocks rail collapses** (`‹`) when the page needs the width, and the
  agent panel collapses with `⌘I`.
- **↗** opens the page in a real browser tab, **⟳** reloads the frame, and `Esc`
  returns to the canvas.

#### Editing the page visually

**Design** mode turns the preview itself into the editing surface:

- **Click** anything to select it; a breadcrumb walks up to its ancestors.
- **Drag** an element to move it — a green line shows where it will land.
- **Double-click** text to edit it in place.
- **Drag a block** in from the left rail, or click one to drop it just after the
  selection. The palette covers layout (sections, grids, flex rows), text,
  media, form controls, and whole blocks (hero, nav, card, feature grid, CTA,
  footer, table).
- The right panel edits the selected element's text, link target, image source
  and alt text, classes, alignment, colour, size, padding, and margin, and can
  duplicate, reorder, or delete it.

Visual edits save on their own a moment after you make them — dragging a block
into place is a decision, not a keystroke. Typed source keeps the explicit
**Apply** button, where a half-finished tag should not be written to disk.

While you are editing, the page's own scripts are parked so a page that rewrites
itself on load cannot fight the editor; they are restored verbatim in the saved
source. The frame stays sandboxed without `allow-same-origin` throughout, so the
page can never reach the D[Ai]NO app hosting it — all editing happens inside the
frame and the two sides exchange `postMessage`.

#### Asking D[Ai]NO to change the page

While a page is open it is attached to the agent's context automatically, so
"make the hero headline bigger" already knows which file it means. D[Ai]NO edits
it through the same design artifact the canvas stores, and **the open preview
updates the moment the change lands** — no reload, marked with a brief
*updated by D[Ai]NO* flag.

If you have unsaved edits of your own when D[Ai]NO writes, nothing is discarded:
a banner offers **Load D[Ai]NO's version** or **Keep mine**.

The **Export** menus (on the viewer for one page, on the toolbar for the whole
canvas) offer:

| Export | Contents |
| --- | --- |
| Prototype bundle `.zip` | `index.html`, every other page under `pages/`, images and notes under `assets/`, plus `design.json` and a `README.md` |
| Standalone page `.html` | One self-contained page — the canvas layout, or a single artifact wrapped in a complete document |
| Vector image `.svg` | Boxes, connectors, and embedded artwork |
| Design document `.json` | The canvas itself; drop it on another canvas to restore it |

An artifact can also be downloaded under its original filename or written back
into the repository (**Save to project…**) so the agent can pick it up.

### Agent context

A context bar above the chat box lets you attach precise context — the active
file, a code selection (line range), a selected design node, the page open in
the artifact viewer, terminal output, or a Git diff — instead of dumping whole
files into the prompt. For example, with a
selection attached you can say "Explain this" and D[Ai]NO knows exactly which lines
you mean.

### Approvals

The GUI reuses the exact command permission model as the TUI. When the agent
wants to run a gated command you get an inline card:

```
Approval needed — the command writes outside the workspace
rm -rf build/
[Allow Once]  [Always Allow]  [Reject]
```

Never-approvable commands are refused, and nothing is committed automatically.

### Design → code

Design is deliberately separate from production code. A design change is never
silently turned into repository code; use **Implement Design**, which asks D[Ai]NO
to inspect the repository and propose an implementation plan first.

## Sessions

The TUI and GUI share one session system. Work started with `daino .` can be
reopened with `daino . --gui` and vice versa, and switching between workspaces
keeps the same session.

## Documentation and API reference

The **?** button opens `/docs`: D[Ai]NO's own documentation — these very pages,
rendered by the IDE with a sidebar, search, and an on-page outline, and served
from the markdown that ships with the package. Links between pages work, so it
reads as one manual rather than a folder of files.

The generated OpenAPI reference for the local API lives alongside it at
`/api-docs` (with ReDoc at `/api-redoc`), linked from the documentation header.

## Security

- Default bind is `127.0.0.1`; the file, shell, and terminal APIs are never
  exposed publicly by default. Remote access requires explicit configuration.
- File writes use optimistic concurrency (content hash) and reject stale writes
  with a conflict warning rather than clobbering out-of-band changes.
- QA scans started from the browser skip checks that would need network
  approval rather than silently granting it; approve those from the TUI or CLI.
- Everything Insights shows is built from redacted, structured audit records —
  never private model chain-of-thought.

## Building the frontend

The browser IDE is a compiled React app (in `daino/gui`). **You don't normally
build it by hand:** the first time you run `daino . --gui`, D[Ai]NO builds the
bundle automatically when Node.js (`npm`) is available — a one-time step that
takes a minute; subsequent launches are instant. Node.js 18+ is the only extra
prerequisite for the GUI.

If Node isn't installed, `--gui` still starts the API and shows a page at `/`
explaining how to build it manually:

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

