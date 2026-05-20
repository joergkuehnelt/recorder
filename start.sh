#!/usr/bin/env bash
# start.sh — bootstrap and launch the Sound Recorder on macOS Apple Silicon.
# Usage: ./start.sh [--segment-minutes N] [--list-devices] [...]
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$REPO_DIR/.venv"
PYTHON_MIN_VERSION="3.9"

# ── 1. Platform guard ─────────────────────────────────────────────────────────
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "Error: Sound Recorder requires macOS." >&2
  exit 1
fi
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "Error: Sound Recorder requires Apple Silicon (arm64)." >&2
  exit 1
fi

# ── 2. Find a suitable Python ─────────────────────────────────────────────────
find_python() {
  local candidates=(
    python3
    python3.13 python3.12 python3.11 python3.10 python3.9
    /opt/homebrew/bin/python3
    /usr/local/bin/python3
  )
  for cmd in "${candidates[@]}"; do
    if command -v "$cmd" &>/dev/null; then
      local arch
      arch=$(python3 -c "import platform; print(platform.machine())" 2>/dev/null || true)
      if [[ "$arch" == "arm64" ]]; then
        local ver
        ver=$("$cmd" -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>/dev/null || true)
        if python3 -c "import sys; exit(0 if sys.version_info >= (3,9) else 1)" 2>/dev/null; then
          echo "$cmd"
          return 0
        fi
      fi
    fi
  done
  return 1
}

PYTHON=$(find_python || true)
if [[ -z "$PYTHON" ]]; then
  echo "Error: No arm64 Python >= ${PYTHON_MIN_VERSION} found." >&2
  echo "Install via Homebrew:  brew install python" >&2
  exit 1
fi

# ── 3. Create venv if missing ─────────────────────────────────────────────────
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  echo "Creating virtual environment in .venv …"
  "$PYTHON" -m venv "$VENV_DIR"
fi

# ── 4. Install / upgrade the package ─────────────────────────────────────────
echo "Checking dependencies …"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -e "$REPO_DIR"

# ── 5. Launch ─────────────────────────────────────────────────────────────────
exec "$VENV_DIR/bin/python" -m sound_recorder "$@"
