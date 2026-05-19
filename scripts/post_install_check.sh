#!/bin/zsh

set -euo pipefail

SCRIPT_DIR=${0:A:h}
PROJECT_ROOT=${SCRIPT_DIR:h}
cd "$PROJECT_ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Post-install check is for macOS only." >&2
  exit 1
fi

if [[ "$(uname -m)" != "arm64" ]]; then
  echo "Expected Apple Silicon machine, got: $(uname -m)" >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  echo "Virtual environment not found. Run scripts/bootstrap_m1.sh first." >&2
  exit 1
fi

source .venv/bin/activate

python - <<'PY'
import platform
import sys

if platform.machine() != "arm64":
    raise SystemExit(f"Python is not running as arm64: {platform.machine()}")

print(f"Python executable: {sys.executable}")
print(f"Python version: {sys.version.split()[0]}")
print(f"Python architecture: {platform.machine()}")
PY

echo
echo "Checking package import..."
python - <<'PY'
import sound_recorder

print(f"sound-recorder version: {sound_recorder.__version__}")
PY

echo
echo "Checking device discovery..."
python -m sound_recorder --list-devices

cat <<'EOF'

Post-install check complete.

If device listing worked, the deployment is ready for a live recording test.
Recommended next step:
  python -m sound_recorder --segment-minutes 1

On first real recording start, grant microphone access to your terminal app in macOS.
EOF