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

## Menu bar

The window chrome is two rows: an application menu over the workspace tabs.

| Menu | What it holds |
| --- | --- |
| **File** | New file/folder, open by path, save, save all, revert, close editor(s), copy the active path, re-read configuration from disk |
| **Edit** | Undo/redo, find, replace, find in files, toggle comment, format, fold/unfold, select all |
| **Go** | Go to file, line, or symbol; next/previous editor; Explorer, Search, Source Control; execution map; QA report |
| **View** | Switch workspace, toggle the sidebar/panel/agent, choose the panel view, interface zoom |
| **Run** | Start/stop the preview, run or cancel a QA scan, index the repository, stop the running agent turn |
| **Terminal** | New terminal, clear, switch shell, kill one or all, show the panel |
| **Settings** | Everything below |
| **Help** | Documentation, API reference, keyboard shortcuts, about |

Menu items are greyed out when they cannot act — Save with nothing modified,
Kill terminal with no shell open — rather than failing after the click.

### Settings

Settings covers two different kinds of state, and the menu keeps them apart.

**Interface preferences** stay in the browser and never touch your project:

- **Theme** — Dark (default), Light, or High contrast. The palette, the Monaco
  editor, and the terminal all switch together.
- **Interface font size** — one control moves the whole interface: labels, trees,
  tabs, badges, and the status bar are all expressed as offsets from it. The
  editor and terminal keep their own sizes.
- **Editor behaviour** — word wrap, minimap, line numbers, whitespace, sticky
  scroll, tab size, auto-save, and whether closing a modified file asks first.
- **Conversation** — whether the open file, selection, and diff are attached to
  each message, and whether reasoning is shown while a turn runs.
- **Verbose event stream** — widens the Output panel from the run summary to
  every event the backend published, for when the question is "why did nothing
  happen?".

**Project and agent configuration** is written to `.daino/config.yaml` through
the same validated services the TUI and CLI use, and takes effect immediately:

- **Provider** — open provider setup in the agent panel (below), point every
  agent role at one configured provider, or check each provider's health with a
  live request.
- **Model routing** — choose the model profile for each role individually:
  architect, planner, builder, reviewer, debugger, tester, summarizer, deployer.
  See [model routing](model-routing.md).
- **Runtime** — `docker` (sandboxed), `local`, or `ssh`, and whether commands may
  reach the network. The runtime also has a quick switch in the menu bar beside
  the project name, where `local` is coloured as a caution because it runs the
  agent's commands unsandboxed on your machine.
- **Approvals** — whether installs, network access, and production actions still
  require approval, and whether review is required before commit.
- **Log level** — applied to the running server, not just the next start.
- **Notifications** — whether to raise an OS notification when a turn completes,
  when something fails, and when an approval is needed, plus whether to use a
  desktop notification, the terminal bell, or both.
- **Keep awake while working** — see below.

### Choosing and connecting a model

The model picker sits in the **agent panel**, above the conversation it governs,
with a gear beside it. Picking a profile is a *session* choice — it pins the
model for this conversation (the terminal client sees the same pin) without
changing the saved routing.

The gear opens **Providers** in that same column:

- Every configured provider, with the type, model, and an *in use* badge when
  agent roles route to it. Selecting one edits it.
- **+ New** adds one: name, type (OpenRouter, Ollama, vLLM, or any
  OpenAI-compatible gateway), base URL, model, key, and scope — this project or
  every project.
- The **model field** is filled from the provider's real catalog, fetched as soon
  as the base URL is set. A hosted catalog is hundreds of ids long, so it is
  *searchable* — type to filter, and an id the catalog omits can still be entered
  by hand. Ollama and vLLM serve a handful of models, so those are a plain list
  of what is actually installed; a saved model that is no longer there is marked
  `(not installed)` rather than silently accepted.
