#!/usr/bin/env sh
# Install D[Ai]NO as a user application.
#
# Two ways to run it:
#   From a clone:  ./scripts/install.sh
#                  (installs the checked-out source)
#   Over the web:  curl -fsSL https://chintan99.github.io/daino/install.sh | sh
#                  (installs straight from GitHub, no clone needed)
#
# The web copy served at the URL above is docs/install.sh, kept identical to
# this file so the GitHub Pages site can host the one-line installer.
set -eu

REPO_URL="https://github.com/Chintan99/daino.git"
REPO_REF="v2"
GIT_SPEC="git+${REPO_URL}@${REPO_REF}"

# Prefer a local checkout when this script is run from one; otherwise install
# from GitHub. When piped through `sh`, "$0" is not a readable file, so the
# checkout branch is skipped and GIT_SPEC is used.
source_spec="$GIT_SPEC"
source_desc="GitHub (${REPO_REF})"
project_dir=""
self="$0"
if [ -f "$self" ]; then
    script_dir=$(CDPATH= cd -- "$(dirname -- "$self")" && pwd)
    candidate=$(dirname -- "$script_dir")
    if [ -f "$candidate/pyproject.toml" ]; then
        project_dir="$candidate"
        source_spec="$candidate"
        source_desc="$candidate"
    fi
fi

# D[Ai]NO installs through uv or pipx. If neither is present, bootstrap uv so
# the one-line install still works on a clean machine.
if ! command -v uv >/dev/null 2>&1 && ! command -v pipx >/dev/null 2>&1; then
    printf '%s\n' "Neither uv nor pipx found; installing uv from https://astral.sh/uv ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv installs its launcher under ~/.local/bin (or ~/.cargo/bin on older
    # setups); make it visible to the rest of this script.
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH
fi

printf '%s\n' "Installing D[Ai]NO from ${source_desc} ..."
if command -v uv >/dev/null 2>&1; then
    uv tool install --force --reinstall-package daino "$source_spec"
    bin_dir=$(uv tool dir --bin 2>/dev/null || printf '%s' "$HOME/.local/bin")
elif command -v pipx >/dev/null 2>&1; then
    pipx install --force "$source_spec"
    bin_dir=$(python3 -m site --user-base)/bin
else
    printf '%s\n' "D[Ai]NO needs uv or pipx to install." >&2
    printf '%s\n' "Install uv from https://astral.sh/uv and run this again." >&2
    exit 1
fi

daino_bin="$bin_dir/daino"
if [ ! -x "$daino_bin" ]; then
    # uv/pipx may resolve a different bin directory; fall back to PATH.
    if command -v daino >/dev/null 2>&1; then
        daino_bin=$(command -v daino)
        bin_dir=$(dirname -- "$daino_bin")
    else
        printf '%s\n' "Installation finished, but the daino launcher was not found." >&2
        printf '%s\n' "Add ~/.local/bin to PATH (uv tool update-shell / pipx ensurepath)," >&2
        printf '%s\n' "then open a new terminal." >&2
        exit 1
    fi
fi

installed_version=$("$daino_bin" --version)

# For a local checkout, confirm the installed version matches the source.
if [ -n "$project_dir" ]; then
    expected_version=$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$project_dir/pyproject.toml")
    if [ "$installed_version" != "daino $expected_version" ]; then
        printf '%s\n' "Installed version check failed." >&2
        printf '%s\n' "Expected: daino $expected_version" >&2
        printf '%s\n' "Found:    $installed_version" >&2
        exit 1
    fi
fi

printf '%s\n' ""
printf '%s\n' "$installed_version installed"
printf '%s\n' "Launcher: $daino_bin"

case ":${PATH:-}:" in
    *":$bin_dir:"*)
        printf '%s\n' "Next:     cd /path/to/project && daino --gui"
        ;;
    *)
        printf '%s\n' "$bin_dir is not on PATH yet. Add it, then open a new terminal:"
        printf '  export PATH="%s:$PATH"\n' "$bin_dir"
        ;;
esac
