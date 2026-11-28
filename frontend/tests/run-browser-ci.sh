#!/usr/bin/env bash
# Start the isolated API and Vite, then run the browser regression suite.
# Intended for the Linux CI browser job; local Windows contributors can run the
# two servers and the Node commands manually as documented in README.md.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ARTIFACTS="${BROWSER_ARTIFACT_DIR:-$ROOT/frontend/test-results}"
mkdir -p "$ARTIFACTS"
API_LOG="$ARTIFACTS/api.log"
VITE_LOG="$ARTIFACTS/vite.log"
API_PID=""
VITE_PID=""

cleanup() {
  if [ -n "$VITE_PID" ]; then kill "$VITE_PID" 2>/dev/null || true; fi
  if [ -n "$API_PID" ]; then kill "$API_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT

python "$ROOT/frontend/tests/serve_api.py" >"$API_LOG" 2>&1 &
API_PID=$!

for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:18086/health >/dev/null 2>&1; then break; fi
  if ! kill -0 "$API_PID" 2>/dev/null; then
    echo "API fixture exited before becoming ready" >&2
    cat "$API_LOG" >&2 || true
    exit 1
  fi
  sleep 1
done
curl -fsS http://127.0.0.1:18086/health >/dev/null

FIXTURE_DIR="$(sed -n 's/^FIXTURE_DIR=//p' "$API_LOG" | head -n 1)"
if [ -z "$FIXTURE_DIR" ]; then
  echo "FIXTURE_DIR was not printed by the API fixture" >&2
  cat "$API_LOG" >&2 || true
  exit 1
fi

cd "$ROOT/frontend"
VITE_API_TARGET=http://127.0.0.1:18086 \
  ./node_modules/.bin/vite --configLoader runner --host 127.0.0.1 --port 5174 --strictPort \
  >"$VITE_LOG" 2>&1 &
VITE_PID=$!
echo "$VITE_PID" >"$ARTIFACTS/vite.pid"
cd "$ROOT"

for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:5174/ >/dev/null 2>&1; then break; fi
  if ! kill -0 "$VITE_PID" 2>/dev/null; then
    echo "Vite exited before becoming ready" >&2
    cat "$VITE_LOG" >&2 || true
    exit 1
  fi
  sleep 1
done
curl -fsS http://127.0.0.1:5174/ >/dev/null

export FRONTEND_TEST_URL=http://127.0.0.1:5174
export BROWSER_ARTIFACT_DIR="$ARTIFACTS"
# CI has no PLAYWRIGHT_MODULE and resolves the pinned frontend dependency;
# local developers may point it at an existing Playwright installation.

cd "$ROOT"
node frontend/tests/chat-flow-browser.cjs
node frontend/tests/workbench-browser.cjs
node frontend/tests/session-browser.cjs
node frontend/tests/ingestion-browser.cjs "$FIXTURE_DIR"
node frontend/tests/project-library-browser.cjs "$ARTIFACTS"

echo "Browser regression suite passed."
