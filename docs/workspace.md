# Workspace

The WORKSPACE tab is for the work that is not code: documents, research, planning, analysis. It
sits beside CODE (software), DESIGN (UI and diagrams), and INSPECTOR (pre-push QA).

A **workspace** is a named body of work — a goal, the files it needs, the documents it produces, a
plan, and the sources behind it — that continues across as many conversations as it takes. Until
now Daino had no such container: a project is a directory, a mission is one request bound to a git
worktree, and a session is one chat.

## The one thing to understand first

**A workspace is a real folder on disk, inside `.daino/`.**

```
your-repo/
├─ src/
├─ docs/
└─ .daino/                                        ← Daino's state directory
   └─ workspaces/
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

Everything follows from that. The documents are ordinary files, so the agent needs no special tools
to write them — `read_file`, `write`, `replace`, `glob`, and `grep` already work, and Daino exempts
the workspaces subtree from the filter that otherwise hides `.daino` from every search. Open one in
CODE with **↗** like any other file. And the database row is only an index: delete it and the work
survives.

They sit inside the state directory rather than at the top of your repository because a documents
folder in the working tree turns up in every `git status`, every file tree, and every package build.
`.daino/` is already in `.gitignore` (`daino init` puts it there), so workspace documents are not
committed by default and never appear in the diff you are about to review.

If you *do* want this work versioned with the project, un-ignore just that subtree:

```gitignore
.daino/
!.daino/workspaces/
```

A workspace remembers its own folder, so anything created before this default moved keeps working
where it is, and passing an explicit `folder` when creating one still puts it wherever you say —
including in the working tree, if that is what you want.

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

Every save is recorded as a version, with who saved it, and any kept version can be restored — the
current text becomes a new version, so a restore is undoable too.

History is a safety net rather than an archive: the newest 50 versions of a document are kept, and
older ones are trimmed. Versions a change set still points at are exempt from that trim, so
reviewing or rejecting an old run's work never finds its "before" missing.

Saving is checked against the version you opened. If the agent (or another window) has rewritten the
document since, the save is refused and the editor offers **Reload from disk** or **Keep mine** —
your draft is never silently replaced, and neither is theirs.

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

A step may declare that it depends on other steps. Ordinary plan order covers almost everything, so
this is for the plans where order alone is not the truth; there is no workflow engine behind it, and
the executor simply will not start a step whose predecessors are unfinished.

## Running the plan

**Run Plan** makes the plan execute. Daino works through the pending steps in order, and for each
one it runs a single ordinary agent turn — the same tools, the same prompt, the same approvals, the
same revision history as if you had asked for that step yourself.

```
Run #12   Running   3 / 7 steps        [Pause] [Stop]

