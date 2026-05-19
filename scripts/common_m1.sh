#!/bin/zsh

set -euo pipefail

project_root_from_script() {
  local script_path=$1
  local script_dir=${script_path:A:h}
  print -- ${script_dir:h}
}

require_macos_arm64_host() {
  if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "This script is for macOS only." >&2
    exit 1
  fi

  if [[ "$(uname -m)" != "arm64" ]]; then
    echo "This project targets Apple Silicon. Current architecture: $(uname -m)" >&2
    exit 1
  fi
}

detect_python_bin() {
  local requested_python=${1:-}

  if [[ -n "$requested_python" ]]; then
    if ! command -v "$requested_python" >/dev/null 2>&1; then
      echo "Python executable not found: $requested_python" >&2
      exit 1
    fi
    print -- "$requested_python"
    return
  fi

  local candidate
  for candidate in python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
      print -- "$candidate"
      return
    fi
  done

  echo "No suitable python3 executable found in PATH." >&2
  exit 1
}

python_env_summary() {
  local python_bin=$1

  "$python_bin" - <<'PY'
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
}

venv_python_healthy() {
  local venv_python=$1

  [[ -x "$venv_python" ]] || return 1

  "$venv_python" - <<'PY' >/dev/null 2>&1
import platform
import sys

if sys.version_info[:2] < (3, 9):
    raise SystemExit(1)

if platform.machine() != "arm64":
    raise SystemExit(1)
PY
}