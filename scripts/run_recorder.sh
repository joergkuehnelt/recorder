#!/bin/zsh

set -euo pipefail

SCRIPT_DIR=${0:A:h}
source "$SCRIPT_DIR/common_m1.sh"

PROJECT_ROOT=$(project_root_from_script "$0")
cd "$PROJECT_ROOT"

require_macos_arm64_host

if [[ ! -x .venv/bin/python ]] || ! venv_python_healthy .venv/bin/python; then
  echo "Local installation missing or invalid. Running bootstrap first..."
  ./scripts/bootstrap_m1.sh
fi

source .venv/bin/activate

if ! python - <<'PY' >/dev/null 2>&1
import sound_recorder
PY
then
  echo "Recorder package import failed. Re-running bootstrap..."
  ./scripts/bootstrap_m1.sh
  source .venv/bin/activate
fi

echo "Starting recorder from $PROJECT_ROOT"
exec python -m sound_recorder "$@"