✓ Analyse the uploaded requirements
✓ Extract the technical constraints
◉ Research architecture options
○ Create the comparison
○ Draft the proposal
```

One task, one turn, is the whole design. The alternative — a single prompt asking a model to carry
out seven steps — produces a plausible narrative of work rather than the work, and gives you no
point at which to intervene. Because a step is an ordinary turn, everything the rest of Daino
already does keeps working during a run: sources are recorded, revisions are captured, changes are
grouped, and commands still ask.

### Pause, Resume, Stop

**Pause** stops after the current step rather than in the middle of one — a half-written document
and a step marked "in progress" that nobody owns is not a state worth being able to reach. **Resume**
picks up from wherever the plan is. **Stop** cancels the run and keeps everything: the plan, the
finished steps, and every document produced. Nothing is unwound, and you never restart from the
beginning.

A run survives a restart. If Daino stops while a run is going, the run comes back as **Paused** with
"Interrupted" as its reason — because a row that says "running" with no process behind it is a lie
the UI would render as live work.

### Steering it while it works

Type into the chat while a run is going and it becomes direction for that run rather than a second
turn:

> Also compare their enterprise pricing and deployment options.

Daino folds this into the plan at the next step boundary — adding, rewording or reordering what is
still ahead — and tells you it did. Completed steps are never rescheduled and finished work is never
discarded. The current step finishes first: interrupting a turn mid-way to re-plan would abandon
whatever it was writing.

### When a step fails

The run holds at **Needs you** with the reason attached, and offers **Retry** and **Skip**. It does
not carry on. Skipping a failed research step and writing the recommendation anyway is exactly the
confident-but-baseless output Daino refuses everywhere else — and a skipped step is recorded as not
done, so the progress count keeps telling the truth. Three failures in a row end the run rather than
asking a fourth time.

### Approvals

A run is unattended, so "the user is right there and will notice" stops being true. Actions are
classified rather than counted:

| Level | Examples | While a run is executing |
|---|---|---|
| Read | Reading files, searching, fetching a page | Allowed |
| Workspace write | Creating and editing documents in the workspace folder | Allowed |
| Local execution | Running a command on your machine | Asks |
| External action | Writing outside the workspace folder | Asks |
| Destructive | Deleting or moving a file | Asks |

When something needs you, the run parks at **Needs approval** and waits — no timeout, no default
yes. Approving a run's command never remembers the answer: a remembered yes inside an unattended
loop is how one approval becomes twenty. The command gate that governs shell commands everywhere
else still applies on top of this and still has the final word.

Asking about everything would be worse than asking about nothing. Someone who has clicked Allow
eleven times for a file write clicks the twelfth without reading it, which is precisely when the
destructive one arrives.

### What a run leaves behind

An **Activity** timeline, written as the run goes and kept afterwards, so it reads the same tomorrow
as it does now — sentences about what happened, not raw tool calls. And a completion summary naming
what was created and how many sources were recorded. The deliverables themselves stay in the
workspace; the chat does not fill up with the documents' contents.

## Skills

A template shapes a *new workspace*. A skill shapes *one piece of work*: how a competent person
approaches a competitive analysis, what a PRD has to contain, which checks an incident review is not
finished without.

Daino ships seven — Competitive Research, PRD Writer, Data Analysis, Technical Proposal, Incident
Review, Architecture Review, Executive Presentation — and picks one from the goal when you start a
run, showing which it chose. Pick a different one, or none, from the run header. Projects add their
own or override a built-in by name from `.daino/workbench-skills/*.yaml`, the same two-directory
pattern templates and playbooks use.

A skill is instructions, preferred tools, what finished work looks like, and a checklist to hold
against it. Nothing about the tool surface changes; a skill changes how the work is approached, not
what is possible.

## Reviewing what changed

The history is per file. **Change sets** are per act: everything one step of the work touched,
grouped, so a task that rewrote the proposal, extended the comparison and added four sources is one
thing to review rather than seven.

```
Daino changed 3 documents

proposal.md      changed   [✓] [↺]
comparison.md    changed   [✓] [↺]
research.md      new       [✓] [↺]

[Accept all] [Reject all]
```

Open one to see the before and after, line by line. Accept keeps it. Reject restores the version the
document had before that change — through the same history the Documents tab has always shown, and
itself recorded as a new version, so rejecting is as undoable as the change it undid. A document the
change *created* is removed on reject, and its history survives even then.

The change set is an index and nothing more. Delete every change-set row and no version is lost.

## Working with CODE and DESIGN

Workspace understands the goal; CODE builds software; DESIGN is where visual work is edited. A
workspace can start work in the other two without duplicating either.

**Diagrams.** When the shape of something matters more than the prose describing it, the agent
creates a real diagram on the same canvas the DESIGN tab edits. It appears in the workspace as a
linked item — *Architecture diagram · Created in DESIGN* — with a button to open it there.

**Code.** Ask for a prototype and the agent prepares a brief in the workspace: what to build, and
which documents define it, by reference rather than by pasting their contents. **Start in CODE**
opens it as a coding session with that brief. It is deliberately a handoff rather than an
immediately running agent — two agents editing one working tree is not a feature, and a person
seeing what was asked for before it is built is worth the extra click.

## Provenance and stale documents

When the agent writes one document from another, it records the relationship. Later, if the source
changes, the derived document is flagged:

> `proposal.md` may be outdated because `architecture.md` changed.  [Review] [Update] [Ignore]

**Review** opens it. **Update** asks Daino to bring it in line with its source. **Ignore** dismisses
the warning durably — a warning that returns after being dismissed teaches people to ignore
warnings.

Nothing is rewritten automatically. An agent silently regenerating a document you have been editing
is a worse failure than a stale one, and only you know which.

## Finished files

Markdown is the source of truth; **Word**, **Sheet**, **Deck** and **PDF** render it into something
to send. Structure survives the crossing: a heading becomes a heading, a table becomes a table with
typed numbers and a frozen header row, a section becomes a slide with the bullets nested as written.

DOCX, XLSX and PPTX need the same optional parsers the uploads use (`pip install 'daino[documents]'`);
PDF is written by Daino directly and always works. A rendering is regenerated from the document
rather than edited, and each regeneration keeps the previous one in history.

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
- **Six extra tools** — `workspace_read`, `workspace_plan`, `workspace_task`, `workspace_link`,
  `workspace_deliverable`, `workspace_code`. Still none of them a file tool: documents are real
  files and `write`/`replace` already write them. Each covers something a file cannot say — what the
  workspace holds, where the work is up to, where a document came from, what a rendering of it
  should be, and what should be built elsewhere. The design tools are here too, so an architecture
  section can be a diagram instead of three paragraphs describing one.
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
