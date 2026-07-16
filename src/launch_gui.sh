#!/usr/bin/env bash
# Linux analog of launch_gui.bat. Resolves this script's own location, cd's to
# the repo root (parent of src/), and launches the tkinter GUI so it works no
# matter where it's invoked from (a .desktop launcher, the app menu, or a shell).
# readlink -f resolves symlinks, so a desktop shortcut that's a symlink to this
# file still finds the real src/ directory.
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO_ROOT="$(dirname "$SRC_DIR")"
cd "$REPO_ROOT"

# Prefer the repo venv if present (matches the .bat's .venv assumption),
# otherwise fall back to whatever python3 is on PATH.
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="python3"
fi

exec "$PY" "src/gui_tk.py" "$@"