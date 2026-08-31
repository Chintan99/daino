# Getting started

This walkthrough takes a fresh D[Ai]NO installation through its first verified coding task. You
need [Python 3.12 or newer](https://www.python.org/downloads/), Git, and either a hosted model API
key or a running local model server.

!!! tip "Not installed yet?"

    Follow [Installation](installation.md) first. The recommended installer creates an isolated
    application environment, so you never need to activate a virtual environment to run D[Ai]NO.

## 1. Open and initialize a project

Move into an existing repository and initialize D[Ai]NO:

```bash
cd /path/to/your/project
daino init
```

Initialization creates `.daino/config.yaml`, a local SQLite database, and an incremental repository
index. It also selects a usable local or Docker runtime. Existing Git history is preserved. In an
empty directory, D[Ai]NO initializes Git and creates a baseline commit so checkpoints and missions
have a safe starting revision.

## 2. Connect a model

Choose one route. Provider configuration is saved globally so the model becomes available to
other projects, while project-specific overrides remain possible.

=== "OpenRouter"

    Export the secret in your shell; D[Ai]NO stores only the `env://` reference:

    ```bash
    export OPENROUTER_API_KEY="your-key"

    daino providers add openrouter \
      --type openrouter \
      --base-url https://openrouter.ai/api/v1 \
      --model openai/gpt-5.6 \
      --api-key-ref env://OPENROUTER_API_KEY
    ```

=== "Ollama (local)"

    Start Ollama and make sure the model has been pulled, then add its OpenAI-compatible endpoint:

    ```bash
    ollama pull qwen2.5-coder:7b

    daino providers add local-ollama \
      --type ollama \
      --base-url http://127.0.0.1:11434/v1 \
      --model qwen2.5-coder:7b \
      --local
    ```

=== "vLLM (local)"

    With a vLLM OpenAI-compatible server already running:

    ```bash
    daino providers add local-vllm \
      --type vllm \
      --base-url http://127.0.0.1:8000/v1 \
      --model Qwen/Qwen2.5-Coder-7B-Instruct \
      --local
    ```

Other OpenAI-compatible services use `--type openai-compatible`. See [Providers](providers.md) for
capabilities, tool calling, structured output, and local-server concurrency.

## 3. Verify the setup

Replace the provider/profile name if you chose a local option:

```bash
daino providers test openrouter
daino models test openrouter
daino doctor
```

`providers test` checks endpoint health, `models test` makes a minimal completion request, and
`doctor` validates the project configuration, database, Git, and runtime prerequisites.

## 4. Launch a workspace

Choose either interface:

```bash
daino . --tui    # terminal interface
daino . --gui    # browser IDE
```

The GUI runs as a background process by default. Use `daino ps` to list local GUI sessions and
`daino kill .` to stop the one for the current project. Add `--foreground` when you want its logs
attached to the terminal.

## 5. Complete a first task

Start with a small, verifiable request:

```text
Explain how this project starts and which file is the entry point.
```

Questions are answered without editing. Then request a change:

```text
Add input validation to the user creation endpoint and cover the invalid-email case with a test.
```

D[Ai]NO reads relevant files, maintains a visible checklist, asks before sensitive commands,
applies scoped edits, and runs the proposed verification. In the TUI, these commands are useful
after the turn:

```text
/diff          # inspect the complete change
/test          # run configured verification
/review        # request an independent model review
/checkpoint    # create another manual recovery point
```

Use `@` in the composer to attach a file or symbol explicitly. Prefix a command with `!` to run it
yourself and add its output to the conversation, for example `!pytest -q`.

## 6. Pick the right workflow

| Work | Recommended entry point |
|---|---|
| Ask about the repository | Plain prompt or `/ask` |
| Make a focused change | Plain prompt in the TUI or GUI |
| Preview a plan without edits | Plan mode or `/plan` |
| Split independent work | `/team <instruction>` |
| Run a durable, isolated workflow | `daino run "<request>"` or `/run` |
| Audit the whole project | QA workspace in the TUI or GUI |
| Automate checks | `daino test --json` |

The four autonomy modes—Plan, Ask, Session, and Full—control whether D[Ai]NO can edit or approve
sensitive command categories. They never bypass hard-denied destructive commands or repository
boundaries. Read [Security](security.md) before using Full mode or remote operations.

## Where D[Ai]NO stores data

| Location | Contents |
|---|---|
| `.daino/config.yaml` | Project runtime, verification, and security settings |
| `.daino/daino.db` | Sessions, missions, events, memory, checkpoints, and evidence metadata |
| `.daino/` | Project-local designs, QA reports, logs, workspaces, and generated state |
| `~/.config/daino/` | Global providers, model profiles, routing, preferences, and private secret files |
| `~/.daino/` | Cross-project user memory and global `DAINO.md` instructions |

Next, browse the [Feature overview](features.md), learn the [Terminal UI](tui.md), or explore the
[Browser IDE](gui.md).
