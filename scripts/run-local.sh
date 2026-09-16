#!/bin/bash
# Run Lite-Recorder on localhost without installing it system-wide.
#
#   ./scripts/run-local.sh              # http://127.0.0.1:8080, simulated cameras
#   ./scripts/run-local.sh --port 9000  # any extra args are passed to the app
#   ./scripts/run-local.sh --real       # use real V4L2 cameras instead of simulated
#
# Nothing touches /opt, /etc or /var: the Python virtualenv lives in
# ./.venv and recordings/state live in ./.local (both git-ignored).
# Requires python3 (with the venv module) and ffmpeg on PATH.
#
# If this file was checked out with CRLF line endings (Git for Windows with
# core.autocrlf=true, before .gitattributes forced LF), bash fails on
# "set -o pipefail\r". Re-run a copy with the CRs stripped instead. Kept on one
# line ending in a comment so this line itself parses with a trailing CR, and
# with no blank line above it (a bare CR would be run as a command).
[ -z "${LITE_RECORDER_CRLF_FIXED-}" ] && grep -q $'\r' "$0" && LITE_RECORDER_CRLF_FIXED=1 exec bash -c "$(tr -d '\r' < "$0")" "$0" "$@" #

set -euo pipefail

# BASH_SOURCE is empty under the "bash -c" re-exec above, so fall back to $0.
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
VENV_DIR="$REPO_DIR/.venv"
LOCAL_DIR="$REPO_DIR/.local"

HOST="${LITE_RECORDER_HOST:-127.0.0.1}"
PORT="${LITE_RECORDER_PORT:-8080}"
SIMULATE=1
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --real) SIMULATE=0; shift ;;
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//' | tr -d '\r'
      exit 0 ;;
    *) EXTRA_ARGS+=("$1"); shift ;;
  esac
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required (https://www.python.org/downloads/)" >&2
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg is required on PATH (Debian/Ubuntu: sudo apt install ffmpeg; macOS: brew install ffmpeg)" >&2
  exit 1
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "==> Creating virtualenv in $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

# Re-install only when requirements changed since the last run.
STAMP="$VENV_DIR/.requirements.stamp"
if [[ ! -f "$STAMP" ]] || [[ "$REPO_DIR/requirements.txt" -nt "$STAMP" ]]; then
  echo "==> Installing Python dependencies"
  "$VENV_DIR/bin/pip" install --quiet --upgrade pip
  "$VENV_DIR/bin/pip" install --quiet -r "$REPO_DIR/requirements.txt"
  touch "$STAMP"
fi

mkdir -p "$LOCAL_DIR/recordings"
export LITE_RECORDER_RECORDINGS_ROOT="${LITE_RECORDER_RECORDINGS_ROOT:-$LOCAL_DIR/recordings}"
export LITE_RECORDER_STATE_DIR="${LITE_RECORDER_STATE_DIR:-$LOCAL_DIR}"

ARGS=(--host "$HOST" --port "$PORT")
if [[ "$SIMULATE" == 1 ]]; then
  ARGS+=(--simulate)
fi

echo "==> Starting Lite-Recorder at http://$HOST:$PORT  (Ctrl-C to stop)"
echo "    recordings: $LITE_RECORDER_RECORDINGS_ROOT"
cd "$REPO_DIR"
exec "$VENV_DIR/bin/python" -m lite_recorder "${ARGS[@]}" "${EXTRA_ARGS[@]}"
