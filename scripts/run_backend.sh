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
pip install -q -r requirements.txt

# The HuRI Control Panel (huri_launcher.py) runs `serve run` from the
# huri-modules checkout: the git submodule at <repo root>/huri-modules, else a
# sibling ../huri-modules, or HURI_MODULES_PATH. It uses that checkout's
# installer venv (huri-modules/.venv/bin/{serve,ray}) and environment
# (huri-modules/.huri-local/env.sh), so run huri-modules/install.sh first;
# HURI_SERVE_BIN/HURI_RAY_BIN override the binaries.
if [ -z "${HURI_MODULES_PATH:-}" ]; then
  if [ -d "$PROJECT_DIR/huri-modules/config" ]; then
    HURI_MODULES_PATH="$PROJECT_DIR/huri-modules"
  elif [ -d "$PROJECT_DIR/../huri-modules/config" ]; then
    HURI_MODULES_PATH="$(cd "$PROJECT_DIR/../huri-modules" && pwd)"
  fi
fi
if [ -z "${HURI_MODULES_PATH:-}" ]; then
  echo "Warning: could not find huri-modules (submodule not checked out, no sibling checkout either);" >&2
  echo "  run 'git submodule update --init' or set HURI_MODULES_PATH explicitly." >&2
  echo "  The Control Panel cannot launch HuRI without it; a HuRI started elsewhere still works." >&2
else
  export HURI_MODULES_PATH
  if [ ! -x "$HURI_MODULES_PATH/.venv/bin/serve" ] && [ -z "${HURI_SERVE_BIN:-}" ]; then
    echo "Warning: $HURI_MODULES_PATH is not installed (no .venv/bin/serve);" >&2
    echo "  run '$HURI_MODULES_PATH/install.sh' to launch HuRI from the Control Panel." >&2
  fi
fi

echo "Starting FastAPI backend on http://localhost:8001 (HURI_MODULES_PATH=${HURI_MODULES_PATH:-})"
uvicorn main:app --reload --host 0.0.0.0 --port 8001
