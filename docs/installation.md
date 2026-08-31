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

Clone the repository and run the included installer:

```bash
git clone https://github.com/Chintan99/daino.git
cd daino
./scripts/install.sh
```

The installer prefers `uv tool`, falls back to `pipx`, verifies the installed version, and prints
the exact launcher path. It does not modify a project's Python dependencies.

If neither application manager is installed, install `uv` and run the script again:

```bash
python3 -m pip install --user uv
./scripts/install.sh
```

!!! note "macOS and Linux"

    The shell installer is the simplest route. On Windows, use the direct `uv tool install .`
    command from PowerShell after cloning the repository.

## Install directly with uv or pipx

From the cloned D[Ai]NO source directory:

=== "uv"

    ```bash
    uv tool install .
    uv tool update-shell
    ```

=== "pipx"

    ```bash
    pipx install .
    pipx ensurepath
    ```

Open a new terminal after updating `PATH`. The usual uv launcher location on macOS and Linux is
`~/.local/bin/daino`.

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

Update the source checkout and rerun the installer:

```bash
cd /path/to/daino
git pull --ff-only
./scripts/install.sh
```

Or reinstall directly with the manager you chose:

```bash
uv tool install --force --reinstall-package daino .
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
