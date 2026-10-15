#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR/backend"

# The backend talks to HuRI through huri-core's client SDK and the curated
# huri-events (both in requirements.txt, installed from git): it needs no
# HuRI checkout on sys.path, no Ray and no torch.
VENV="$PROJECT_DIR/.venv"
if [ ! -d "$VENV" ]; then
  python3 -m venv "$VENV"
fi
source "$VENV/bin/activate"
# SKIP_PIP_INSTALL=1 keeps the venv as is, e.g. with huri-core / huri-events
# installed editable from local checkouts before their pinned tags exist.
if [ "${SKIP_PIP_INSTALL:-0}" != 1 ]; then
  pip install -q -r requirements.txt
fi

# The HuRI Control Panel (huri_launcher.py) finds the huri-modules checkout
# itself (HURI_MODULES_PATH, else the huri-modules/ submodule, else a sibling
# ../huri-modules) and says in the panel when it is missing or not installed.
echo "Starting FastAPI backend on http://localhost:8001"
uvicorn main:app --reload --host 0.0.0.0 --port 8001