- **Test connection** runs four checks and reports each one, without saving
  anything:

  | Check | What it proves |
  | --- | --- |
  | Endpoint | something answered at this URL, and how quickly |
  | Credentials | the key was accepted (OpenRouter validates directly; elsewhere the generation check covers it) |
  | Model | *this* model is among the ones the provider actually offers |
  | Generation | a real one-token request came back — the only check that proves a turn would work |

  A single verdict would hide the interesting case: a running Ollama whose
  configured model was never pulled answers the endpoint check in milliseconds
  and fails everything that matters.
- Keys are never rendered back into the form: blank means "keep the stored one".
  A literal key is written to the project or global secret store and only its
  reference (`env://`, `file://`, `keyring://`) goes into YAML.

Saving an OpenRouter provider validates the key and requires the model to exist
in the live catalog. Self-hosted providers save even while down — a local Ollama
you have not started yet is still worth configuring — and the health result is
reported rather than swallowed.

### The composer

Above the message box sit the two controls the terminal client cycles from the
keyboard, bound to the same keys here and clickable as well:

| Control | Key | What it does |
| --- | --- | --- |
| Autonomy | `⇧⇥` | Plan → Ask → Session → Full, in the TUI's order. The dot is coloured per mode, and `full` is outlined in red because it is not interchangeable with `plan`. |
| Model | `⌘M` | Steps to the next configured profile and pins it on the session |

**Attachments.** Drop files on the composer, paste them (a pasted screenshot is
attached rather than pasted as text), or use the paperclip. Each one is stored
under `.daino/attachments/` and attached to the message as a *path* — the agent
opens files with its own tools, so a path is something it can act on, and the
attachment never turns up as an untracked file in the diff you are about to
review. Names are sanitised, a repeated name is kept rather than overwritten, and
the cap is 8 MB and 10 files per message.

Images are stored the same way, but D[Ai]NO's provider layer sends text messages
(`Message.content` is a string), so **no configured model can look at a picture
yet** — the path is still useful for "optimise the screenshot at …" or "move it
into assets/". Vision would need image parts through the schema, the wire format,
and a per-profile capability flag.

The terminal client's equivalent is the `@file:` reference in its prompt, which
completes against the repository index.

### Tasks and live changes

While a turn runs, three things track it, so a long run is never a blank wait:

- The **runner's label** names the current step and the file being written.
- A collapsible **Tasks** panel under the model picker holds the agent's
  checklist with a `done/total` count, the in-progress item marked, and the
  current item shown even when collapsed. It appears only while a turn is
  running: a checklist describes work in progress, and last turn's plan left on
  screen reads as unfinished work nobody is doing.
- A live **Editing N files** card at the end of the stream accumulates every file
  the turn has touched with running `+`/`-` counts, the file being written
  highlighted last. It is the same card as the closing changeset, still moving.

Each checklist item that finishes adds one line to the transcript — `✓ Remove the
GitHub Pages section` — instead of reprinting the whole plan on every update,
which read as five plans rather than one making progress. A failed item is shown
in red. The terminal client does the same in its side panel and transcript.

### The changeset

When a turn ends having edited anything, both clients close it with one summary
of the whole changeset rather than leaving you to scroll back through the diffs
that streamed past:

```
Edited 6 files  +49 -81
  README.md                          +15 -15
  docs/assets/relevance-heatmap.svg   +2 -2
```

In the browser it is a card: the filename opens the file, **diff** opens that
file's diff beside the code, **Review** opens all of them, and files beyond the
first three collapse behind *Show N more*. A file edited twice in one turn is one
row with the totals summed, biggest change first, and a turn whose verification
failed is marked `unverified`.

### The runner

A dinosaur runs under the agent panel's header while D[Ai]NO is working — the
browser counterpart of the terminal client's own runner
(`daino/tui/widgets/checklist.py`), driven by the same activity states and the
same event mapping, so both clients agree about what is happening:

| It shows | When |
| --- | --- |
| Running, jumping a cactus, with `THINKING` / `PLANNING` / `INSPECTING` / `BUILDING` / `VERIFYING` and the current step | A turn is in flight — the state follows the agent role and the tool being used |
| Standing still, green, `TASK COMPLETED` | The turn finished |
| Red, with the cactus stopped against it, `ERROR` and the reason | A tool failed, tests failed, or the mission failed |
| Standing still, `READY` | Idle |

