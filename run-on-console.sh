#!/bin/bash
# Run the dashboard on the Pi's local console (tty1) for debugging touch/click.
# SSH is not enough — use the Pi's screen and keyboard, or switch to tty1 (Ctrl+Alt+F1).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
VENV="${ROOT}/.venv/bin/python"
SCRIPT="${ROOT}/pagerduty_dashboard.py"

if [[ ! -x "$VENV" ]]; then
  echo "Missing venv: ${ROOT}/.venv — run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi

if systemctl is-active --quiet pagerduty-dashboard.service 2>/dev/null; then
  echo "Stopping pagerduty-dashboard.service so it does not hold tty1 or the touch device..."
  sudo systemctl stop pagerduty-dashboard.service
fi

if [[ -r /etc/pagerduty-dashboard.env ]]; then
  set -a
  # shellcheck disable=SC1091
  source /etc/pagerduty-dashboard.env
  set +a
fi

export PAGERDUTY_POINTER_DEVICE="${PAGERDUTY_POINTER_DEVICE:-/dev/input/event6}"
export PAGERDUTY_DEBUG_POINTER="${PAGERDUTY_DEBUG_POINTER:-1}"

echo "Pointer device: ${PAGERDUTY_POINTER_DEVICE}"
echo "Debug pointer:  ${PAGERDUTY_DEBUG_POINTER} (row numbers on stderr)"
echo "Quit with q. Touch the screen — watch for 'pointer: row N' below if debug is on."
echo "---"

cd "$ROOT"
exec "$VENV" "$SCRIPT" --debug-pointer "$@"
