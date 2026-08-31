# Installation

Install D[Ai]NO as a managed command-line application. This gives it an isolated Python
environment and a `daino` command that works from every project—no virtual environment activation
required.

## Requirements

| Requirement | Version or purpose |
|---|---|
| Python | 3.12 or newer |
| Git | Required for checkpoints, diffs, and mission worktrees |
| `uv` or `pipx` | Creates the isolated application environment |
| Docker | Optional; provides isolated command execution when its daemon is available |
| Node.js | Optional, version 18 or newer; needed to build the browser IDE on first use |

The terminal UI and local runtime work without Docker or Node.js.

## Recommended installation

On macOS or Linux, install D[Ai]NO with one command — no clone required:

```bash
curl -fsSL https://chintan99.github.io/daino/install.sh | sh
```

The script installs D[Ai]NO straight from GitHub. It prefers `uv tool` and falls back to `pipx`;
if neither is present it bootstraps `uv` first, then verifies the installed version and prints the
exact launcher path. It does not modify any project's Python dependencies.

## Install directly with uv or pipx

Already have an application manager? Install from GitHub without cloning:

=== "uv"

    ```bash
    uv tool install git+https://github.com/Chintan99/daino.git@v2
    uv tool update-shell
    ```

=== "pipx"

    ```bash
    pipx install git+https://github.com/Chintan99/daino.git@v2
    pipx ensurepath
    ```

On Windows, install `uv` first (`powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`), then
run the same `uv tool install git+…` command. Open a new terminal after updating `PATH`; the usual
uv launcher location on macOS and Linux is `~/.local/bin/daino`.

## Install from a clone

To build from a checked-out source tree — for a specific revision or offline install — run the
bundled installer, which detects the checkout and installs it in place:

```bash
git clone https://github.com/Chintan99/daino.git
cd daino
./scripts/install.sh
```

## Verify the installation

Run these commands outside the source checkout:

```bash
cd /tmp
daino --version
daino --help
```

The first command should print the installed D[Ai]NO version. Next, initialize a project:

```bash
cd /path/to/your/project
daino init
daino doctor
```

Continue with [Getting started](getting-started.md) to connect a hosted or local model and complete
your first task.

## Browser IDE installation

The browser IDE is optional. On its first launch, D[Ai]NO builds the React frontend automatically
when Node.js 18 or newer and npm are available:

```bash
cd /path/to/your/project
daino . --gui
```

If Node.js is unavailable, the local API still starts and its home page explains what is missing.
After installing Node.js, build the frontend manually from the D[Ai]NO source checkout if needed:

```bash
cd daino/gui
npm install
npm run build
```

Later GUI launches reuse the built assets. See the [Browser IDE guide](gui.md) for port, browser,
foreground, and development options.

## PATH troubleshooting

If installation succeeds but `daino` is not found, open a new terminal and inspect the launcher:

```bash
hash -r
which -a daino
```

For a uv installation, run:

```bash
uv tool update-shell
uv tool dir --bin
```

The second command prints the directory that must be on `PATH`. A normal uv launcher resolves into
the managed tool directory, not the source checkout's `.venv`.

On Windows PowerShell, use `Get-Command daino -All` instead of `which -a daino`, then restart
PowerShell after `uv tool update-shell` or `pipx ensurepath`.

## Upgrade

Re-run the one-line installer to pull the latest version:

```bash
curl -fsSL https://chintan99.github.io/daino/install.sh | sh
```

Or reinstall directly with the manager you chose:

```bash
uv tool install --force git+https://github.com/Chintan99/daino.git@v2
```

From a clone, update the checkout and rerun the bundled installer:

```bash
cd /path/to/daino
git pull --ff-only
./scripts/install.sh
```

Upgrading the application leaves every project's `.daino` state and the global configuration
untouched.

## Uninstall

Use the manager that installed D[Ai]NO:

=== "uv"

    ```bash
    uv tool uninstall daino
    ```

=== "pipx"

    ```bash
    pipx uninstall daino
    ```

Uninstalling the command does not remove `.daino` directories, project history, or global settings.
Delete those separately only when you intentionally want to remove their saved sessions and
state.

## Development installation

Contributors who are modifying D[Ai]NO itself need an editable environment instead of the
application-style install. Follow [Contributing](contributing.md) for the backend, frontend, test,
lint, and documentation commands.

The deprecated `vasuki` launcher remains as a compatibility alias and prints a migration notice;
new scripts and documentation should use `daino`.
