# Extending the agent

Four ways to change what D[Ai]NO does without changing D[Ai]NO. They are listed
in rough order of weight — a slash command is one file and two minutes, an MCP
server is a process with its own dependencies.

| | What it is | Where it lives | Who invokes it |
|---|---|---|---|
| [Slash commands](#slash-commands) | A prompt you reuse | `.daino/commands/*.md` | You, by typing `/name` |
| [Skills](#skills) | Practice the agent loads when it applies | `.daino/skills/<name>/SKILL.md` | The model, when the task matches |
| [Hooks](#hooks) | A command run around what the agent does | `.daino/hooks.yaml` | The agent lifecycle |
| [MCP servers](#mcp-servers) | External tools | `.daino/mcp.json` | The model, as ordinary tools |

Two of these are protected from the agent and two are not, and the difference is
deliberate. Hooks and MCP servers run shell commands and launch processes, so
their files live in `.daino/` — which D[Ai]NO refuses to let the agent write to.
Commands and skills are only ever prompt text, so the agent may write those; an
agent that writes itself a skill has written itself a note, which is useful.

---

## Slash commands

A markdown file becomes a command. `.daino/commands/review-pr.md`:

```markdown
---
description: Review a pull request the way this team reviews them
argument-hint: <pr-number>
---
Review PR $ARGUMENTS against our checklist:

- Does every new endpoint have an integration test?
- Are database migrations reversible?
- Is anything logged that should not be?

Read the diff first, then answer point by point.
```

Then type `/review-pr 481`. The frontmatter is optional — a file containing
nothing but instructions is a perfectly good command.

**Substitution.** `$ARGUMENTS` becomes everything after the command name;
`$1`…`$9` are positional. A template that references neither gets the arguments
appended, because silently dropping what you typed is the worse failure.

**Namespacing.** A file in a subdirectory takes a prefixed name:
`.daino/commands/db/migrate.md` is `/db:migrate`.

**Scope.** `~/.daino/commands/` applies to every project. A project file of the
same name wins. Neither can shadow a built-in command like `/diff` — those are
matched first, so a project cannot quietly change what `/model` does.

Commands are re-read on every turn, so a command you just wrote works now.

---

## Skills

A skill is practice the *model* decides to load. Its name and one-line
description sit in the system prompt; the body arrives only when the model picks
it. That two-step shape is what makes a dozen skills affordable — a dozen full
documents in context would not be.

`.daino/skills/api-conventions/SKILL.md`:

```markdown
---
name: api-conventions
description: Use when adding or changing an HTTP endpoint
---
Endpoints live in `routes/`, one router per resource.

- Request and response models go in `schemas/`, never inline.
- Every endpoint gets an integration test in `tests/api/`.
- Errors raise `AppError`; never return a bare dict with a `error` key.
```

The `description` is required, and it is the only thing the model has to choose
by — write it as *when to use this*, not as what it contains. A skill without one
is reported as a problem rather than loaded silently.

Files sitting beside `SKILL.md` are listed to the agent by path rather than
inlined, so a skill can ship a long checklist or a reference table without that
text costing anything until it is wanted.

> Not to be confused with [workspace skills](workspace.md#skills), which are
> templates for knowledge work — a different feature with an unfortunately
> similar name.

---

## Hooks

A hook is a command D[Ai]NO runs at a point in the session. Auto-format after
every edit, refuse edits to a generated directory, post to Slack when a long
mission finishes, enforce a rule your team has that D[Ai]NO has never heard of.

`.daino/hooks.yaml`:

```yaml
post_tool_use:
  - name: formatter
    matcher: "write|replace|multi_edit"
    command: ruff format . && echo "formatted"

pre_tool_use:
  - name: protect-generated
    matcher: "write|replace|delete|multi_edit"
    command: ./scripts/refuse-generated.sh
    timeout: 5

stop:
  - name: notify
    command: ./scripts/notify-slack.sh
```

**Events.** `session_start`, `user_prompt_submit`, `pre_tool_use`,
`post_tool_use`, `notification`, `stop`, `session_end`.

**Matcher.** A fully-anchored regular expression against the tool name. Empty
matches every tool. A matcher that does not compile is reported at load time
rather than silently never firing.

**The protocol** is the one Claude Code uses, so scripts written for that work
here. Your hook receives a JSON object on stdin:

```json
{
  "hook_event_name": "pre_tool_use",
  "session_id": "session-abc",
  "cwd": "/path/to/project",
  "tool_name": "write",
  "tool_input": {"path": "generated/schema.py"}
}
```

and answers with an exit code, optionally plus JSON on stdout:

| Exit | Meaning |
|---|---|
| `0` | Allow. Plain stdout becomes feedback the agent sees. |
| `2` | Block. stderr becomes the reason, and reaches the model. |
| other | The hook broke. Reported, then ignored. |

```json
{"permissionDecision": "deny", "permissionDecisionReason": "generated/ is regenerated by `make schema`"}
```

**What can actually stop something.** Only `pre_tool_use` and
`user_prompt_submit`. Everywhere else the work has already happened, so a
"deny" is downgraded to feedback — the reason still reaches the model, because
pretending it was enforced would tell the agent something untrue.

**Failure is not fatal.** A hook that crashes, times out, or prints nonsense is
reported and ignored. A broken formatter hook that blocked every edit in the
repository would be worse than an unformatted file. Only an explicit block — exit
2, or a JSON deny — stops anything.

**Ordering.** Hooks for one event run concurrently, and their answers are
combined with deny winning over ask winning over allow. Two hooks disagreeing
about whether an edit may happen resolves to "no".

`~/.daino/hooks.yaml` applies to every project, and both layers run: a project
cannot drop a hook your organisation configured globally.

---

## MCP servers

[Model Context Protocol](https://modelcontextprotocol.io) servers give the agent
tools D[Ai]NO does not implement — your database, your issue tracker, your
internal API. The file is the one every MCP client uses, so a configuration
copied from another tool works unchanged.

`.daino/mcp.json`:

```json
{
  "mcpServers": {
    "postgres": {
      "command": "uvx",
      "args": ["mcp-server-postgres", "postgresql://localhost/app"]
    },
    "sentry": {
      "type": "http",
      "url": "https://mcp.sentry.dev/mcp",
      "headers": {"Authorization": "Bearer ..."}
    },
    "internal": {
      "command": "node",
      "args": ["./tools/mcp-server.js"],
      "env": {"API_TOKEN": "env://INTERNAL_API_TOKEN"}
    }
  }
}
```

**Transports.** `stdio` launches a local process; `http` posts JSON-RPC to a URL
(`sse` and `streamable-http` are accepted as spellings of the same thing).
Omitting `transport` infers it from whether you gave a `command` or a `url`.

**Secrets.** An `env` value may be an `env://`, `keyring://` or `file://`
reference, resolved the same way a provider key is, so a token never has to be
written into a file in the checkout.

**Naming.** A server's tools reach the model as `mcp__<server>__<tool>`, so two
servers can both offer `search` without colliding.

**Narrowing.** `allowed_tools` and `denied_tools` limit what a server exposes. A
server with sixty tools otherwise spends a large part of the model's context
describing tools this project will never call.

**Failure is per-server.** A server that will not start is reported once and its
tools are absent; the session works without them. A tool result is labelled
untrusted before it reaches the model, for the same reason a fetched web page is:
it is text a third party wrote, and "ignore your previous instructions" is a
realistic thing to receive from one.

Servers connect on the first turn that needs them and stay connected for the
session. `GET /api/agent/extensions` reports which connected, which did not, and
why.

---

## Seeing what loaded

Everything here fails quietly by design — a broken hook is skipped so it cannot
block every edit, an unreachable server is dropped so it cannot stop a session.
So there is one place that says what actually happened:

```bash
curl localhost:PORT/api/agent/extensions
```

which lists the hooks, servers, skills and commands that loaded, and a `problems`
array for everything that did not.
