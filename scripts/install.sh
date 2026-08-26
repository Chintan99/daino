#!/usr/bin/env sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(dirname -- "$script_dir")

if command -v uv >/dev/null 2>&1; then
    printf '%s\n' "Installing Daino as a user application with uv..."
    uv tool install --force --reinstall-package daino "$project_dir"
    bin_dir=$(uv tool dir --bin)
elif command -v pipx >/dev/null 2>&1; then
    printf '%s\n' "Installing Daino as a user application with pipx..."
    pipx install --force "$project_dir"
    bin_dir=$(python3 -m site --user-base)/bin
else
    printf '%s\n' "Daino needs uv or pipx for an application-style install." >&2
    printf '%s\n' "Install uv, then run this script again:" >&2
    printf '%s\n' "  python3 -m pip install --user uv" >&2
    exit 1
fi

daino_bin="$bin_dir/daino"
if [ ! -x "$daino_bin" ]; then
    printf '%s\n' "Installation completed, but $daino_bin was not created." >&2
    exit 1
fi

expected_version=$(sed -n 's/^version = "\([^"]*\)"/\1/p' "$project_dir/pyproject.toml")
installed_version=$("$daino_bin" --version)
if [ "$installed_version" != "daino $expected_version" ]; then
    printf '%s\n' "Installed version check failed." >&2
    printf '%s\n' "Expected: daino $expected_version" >&2
    printf '%s\n' "Found:    $installed_version" >&2
    exit 1
fi

printf '%s\n' "$installed_version"
printf '%s\n' "Installed: $daino_bin"
printf '%s\n' "Source:    $project_dir"

case ":${PATH:-}:" in
    *":$bin_dir:"*)
        printf '%s\n' "Run 'daino' from any directory."
        ;;
    *)
        printf '%s\n' "$bin_dir is not currently on PATH."
        printf '%s\n' "Add this line to your shell profile, then open a new terminal:"
        printf '  export PATH="%s:$PATH"\n' "$bin_dir"
        ;;
esac