It is not decoration: a long turn is otherwise indistinguishable from a stalled
one. The animation stops entirely when idle rather than spinning in the
background, and it does not animate at all under
`prefers-reduced-motion: reduce`.

### Notifications and staying awake

A turn runs for minutes, so the interesting moments arrive when nobody is
looking at the window. Both clients therefore raise a **real OS notification**
for the three that matter — a turn finished, something failed, an approval is
waiting — through one shared service
(`daino/notifications.py`), so the terminal client and the browser behave
identically. macOS uses `terminal-notifier` when installed and `osascript`
otherwise, Linux uses `notify-send`, Windows a balloon tip; a host with no
notifier still runs the turn. The terminal bell is sent as well, which most
terminals turn into a tab badge. `DAINO_NOTIFY=off` silences everything for one
process.

While the agent is working, D[Ai]NO also **stops the machine sleeping**
(`daino/keepawake.py`): a host that suspends mid-turn drops the model connection
and freezes commands in flight. It holds `caffeinate -dimsu` on macOS,
`systemd-inhibit` on Linux, and `SetThreadExecutionState` on Windows — refcounted,
so an overlapping chat turn and QA scan release it only when the last finishes,
and dropped on shutdown so quitting mid-turn never leaves sleep inhibited. The
browser additionally holds a **Screen Wake Lock** while the IDE is the visible
tab, so the display itself does not dim; the two are governed by the one
*Keep awake while working* switch. `DAINO_WAKELOCK=off` disables it entirely.

If a turn ends while the IDE is in a background tab, its title is also marked
`✓` or `✗` until you look at it again — the quiet signal that is still there ten
minutes later.

### Agent settings

The same gear opens **Agent settings** — the browser's half of the terminal
client's customization commands. Each section is one section of the column, and
each one drives the service the slash command drives:

| Section | Slash command | What it changes |
| --- | --- | --- |
| Autonomy & effort | `/mode`, `/effort`, `/verbose` | How much this conversation may do on its own, the model's reasoning effort, and how much of a running turn is reported |
| Agent roles | routing | Which model plans, builds, reviews, debugs, tests, deploys |
| Instructions | `DAINO.md` | Always-on guidance, in precedence layers |
| Memory | `/memory` | Facts, decisions, and failures worth keeping |
| Playbooks | `/playbooks` | Reusable staged procedures |
| Providers | `/provider` | Connect, edit, and test a provider |

**Autonomy** is a session policy, not a project setting: Plan (read-only
planning), Ask (routine work allowed, risky commands ask first), Session
(approval-gated commands allowed for this session), or Full. A conversation
carries its mode into the terminal client and back.

**Instructions** lists every `DAINO.md` the resolver can pick up — the user-level
file, the repository file, and any scoped file in a subdirectory — with what is
missing offered for creation. Because closer layers override broader ones, the
section also shows *what actually applies* to the file you have open, resolved by
the same code the agent uses: a `style:` rule in `src/DAINO.md` replaces the
repository-wide one rather than both being sent. The user-level file lives outside
the repository, so it is edited here; repository files open in the editor.

**Memory** adds a fact you state yourself as an authoritative `user` memory — the
one class the agent's own extraction cannot grant itself — and lets you search,
filter by type, re-verify an item against its current source, forget one, or clear
the project's memory.

**Playbooks** shows the built-in procedures and any your project adds under
`.daino/playbooks/`, each with its stages, allowed tools, and approval points.

There are no cards here for skills, hooks, plugins, or MCP servers: D[Ai]NO
implements none of them, and an inert card promising one is worse than its
absence.

### Keyboard shortcuts

Every binding below is also printed next to its menu item (Help ▸ Keyboard
shortcuts lists them all).

