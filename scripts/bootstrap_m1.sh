#!/bin/zsh

set -euo pipefail

SCRIPT_DIR=${0:A:h}
PROJECT_ROOT=${SCRIPT_DIR:h}
cd "$PROJECT_ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This bootstrap script is for macOS only." >&2
  exit 1
fi

if [[ "$(uname -m)" != "arm64" ]]; then
  echo "This project targets Apple Silicon. Current architecture: $(uname -m)" >&2
  exit 1
fi

PYTHON_BIN=${PYTHON_BIN:-python3}

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python executable not found: $PYTHON_BIN" >&2
  exit 1
fi

PYTHON_CHECK=$(
  "$PYTHON_BIN" - <<'PY'
import platform
import sys

major, minor = sys.version_info[:2]
machine = platform.machine()

if (major, minor) < (3, 9):
    raise SystemExit("Python 3.9 or newer is required.")

if machine != "arm64":
    raise SystemExit(f"Python must run natively as arm64, got {machine}.")

print(sys.executable)
PY
)

echo "Using Python: $PYTHON_CHECK"

if [[ ! -d .venv ]]; then
  "$PYTHON_BIN" -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m compileall src

cat <<'EOF'

Bootstrap complete.

Next steps:
  source .venv/bin/activate
  python -m sound_recorder --list-devices
  python -m sound_recorder

On first recording start, grant microphone access to your terminal app in macOS.
EOF