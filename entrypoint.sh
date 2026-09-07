#!/usr/bin/env bash
# Role switch. One image, two roles, one process per container.
#
# Fails FAST and LOUDLY. A previous project shipped an entrypoint defaulting to
# ROLE=both after its UI dependency had been removed from requirements, so every
# container died on "command not found" with no explanation. Exit 78 (EX_CONFIG)
# says "your configuration is wrong", not "the app crashed".
set -euo pipefail

ROLE="${ROLE:-}"
API_PORT="${API_PORT:-8000}"
UI_PORT="${UI_PORT:-8501}"

die() { echo "FATAL: $*" >&2; exit 78; }

case "$ROLE" in
  api)
    command -v uvicorn >/dev/null || die "ROLE=api but uvicorn is not installed in this image"
    echo "starting API on :${API_PORT}"
    exec uvicorn src.api:app --host 0.0.0.0 --port "${API_PORT}" \
         --workers 1 --timeout-graceful-shutdown 25
    ;;
  ui)
    command -v streamlit >/dev/null || die "ROLE=ui but streamlit is not installed in this image"
    echo "starting UI on :${UI_PORT} talking to ${API_URL:-unset}"
    [ -n "${API_URL:-}" ] || die "ROLE=ui requires API_URL"
    exec streamlit run src/app.py \
         --server.port "${UI_PORT}" --server.address 0.0.0.0 \
         --server.headless true --browser.gatherUsageStats false
    ;;
  train)
    exec python -m src.train "$@"
    ;;
  "")
    die "ROLE is not set. Expected one of: api | ui | train"
    ;;
  *)
    die "ROLE='${ROLE}' is not recognised. Expected one of: api | ui | train"
    ;;
esac
