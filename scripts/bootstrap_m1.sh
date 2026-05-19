#!/bin/zsh

set -euo pipefail

SCRIPT_DIR=${0:A:h}
source "$SCRIPT_DIR/common_m1.sh"

PROJECT_ROOT=$(project_root_from_script "$0")
cd "$PROJECT_ROOT"

require_macos_arm64_host

PYTHON_BIN=$(detect_python_bin "${PYTHON_BIN:-}")
PYTHON_CHECK=$(python_env_summary "$PYTHON_BIN")

echo "Using Python: $PYTHON_CHECK"

if [[ -d .venv && ! -x .venv/bin/python ]]; then
  echo "Existing .venv is incomplete. Recreating it."
  rm -rf .venv
fi

if [[ -d .venv && ! $(venv_python_healthy .venv/bin/python; echo $?) -eq 0 ]]; then
  echo "Existing .venv does not match the required Python version or architecture. Recreating it."
  rm -rf .venv
fi

if [[ ! -d .venv ]]; then
  "$PYTHON_BIN" -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m compileall src

if [[ -x scripts/post_install_check.sh ]]; then
  echo
  echo "Running post-install verification..."
  ./scripts/post_install_check.sh
fi

cat <<'EOF'

Bootstrap complete.

Next steps:
  source .venv/bin/activate
  python -m sound_recorder --list-devices
  python -m sound_recorder

On first recording start, grant microphone access to your terminal app in macOS.
EOF