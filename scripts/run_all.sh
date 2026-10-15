#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Local dev by default: no Authelia, no signed-in session required. Export
# REQUIRE_AUTH=1 yourself (plus OIDC_ISSUER/etc, see backend/main.py) to
# exercise the real login flow instead.
export REQUIRE_AUTH="${REQUIRE_AUTH:-0}"
export VITE_BACKEND_URL="${VITE_BACKEND_URL:-http://localhost:8001}"
BACKEND_URL="${VITE_BACKEND_URL}"

pids=()

cleanup() {
  echo
  echo "==> Shutting down..."
  # Ask the backend's launcher to stop a HuRI it started while the backend is
  # still up: killing run_backend.sh below may not reach uvicorn's shutdown.
  curl -fsS -X POST "$BACKEND_URL/launcher/huri/stop" >/dev/null 2>&1 || true
  for pid in "${pids[@]:-}"; do
    kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "==> Starting backend on $BACKEND_URL ..."
"$SCRIPT_DIR/run_backend.sh" &
pids+=("$!")

echo "==> Waiting for the backend to come up..."
for _ in $(seq 1 60); do
  curl -fsS "$BACKEND_URL/presets" >/dev/null 2>&1 && break
  sleep 1
done

echo "==> Starting frontend..."
"$SCRIPT_DIR/run_frontend.sh" &
pids+=("$!")

echo
echo "Backend and frontend are up. HuRI itself is NOT started — open"
echo "http://localhost:5173 and use the HuRI Control Panel (it opens"
echo "automatically) to pick a config and start it. Ctrl-C here stops"
echo "everything cleanly, including HuRI if you started it."
wait
