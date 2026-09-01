# Workspace

The WORKSPACE tab is for the work that is not code: documents, research, planning, analysis. It
sits beside CODE (software), DESIGN (UI and diagrams), and INSPECTOR (pre-push QA).

A **workspace** is a named body of work — a goal, the files it needs, the documents it produces, a
plan, and the sources behind it — that continues across as many conversations as it takes. Until
now Daino had no such container: a project is a directory, a mission is one request bound to a git
worktree, and a session is one chat.

## The one thing to understand first

**A workspace is a real folder in your project.**

```
your-repo/
├─ src/
├─ docs/
└─ workspace/
   └─ q3-pricing-research/
      ├─ uploads/
      │  ├─ competitor-pricing.pdf
      │  └─ .extracted/competitor-pricing.md   ← what the agent actually reads
      ├─ .sources/<digest>.md                  ← cached research pages
      ├─ .history/                             ← previous versions
      ├─ findings.md                           ← a document
      ├─ recommendation.md                     ← a document
      └─ workspace.json                        ← manifest, so the folder is self-describing
```

Everything follows from that. The documents are ordinary files, so they are greppable, indexed,
diffable, openable in CODE, and versioned with your repository. The agent needs no special tools to
write them — `read_file`, `write`, `replace`, and `grep` already work. And the database row is only
an index: delete it and the work survives.

If you would rather not commit them, one line in `.gitignore` covers it. Daino does not add that
line for you, because the point of the choice is that this work lives with the project.

## Starting one

**New**, then a name, a goal, and a work type:

| Template | What it seeds |
|---|---|
| **General** | Nothing assumed — three generic steps |
| **Research** | A `findings.md` outline, and an agent told to cite every claim and separate evidence from inference |
| **Product requirements** | A `requirements.md` outline, and an agent told to write outcomes rather than implementations |
| **Data analysis** | An `analysis.md` outline, and an agent told to report the shape of the data before its conclusions |
| **Meeting notes** | A `notes.md` outline, and an agent told to separate decisions from discussion |
| **Incident review** | A `review.md` outline, and an agent told to write causes as system conditions, never as people's mistakes |

A template contributes three honest things: starter tasks, starter documents, and a preamble
appended to the agent's system prompt. Projects add their own or override a built-in by name from
`.daino/workbench-templates/*.yaml`, the same two-directory pattern playbooks use.

## Uploads

Drop files into UPLOADS. Anything that is not already text is extracted to markdown beside the
original, and it is the extraction the agent reads — Daino's file tools are UTF-8 only, so a PDF
would otherwise arrive as a decode error.

Extraction needs the optional parsers:

```bash
pip install 'daino[documents]'
```

That adds `pypdf`, `python-docx`, `openpyxl`, and `python-pptx`. Without it, markdown, text, CSV,
JSON, YAML, HTML and source code still work; other formats are stored and listed with an explicit
note saying which extra to install.

Extraction is cached by the file's content hash, so re-uploading or reopening never re-parses a
200-page report. A PDF with no text layer is a scan, and there is no OCR here — the upload says so
rather than handing the agent an empty document to summarise confidently.

## Documents

Every file in the workspace folder that is not `uploads/`, `.sources/`, `.history/`, or
`workspace.json` is a document. Open one to read it rendered, EDIT to change it in Monaco, or **↗**
to open it in CODE like any other file.

**Ask** hands the open document to the agent with a request to revise it.

### History

Every saved version is kept, with who saved it, and any of them can be restored — the current text
becomes a new version, so a restore is undoable too.

Versions are recorded from the file-change event rather than from the save button, which means an
edit you make by hand in CODE and an edit the agent makes are captured identically. That is the pair
that matters: the reason this exists is an agent rewriting a document you had been working on.

## The plan

A workspace task list, editable by you and by the agent. Click a step to walk it round
pending → in progress → done, double-click to reword it, and reorder or delete freely.

Unlike the agent's per-turn checklist, these persist across sessions, have stable identities (so two
steps may share their text), keep an explicit order, and are never terminal — a completed step can
be reopened. The agent keeps them current as it works, and restates the whole plan when the shape of
the work changes; statuses survive that by matching text.

## Research and sources

Ask the agent to research something and it uses the same hardened web tools as everywhere else:
DuckDuckGo search plus an SSRF-resistant fetch that revalidates every redirect and rejects
non-text responses.

Every page it fetches is filed in SOURCES automatically — the agent is not asked to remember, because
a bibliography that depends on that is a bibliography with holes in it. The fetched text is cached
on disk, so a claim stays checkable after the page changes or disappears. The agent is instructed to
cite with markdown footnotes pointing at those sources.

For a question with several distinct angles, Daino can fan out: a team of read-only researchers
investigates the sub-questions concurrently, each blind to the others' findings, and a synthesis
step reconciles them. Nothing in that team can edit anything, which is what makes running them at
once safe — there are no overlapping scopes to arbitrate.

## The conversation

Selecting a workspace re-points the agent panel at that workspace's own conversation, so you keep
the whole composer — slash commands, model picker, autonomy, approvals, attachments — and the
workspace decides which thread it is talking to. Leaving the tab restores whatever CODE was on.

A workspace turn differs from a repository turn in three ways:

- **A different system prompt.** Every other prompt in Daino says "you are a coding agent working in
  the user's repository". This one is about documents, uploads, citation, and keeping the plan
  current.
- **Three extra tools** — `workspace_read`, `workspace_plan`, `workspace_task`. Deliberately only
  three, because documents are real files and the existing file tools already write them. What a
  file cannot express is what the workspace holds and where the work is up to.
- **No verification demanded.** A chat turn normally has to finish with runnable checks. A written
  report has no test suite, and inventing one is worse than admitting there is nothing to run.

`workspace_read` summarises: document titles with short previews, never their bodies. The full text
is one `read_file` away, and the path it gives is exactly the one that tool accepts.

### Routing it to a different model

Optional. Reading documents suits a long-context, inexpensive profile, which is rarely the same one
you want writing code:

```yaml
routing:
  researcher: my-long-context-profile
```

Leave it out and workspace turns use the builder profile, exactly as before. Nothing breaks by
omission.

## Security

The same boundaries as everywhere else in Daino.

Every workspace path — from the browser and from the agent — is resolved and checked for containment
before anything touches the disk; a traversing path is refused. An absolute path is normalised into
the workspace rather than rejected, matching how `EditTools` treats agent-supplied paths, and the
guarantee is containment: a write never lands outside the folder.

Uploads are capped at 8 MB and their names sanitised, reusing the attachment path's hardening.
Extraction never executes anything in a document. Deleting a workspace removes the entry and leaves
your files alone unless you ask for both.
