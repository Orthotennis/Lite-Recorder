#!/bin/bash
# Run Lite-Recorder on localhost without installing it system-wide.
#
#   ./scripts/run-local.sh              # http://127.0.0.1:8080, real cameras
#   ./scripts/run-local.sh --port 9000  # any extra args are passed to the app
#   ./scripts/run-local.sh --real       # fail loudly if no real camera is found
#   ./scripts/run-local.sh --simulate   # synthetic test-pattern cameras
#
# By default it records from whatever V4L2 cameras (USB webcam, MIPI CSI)
# are attached, and only falls back to synthetic test patterns if none are
# found -- saying so on the console and in the web UI.
#
# Nothing touches /opt, /etc or /var: the Python virtualenv lives in
# ./.venv and recordings/state live in ./.local (both git-ignored).
# Requires python3 (with the venv module) and ffmpeg on PATH.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_DIR/.venv"
LOCAL_DIR="$REPO_DIR/.local"

HOST="${LITE_RECORDER_HOST:-127.0.0.1}"
PORT="${LITE_RECORDER_PORT:-8080}"
CAMERA_MODE="${LITE_RECORDER_CAMERA_MODE:-auto}"
EXTRA_ARGS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --real) CAMERA_MODE=real; shift ;;
    --simulate|--sim) CAMERA_MODE=simulate; shift ;;
    --camera-mode) CAMERA_MODE="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    -h|--help)
      sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) EXTRA_ARGS+=("$1"); shift ;;
  esac
done

case "$CAMERA_MODE" in
  auto|real|simulate) ;;
  *) echo "unknown camera mode: $CAMERA_MODE (expected auto, real or simulate)" >&2; exit 2 ;;
esac

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required (https://www.python.org/downloads/)" >&2
  exit 1
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg is required on PATH (Debian/Ubuntu: sudo apt install ffmpeg; macOS: brew install ffmpeg)" >&2
  exit 1
fi

# Report what the app is about to find, so "why am I seeing test patterns?"
# is answered before the browser is even open.
if [[ "$CAMERA_MODE" != simulate ]]; then
  VIDEO_NODES=(/dev/video*)
  if [[ ! -e "${VIDEO_NODES[0]}" ]]; then
    echo "==> No /dev/video* devices found."
    if [[ "$CAMERA_MODE" == real ]]; then
      echo "    --real was requested, so no cameras will be available." >&2
      echo "    Plug in a USB webcam (under WSL, attach it with usbipd) and retry." >&2
    else
      echo "    Falling back to simulated cameras (synthetic test patterns)."
      echo "    Plug in a USB webcam and rerun for real capture."
    fi
  else
    echo "==> Video devices: ${VIDEO_NODES[*]}"
    UNREADABLE=()
    for node in "${VIDEO_NODES[@]}"; do
      [[ -r "$node" && -w "$node" ]] || UNREADABLE+=("$node")
    done
    if [[ ${#UNREADABLE[@]} -gt 0 ]]; then
      echo "    Not readable/writable by $(id -un): ${UNREADABLE[*]}"
      echo "    Fix with: sudo usermod -aG video $(id -un)   (then log out and back in)"
    fi
  fi
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

ARGS=(--host "$HOST" --port "$PORT" --camera-mode "$CAMERA_MODE")

echo "==> Starting Lite-Recorder at http://$HOST:$PORT  (Ctrl-C to stop)"
echo "    cameras:    $CAMERA_MODE"
echo "    recordings: $LITE_RECORDER_RECORDINGS_ROOT"
cd "$REPO_DIR"
exec "$VENV_DIR/bin/python" -m lite_recorder "${ARGS[@]}" "${EXTRA_ARGS[@]}"
