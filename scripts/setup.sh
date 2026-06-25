#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
.venv/bin/second-memory --help >/dev/null

mkdir -p "$HOME/.local/bin"
ln -sf "$ROOT/scripts/second-memory" "$HOME/.local/bin/second-memory"

CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
mkdir -p "$CODEX_HOME/skills"
ln -sfn "$ROOT" "$CODEX_HOME/skills/second-memory"

echo "second-memory setup ok"
