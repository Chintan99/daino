# Browser IDE (GUI)

D[Ai]NO ships a local, VS Code-style browser IDE driven by the **same** agent runtime, sessions, and
event bus as the [terminal UI](tui.md). There is no cloud component — the API server binds to
`127.0.0.1` only.

```bash
daino . --gui                # browser IDE at http://127.0.0.1:4173 (background)
daino . --gui --port 5000    # choose a port (--port 0 picks a free one)
daino . --gui --no-browser   # start the server without opening a browser
daino . --gui --foreground   # keep server logs in this terminal
```

`--gui` resolves the project (initializing it on first run), starts a background FastAPI + WebSocket
server, and opens your browser. Manage background servers with `daino ps` (session id, project dir,
start time) and `daino kill .` (stop the current project's server); logs live in the project's
`.daino/` directory. With `--foreground`, `Ctrl+C` stops it and terminates child terminals and
preview servers.

The first `--gui` run builds the React bundle automatically when Node.js 18+ (`npm`) is available —
a one-time step of about a minute; later launches are instant. Without Node, the API still starts and
`/` explains how to `npm install && npm run build` in `daino/gui` by hand.

## Layout

The window is two chrome rows — an application menu over the workspace tabs — with the D[Ai]NO agent
panel on the right in every workspace. It uses the same near-black palette and single jade accent as
the terminal client, so the two read as one product.

| Menu | Holds |
|---|---|
| **File** | New file/folder, open by path, save, save all, revert, close editors, copy path, re-read config, new conversation |
| **Edit** | Undo/redo, find, replace, find in files, toggle comment, format, fold/unfold, select all |
| **Go** | Go to file/line/symbol, next/previous editor, Explorer, Search, Source Control, execution map, workspaces, inspection report |
| **View** | Switch workspace, toggle sidebar/panel/agent, choose panel view, interface zoom |
| **Run** | Start/stop the app, run/cancel an inspection, index the repository, stop the running agent turn |
| **Terminal** | New terminal, clear, switch shell, kill one or all, show panel |
| **Settings** | Preferences and project/agent configuration (below) |
| **Help** | Documentation, API reference, keyboard shortcuts, about |

Items grey out when they cannot act rather than failing after the click.

### Workspaces (top tabs)

- **Code** — file explorer with Git indicators, a Monaco editor (tabs, dirty state, multi-language),
  an integrated PTY terminal (xterm.js), and a panel for output, problems, and test runs.
- **Design** — a canvas. Drop an `.html`, `.svg`, image, note, or exported design and the file lives
  there; architecture diagrams (nodes and edges) share the same sheet. A **left panel lists canvases
  and the folder's files**. Manual and agent edits mutate the same document under
  `.daino/designs/<id>/design.json`. See [the visual editor](#the-visual-html-editor).
- **Workspace** — the work that is not code: documents, research, planning, and analysis. A
  workspace is a goal plus a real folder in your project (`.daino/workspaces/<name>/`), so its
  documents are ordinary files — greppable by the agent and openable in CODE — while staying out of
  your source tree and your diffs. **Run Plan** executes the plan a step at a time, steerable from
  the chat and pausable mid-plan; **CHANGES** reviews what it wrote. See [Workspace](workspace.md).
- **Inspector** — the pre-production check, in three views. **Scan** runs end-to-end QA and a
  vulnerability assessment and answers one question: can this be pushed? **Review** reads one change
  — working tree, staged, or this branch against its base — and answers whether it can be merged.
  **Live app** runs your project's dev server (detected from `package.json` / `pyproject.toml` / `compose.yaml`, started
  through the approval flow), embeds the running app, and becomes the scan's live target. See
  [the Inspector](#the-inspector).
- **Insights** — the browser counterpart of the TUI's views behind one segmented control: per-prompt
  **execution map** (models, tools, tests, timing, tokens, cost), live and recorded **logs**,
  **missions** and evidence, **checkpoints**, **approvals**, and **repository** intelligence.

## The agent panel

The right panel shows streamed responses, tool executions, file edits, test runs, errors, approval
cards, and completion — never raw event JSON. It collapses to a rail (`⌘I` / `Ctrl+I`) when the
canvas or editor needs the width. Under its header:

- A **runner** — a dinosaur mirroring the terminal client's, driven by the same activity states:
  running with `THINKING`/`PLANNING`/`INSPECTING`/`BUILDING`/`VERIFYING` in flight, green
  `TASK COMPLETED` when done, red `ERROR` with the reason on failure, `READY` when idle. It stops
  entirely when idle and honours `prefers-reduced-motion`.
- The **model picker** and a **gear** for [provider setup](#choosing-a-model).

### The composer

| Control | Key | What it does |
|---|---|---|
| Autonomy | `⇧⇥` | Plan → Ask → Session → Full, coloured per mode (`full` outlined in red) |
| Model | `⌘M` | Steps to the next configured profile, pinning it on the session |

**Slash commands** — type `/` to open a menu of the [full command catalog](#slash-commands); pick
with the arrows and Enter. Commands the browser can carry out itself (opening a view, clearing the
chat, stopping a turn) act immediately; the rest are sent to the agent. `/effort` opens an inline
picker to choose the reasoning level.

**Attachments** — drop files on the composer, paste them (a pasted screenshot is attached, not
pasted as text), or use the paperclip. Each is stored under `.daino/attachments/` and attached as a
*path*, so the agent opens it with its own tools and it never shows up as an untracked file in your
diff. Cap: 8 MB and 10 files per message. (The provider layer sends text, so no model reads images
yet — the path is still useful for "optimise the screenshot at …".)

**Context bar** — attach precise context instead of dumping whole files: the active file, a code
selection (line range), a selected design node, the open page, terminal output, or a Git diff. With
a selection attached, "Explain this" knows exactly which lines you mean.

### Stop, tasks, and live changes

The **Stop** button cancels the running turn immediately — its status shows *Stopping…* and then
stopped, and only an explicit Stop cancels a turn (see [reloading](#reloading-during-a-turn)). While
a turn runs, three things track it: the runner's label names the current step and file; a
collapsible **Tasks** panel holds the agent's checklist with a `done/total` count (shown only while
running, since a leftover plan reads as unfinished work); and a live **Editing N files** card
accumulates every file touched with running `+`/`-` counts. Each finished checklist item adds one
line to the transcript (`✓ …`); a failed item is red.

### The changeset

A turn that edited anything closes with one changeset card:

```
Edited 6 files  +49 -81
  README.md                          +15 -15
  docs/assets/relevance-heatmap.svg   +2 -2
```

The filename opens the file, **diff** opens that file's diff beside the code, **Review** opens all of
them, and files past the first three collapse behind *Show N more*. A file edited twice is one row
with totals summed; a turn whose verification failed is marked `unverified`. This is the same summary
the TUI prints, so the clients never disagree.

## The Inspector

The Inspector answers the question you ask before a production push: *is this safe to ship?* It runs
without a model configured — the offline audit, the project's own commands, and the live probe are
all deterministic — and uses one when you have one, for the reviewer specialists.

### Scan

Pick a profile, optionally give it a live target, and press **Run inspection**.

| Profile | What it runs |
|---|---|
| **Full** | Everything below |
| **Quality** | Lint, types, tests, Playwright, and the architecture / code-quality / frontend / backend reviewers |
| **Security** | The vulnerability assessment only: built-in audit, SAST, dependency and secret scanners, live probe, and the security / threat-model / supply-chain reviewers |

Evidence comes from four places, and each appears in the report as its own check:

- **Built-in security audit** — no tools required. Reads the working tree once and applies a fixed
  rule table: credential shapes (with placeholders and environment lookups discarded), insecure code
  patterns per language (shell injection, unsafe deserialization, disabled TLS verification, dynamic
  evaluation, string-built SQL, DOM injection), and weak configuration (container privilege, root
  images, open ingress CIDRs, public buckets, CI workflows that mix untrusted input with secrets).
- **The project's own commands** — lint, types, tests, build, Playwright.
- **Installed scanners** — `bandit`, `gitleaks`, `semgrep`, `osv-scanner`, `trivy`, `pip-audit`,
  `npm audit`, `cargo-audit`, `govulncheck`. None is a dependency of Daino: whichever are on the
  host are run, and the rest are listed as skipped so the report never implies coverage it lacks.
- **The live probe** — `GET`/`HEAD`/`OPTIONS` against a running app. Security headers, cookie flags,
  exposed paths (`/.env`, `/.git/config`, `/actuator/env`, …), error pages that leak stack traces,
  reflected-origin CORS, and advertised HTTP methods. It never sends a payload and never mutates
  state.

Everything they produce is normalised into one **findings** list — severity, CWE, location,
remediation, and the source that reported it. One weakness seen by two sources is merged into one
row that credits both.

### The verdict

The report leads with a release gate, and states every reason behind it:

| Verdict | When |
|---|---|
| **Safe to push** | No confirmed critical or high finding, and no failing test or quality check |
| **Review before push** | High or medium findings to triage, failing quality checks, or security evidence that could not be collected |
| **Do not push** | A confirmed critical finding, a failing test, or a cluster of high findings |

The gate is deterministic — it reads findings and checks, never a model's opinion — so the same
evidence always produces the same answer. Low-confidence findings (a credential shape inside a test
fixture, for instance) stay in the report but can never be what blocks a release.

When the scan lands, your desktop gets a notification carrying the verdict, the browser tab is
marked if you are looking elsewhere, and the **INSPECTOR** tab keeps a coloured dot showing whether
this checkout is currently cleared to push.

### Review

The other half of the Inspector. A scan asks whether the repository is sound; a review asks whether
the **change in front of you** is sound before it lands.

Pick what to review:

| Scope | What it covers |
|---|---|
| **Working** | Everything uncommitted — including files you have just created, which `git diff` never mentions |
| **Staged** | Only what is staged, read from the index rather than from a file that may have moved on |
| **Branch** | This branch against its base, using the merge base — the pull request you would open |

Two layers run, in this order:

**Mechanical, and deterministic.** It reads only the lines the change *introduced*, so a file you
merely touched is never blamed for what was already in it:

- **Syntax** — every changed file is re-parsed. Python and JSON/YAML/TOML through their own parsers,
  and JavaScript, TypeScript, Go, Rust, Java, Kotlin, Ruby, PHP, C and C++ through their grammars. A
  language with no bundled grammar gets *no opinion* rather than a false all-clear.
- **Left behind** — merge conflict markers, `console.log`, `debugger`, `pdb.set_trace`, `dbg!`,
  focused or skipped tests, new TODO/FIXME markers.
- **Deceptive characters** — bidirectional overrides and zero-width characters, the trick that makes
  source read one way to you and compile another.
- **Introduced risk** — the repository audit's own credential and insecure-code rules, applied to the
  added lines and reported at the line they landed on.
- **Gaps** — source changed with no test touched, a test that removes more assertions than it adds, a
  manifest changed without its lockfile, a schema changed without a migration, a removed public
  definition.
- **Shape** — change size, files touched across many areas, very long lines, binaries added.

**Reviewers.** Four read-only agents then read the diff *and the code around it* — correctness,
gaps, impact and compatibility, security — and a synthesis step writes the review: what the change
does and why, blockers, findings by severity, what is missing, and what it could not determine. They
are told to triage the mechanical findings rather than repeat them.

The review ends in a merge verdict — **ready to merge**, **needs a look**, or **do not merge** —
decided the same deterministic way as the scan's. A change that stops a file parsing, leaves a
conflict marker, or adds a credential is blocked outright: unlike a pre-existing problem, it was
introduced here, by someone who is still looking at it.

Click a file to read its patch inline, or open it in CODE.

Two things worth knowing about what it deliberately does not do. It does not re-run your lint, type
or test commands — SCAN does that across the whole repository, and duplicating it per change would
only tell you the same thing more slowly. And a finding in a test or fixture path is kept but
demoted and marked low confidence, because a security test has to contain the very pattern it
asserts is caught.

### Live app

Detects runnable commands from `package.json`, `pyproject.toml`, and `compose.yaml`, starts one
through the normal approval flow, and embeds it. While it runs, its URL prefills the scan's live
target, so "see it working" and "check what it exposes" are the same two clicks.

Probing is limited to loopback and private-network addresses. Anything else has to be confirmed as
yours before the button will run, and the confirmation is audited.

## The visual HTML editor

Opening a page — importing it, double-clicking its canvas card, or pressing ⛶ — gives it the whole
workspace. A mode switch offers **Preview / Edit / Split / Code** (rendered page, visual editor,
stacked preview-over-source, source alone), a **viewport menu** (Responsive, Desktop 1440, Laptop
1280, Tablet 834, Mobile 390), and a **zoom bar** (slider, − / +, click the % for **Fit**, **1:1**
for actual size; `⌘`/`Ctrl`+scroll zooms). **↗** opens the page in a real tab, **⟳** reloads the
frame, `Esc` returns to the canvas, and the blocks rail collapses (`‹`) when the page needs width.

### Editing by hand

**Edit** mode turns the preview into the editing surface:

- **Click** anything to select it; a breadcrumb walks up to its ancestors, and the cursor changes on
  hover and selection.
- **Drag** an element to move it freely — dropping it anywhere changes its position, not its style.
- **Double-click** text to edit it in place.
- **Right-click** an element for a context menu (edit, duplicate, wrap, copy, delete).
- **Drag a block** from the left rail, or click one to drop it after the selection. The palette
  covers layout (sections, grids, flex rows), text, media, form controls, and whole blocks (hero,
  nav, card, feature grid, CTA, footer, table).
- The **right panel** edits the selected element's text, link target, image source and alt, classes,
  alignment, colour, size, padding, and margin, and can duplicate, reorder, or delete it.

Visual edits save on their own a moment after you make them; typed source keeps an explicit
**Apply** button, where a half-finished tag should not hit disk. While you edit, the page's own
scripts are parked so a self-rewriting page cannot fight the editor (restored verbatim on save), and
the frame stays sandboxed without `allow-same-origin`, so it can never reach the app hosting it —
the two sides exchange `postMessage` only.

### Intelligent editing with D[Ai]NO

The editor is agent-aware. A ✨ **D[Ai]NO** section on the element panel and page toolbar turns
design intent into scoped agent instructions:

| Scope | Action | What the agent does |
|---|---|---|
| Element | **Match style** | Restyle the element to match the page — reusing its colours, typography, spacing, and similar components — changing only classes/inline styles, never content |
| Element | **Improve** | Refine layout, spacing, hierarchy, and interactive states while keeping purpose and content |
| Element | **Make responsive** | Make the element read well on mobile/tablet/desktop, consistent with the page |
| Element | **Fill content** | Replace placeholder text and image slots with real, project-specific copy — drawn from the repository — keeping the layout |
| Page | **Polish** | Improve spacing, alignment, hierarchy, typography, and consistency across the whole page |
| Page | **Make responsive** | Add the responsive CSS/classes the page needs, preserving its desktop look |
| Page | **Improve accessibility** | Semantic HTML, alt text, labels, contrast, focus states, and ARIA |
| Page | **Generate from prompt** | Add a new section you describe, matched to the page's existing style and framework |

**Auto-match on drop** — when enabled (a toggle on the blocks rail), dropping a block asks D[Ai]NO to
restyle it to the page automatically, so a pasted-in card adopts the surrounding design without a
second step.

While D[Ai]NO edits the page, the canvas, HTML, and components **blur behind a "working on this
page…" overlay** until the change lands, then the preview **refreshes automatically** — no manual
reload. If you have unsaved edits of your own when D[Ai]NO writes, nothing is discarded: a banner
offers **Load D[Ai]NO's version** or **Keep mine**. A page open in the viewer is attached to the
agent's context automatically, so "make the hero headline bigger" already knows the file.

You can also just ask in chat — the open page is context, and the preview updates the moment the
change lands.

### Export and implement

The **Export** menus (viewer for one page, toolbar for the whole canvas) offer a **prototype bundle
`.zip`** (`index.html`, other pages under `pages/`, assets, `design.json`, `README.md`), a
**standalone `.html`**, a **vector `.svg`**, and a **design `.json`** (drop it on another canvas to
restore it). An artifact can also be downloaded under its original name or written back with **Save
to project…** so the agent can pick it up. Design stays separate from production code: use **Implement
Design**, which asks D[Ai]NO to inspect the repository and propose a plan first — a design change is
never silently turned into code.

## Reviewing changes

Source Control lists staged, modified, and untracked paths with stage, unstage, and discard actions.
Selecting one opens a **side-by-side Monaco diff beside your code** — working tree against the index,
or index against HEAD for a staged change. The GUI never commits or pushes; the agent's workflow owns
that.

## Approvals

The GUI reuses the TUI's exact command permission model. A gated command shows an inline card:

```
Approval needed — the command writes outside the workspace
rm -rf build/
[Allow Once]  [Always Allow]  [Reject]
```

Never-approvable commands are refused, and nothing is committed automatically. Approval prompts,
model pickers, and confirmations use D[Ai]NO's own in-app dialogs, not the browser's.

## Choosing a model

The model picker sits above the conversation; picking a profile is a *session* choice that pins the
model for this conversation (the terminal client sees the same pin) without changing saved routing.
The gear opens **Providers** in the same column:

- Every configured provider with type, model, and an *in use* badge; **+ New** adds one (name, type,
  base URL, model, key, and scope — this project or every project).
- The **model field** is filled from the provider's real catalog: a hosted catalog is *searchable*
  and accepts a hand-entered id; Ollama/vLLM show the handful actually installed, marking a saved but
  missing model `(not installed)`.
- **Test connection** runs four checks without saving — Endpoint (something answered, how fast),
  Credentials (the key was accepted), Model (this model is offered), Generation (a real one-token
  request came back, the only check that proves a turn works).
- Keys are never rendered back: blank keeps the stored one; a literal key goes to the secret store
  and only its `env://` / `file://` / `keyring://` reference goes into YAML.

Saving an OpenRouter provider validates the key and requires the model in the live catalog;
self-hosted providers save even while down.

## Agent settings

The same gear opens **Agent settings** — the browser's half of the terminal customization commands.

| Section | Command | Changes |
|---|---|---|
| Autonomy & effort | `/mode`, `/effort`, `/verbose` | How much this conversation may do, reasoning effort, and how much of a turn is reported |
| Agent roles | routing | Which model plans, builds, reviews, debugs, tests, deploys — see [model routing](model-routing.md) |
| Instructions | `DAINO.md` | Always-on guidance in precedence layers; shows what actually applies to the open file |
| Memory | `/memory` | Add an authoritative `user` fact, search, re-verify, forget, or clear project memory |
| Playbooks | `/playbooks` | Built-in and `.daino/playbooks/` procedures with stages, tools, and approval points |
| Providers | `/provider` | Connect, edit, and test a provider |

## Slash commands

The GUI chat carries the full [TUI command catalog](tui.md#slash-commands); type `/` to open the
menu. Commands that map to a browser action open the matching view:

| Command | Opens / does |
|---|---|
| `/help` | Documentation at `/docs` |
| `/clear` | Clear the visible conversation |
| `/cancel` | Stop the running turn |
| `/mode`, `/effort`, `/verbose`, `/model`, `/memory`, `/runtime`, `/settings` | Agent settings panel |
| `/provider`, `/globalprovider` | Provider setup |
| `/files` | Code workspace ▸ Explorer |
| `/diff` | Code workspace ▸ Source Control |
| `/logs`, `/map` | Insights ▸ logs / execution map |
| `/qa`, `/inspect` | Inspector ▸ Scan |
| `/workspace` | Workspaces |
| `/missions`, `/checkpoints` | Insights ▸ the matching view |

Everything else — `/ask`, `/plan`, `/build`, `/run`, `/team`, `/review`, `/test`, `/index`,
`/playbooks`, `/deploy`, `/status`, `/tasks`, `/resume`, `/checkpoint`, `/restore`, `/new` — is sent
to the agent exactly as in the TUI.

## Keyboard shortcuts

Every binding is also printed next to its menu item (Help ▸ Keyboard shortcuts lists them all).

| Action | Binding | Action | Binding |
|---|---|---|---|
| Save / save all | `⌘S` / `⇧⌘S` | Go to line / symbol | `⌘G` / `⇧⌘O` |
| Open file by path | `⌘O` | Cycle autonomy mode | `⇧⇥` |
| Close editor | `⌥W` | Next model profile | `⌘M` |
| Next / previous editor | `⌥⌘→` / `⌥⌘←` | Toggle sidebar / panel / agent | `⌘B` / `⌘J` / `⌘I` |
| Find / replace | `⌘F` / `⌥⌘F` | Interface zoom in / out / reset | `⌘=` / `⌘-` / `⌘0` |
| Find in files | `⇧⌘F` | New terminal | `Ctrl+\`` |

`Ctrl` replaces `⌘` on Linux and Windows. `⌘W` and `⌘N` belong to the browser, which is why closing
an editor is `⌥W`.

## Settings and preferences

Settings keeps two kinds of state apart. **Interface preferences** stay in the browser and never
touch your project — theme (Dark / Light / High contrast), interface font size, editor behaviour
(word wrap, minimap, line numbers, auto-save, close-modified prompt), conversation attachments and
reasoning display, and a verbose event stream. **Project and agent configuration** is written to
`.daino/config.yaml` through the same validated services as the TUI and CLI, effective immediately —
provider, model routing, runtime (`docker` / `local` / `ssh`, with `local` flagged as unsandboxed),
approvals, log level, notifications, and keep-awake.

## Sessions

The TUI and GUI share one session system: work started with `--tui` reopens with `--gui` and vice
versa. **A session is the unit of context**, so each turn sends its recent exchanges as history — a
days-long conversation makes every prompt bigger and answers in the shadow of old ones (felt most on
local models). The panel header names and switches conversations; **New** starts a fresh one with an
empty plan, default autonomy, and no carried-over task state. Each row shows its message count, which
is prompt weight.

## Notifications and staying awake

Both clients raise a real OS notification for the three moments that arrive when nobody is watching —
a turn finished, something failed, an approval is waiting — through one shared service (macOS
`terminal-notifier`/`osascript`, Linux `notify-send`, Windows balloon), plus the terminal bell.
While the agent works, D[Ai]NO stops the machine sleeping (`caffeinate -dimsu` / `systemd-inhibit` /
`SetThreadExecutionState`, refcounted and dropped on shutdown), and the browser additionally holds a
**Screen Wake Lock** while the IDE is the visible tab. A turn ending in a background tab marks its
title `✓` or `✗`. One *Keep awake while working* switch governs it; `DAINO_NOTIFY=off` and
`DAINO_WAKELOCK=off` disable each for a process.

## Reloading during a turn

**A turn is server-side work and survives the page.** Only an explicit **Stop** cancels it. A
reconnecting client is told on connect that work is in flight, so it picks the running state back up
(the runner resumes, the transcript reloads) and can stop the turn a previous connection started. Two
consequences: an approval nobody is there to answer is **denied** rather than left holding the
one-turn-at-a-time lock open, and the live file list starts empty after a reload while the closing
changeset still reports everything (it is built server-side).

## Security

- Default bind is `127.0.0.1`; file, shell, and terminal APIs are never exposed publicly by default.
- **Only this server's own page may call it.** A browser `Origin` is accepted only when it is this
  instance (or the Vite dev server), and `Host` must name a served interface — closing cross-origin
  WebSocket and DNS-rebinding attacks that "it only listens on localhost" does not. Non-browser
  clients send no `Origin` and are unaffected. Set `DAINO_GUI_ALLOWED_HOSTS=name1,name2` for a
  deliberate alternate hostname.
- **One agent turn at a time** — a second tab, or the same project in the terminal client, waits
  rather than interleaving edits against one working tree.
- **Terminals are reaped** — a shell unattached for ten minutes is closed, and a project is capped at
  24.
- File writes use optimistic concurrency (content hash) and reject stale writes with a conflict
  warning. Inspections started from the browser skip network-approval checks rather than silently
  granting them, and the live probe refuses any target outside loopback or private address space
  until you confirm you own it — a confirmation that is written to the audit log. Everything
  Insights shows is built from redacted, structured audit records.

## Documentation and API reference

The **?** button (and `/help`) opens `/docs` — these pages, rendered by the IDE with a sidebar,
search, and an on-page outline from the markdown that ships with the package. The generated OpenAPI
reference lives at `/api-docs` (ReDoc at `/api-redoc`).

For frontend development, run the Vite dev server, which proxies the backend:

```bash
cd daino/gui
npm run dev        # http://127.0.0.1:5173, proxying the backend on 4173
```
