# Install Daino as an application

Daino should be installed as a command-line application, not into a repository's development
virtual environment. The application install creates a private, managed Python environment and
places a `daino` launcher in the user's command path. It never needs to be activated.

Python 3.12 or newer and Git are required. Docker is recommended for isolated mission execution.

## Install from a checkout

The included installer uses `uv tool` when available and falls back to `pipx`:

```bash
git clone <daino-repository-url>
cd daino
./scripts/install.sh
```

The equivalent direct command is:

```bash
uv tool install .
```

By default, uv places the launcher in `~/.local/bin/daino`. If that directory is not already in
the shell's `PATH`, run `uv tool update-shell` once and open a new terminal.

Verify the installation from outside the source checkout:

```bash
cd /tmp
daino --version
```

Then open any project:

```bash
cd /path/to/project
daino
```

Daino keeps project state in that project's `.daino` directory. Provider credentials remain in
environment variables; saved configuration contains only secret references such as
`env://OPENROUTER_API_KEY`.

Running `daino init` in a greenfield directory also initializes Git and creates the first baseline
commit. Existing repositories and their history are left intact. This gives direct edits an
immediate pre-change checkpoint and gives worktree missions a valid starting revision.

## Upgrade

Pull or download the newer Daino source and run the installer again:

```bash
./scripts/install.sh
```

The managed application and launcher are replaced without affecting any project's `.daino`
configuration or history.

## Confirm which installation is running

The installer prints both the installed version and launcher path. If an existing terminal still
appears to run an older release, clear its command cache and inspect every matching launcher:

```bash
hash -r
which -a daino
daino --version
```

For a uv installation, `~/.local/bin/daino` should resolve into
`~/.local/share/uv/tools/daino`, not the source checkout's `.venv`.

## Uninstall

Use the manager that installed the application:

```bash
uv tool uninstall daino
```

or:

```bash
pipx uninstall daino
```

Uninstalling the command does not remove `.daino` directories from projects.

## Development setup

Only contributors working on Daino itself need an editable virtual environment. See
[Contributing](contributing.md) for that workflow.