| Action | Binding |
| --- | --- |
| Save / save all | `⌘S` / `⇧⌘S` |
| Open file by path | `⌘O` |
| Close editor | `⌥W` |
| Next / previous editor | `⌥⌘→` / `⌥⌘←` |
| Find / replace (in the editor) | `⌘F` / `⌥⌘F` |
| Find in files | `⇧⌘F` |
| Go to line / symbol | `⌘G` / `⇧⌘O` |
| Cycle autonomy mode | `⇧⇥` |
| Next model profile | `⌘M` |
| Toggle sidebar / panel / agent | `⌘B` / `⌘J` / `⌘I` |
| Interface zoom in / out / reset | `⌘=` / `⌘-` / `⌘0` |
| New terminal | `Ctrl+\`` |

`Ctrl` replaces `⌘` on Linux and Windows. `⌘W` and `⌘N` belong to the browser
and cannot be intercepted, which is why closing an editor is `⌥W`.

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
approvals, and completion — never raw event JSON. Under its header a **runner**
reports what the agent is doing, and below that sit the model picker and the gear
that opens [provider setup](#choosing-and-connecting-a-model). It collapses to a labelled
rail (`⌘I` / `Ctrl+I`) when the canvas or editor needs the width; `⌘B` toggles
the sidebar and `⌘J` the bottom panel. The **?** button in the menu bar opens
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

### Reloading during a turn

**A turn is server-side work and survives the page.** Closing or refreshing the
tab used to cancel it: the socket's disconnect handler cancelled the task, and
because `CancelledError` is not an `Exception` nothing reported it — the event log
stopped mid-action and the mission was left orphaned at status `created`.

Now only an explicit **Stop** cancels a turn. A reconnecting client is told on
connect that work is in flight, so it picks the running state back up (the runner
resumes, the transcript reloads), and it can stop the turn that a previous
connection started. Two consequences worth knowing:

- An approval that nobody is there to answer is **denied** rather than left
  pending, because an unanswerable approval would hold the turn — and the
  project's one-turn-at-a-time lock — open for the life of the process.
- The live file list starts empty after a reload; the closing changeset still
  reports everything the turn edited, because that is built server-side.

## Sessions

The TUI and GUI share one session system. Work started with `daino .` can be
reopened with `daino . --gui` and vice versa, and switching between workspaces
keeps the same session.

**A session is the unit of context, so starting a new one is a real tool.** Each
turn sends the session's recent exchanges as history: a conversation you have
been in for two days makes every prompt bigger and answers a new request in the
shadow of old ones — which is felt most on a local model, where prompt size is
throughput.

The agent panel's header names the current conversation and switches between
them; **New** starts a fresh one, as does File ▸ New conversation (the terminal
client's `/new`). A session is named after the first thing you ask it, so the
list is legible, and each row shows how many messages it carries — that number is
prompt weight. Switching clears the live view (plan, file list, context chips)
because none of it belongs to the conversation you moved to; the transcript is
loaded per session.

A new session also starts with an empty plan, the default autonomy mode, and no
carried-over task state.

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
- **Only this server's own page may call it.** A request carrying a browser
  `Origin` is accepted only when that origin is this instance (or the Vite dev
  server during frontend development), and the `Host` must name an interface
  this instance was told to serve. That closes two attacks that "it only listens
  on localhost" does not: cross-origin **WebSockets are exempt from CORS**, so
  any page you happen to have open could otherwise drive the agent or type into
  a shell; and **DNS rebinding** points an attacker's hostname at your loopback
  listener. Non-browser clients (curl, editors, scripts) send no `Origin` and are
  unaffected. Set `DAINO_GUI_ALLOWED_HOSTS=name1,name2` when you deliberately
  reach the IDE under another hostname.
- **One agent turn at a time.** A second browser tab — or the same project open
  in the terminal client — waits rather than interleaving tool calls and file
  edits against one working tree.
- **Terminals are reaped.** Every page load opens a shell; one that no client
  has been attached to for ten minutes is closed, and a project is capped at 24,
  so closed tabs cannot leave live PTYs behind.
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

