#!/usr/bin/env bash
# Install the cst CLI and daemon. Uses uv when present; otherwise a private venv.
# The command lands in ~/.local/bin/cst, where the desktop app looks for it.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin"
mkdir -p "$BIN"
if command -v uv >/dev/null 2>&1; then
  uv tool install --force --editable "$HERE/core"
else
  PY="$(command -v python3)"
  "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { echo "needs Python 3.11+"; exit 1; }
  VENV="$HOME/.local/share/captain-search/venv"
  "$PY" -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -e "$HERE/core"
  ln -sf "$VENV/bin/cst" "$BIN/cst"
fi
echo "installed: $BIN/cst"
case ":$PATH:" in *":$BIN:"*) ;; *) echo "add $BIN to your PATH";; esac
