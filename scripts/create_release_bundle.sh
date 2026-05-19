#!/bin/zsh

set -euo pipefail

SCRIPT_DIR=${0:A:h}
PROJECT_ROOT=${SCRIPT_DIR:h}
cd "$PROJECT_ROOT"

VERSION=$(sed -n 's/^version = "\([^"]*\)"$/\1/p' pyproject.toml | head -n 1)
if [[ -z "$VERSION" ]]; then
  echo "Unable to determine project version from pyproject.toml" >&2
  exit 1
fi

RELEASE_DIR=${RELEASE_DIR:-releases}
ARCHIVE_BASENAME="sound-recorder-${VERSION}-macos-arm64"
ARCHIVE_PATH="$RELEASE_DIR/${ARCHIVE_BASENAME}.tar.gz"
CHECKSUM_PATH="$RELEASE_DIR/${ARCHIVE_BASENAME}.sha256"

mkdir -p "$RELEASE_DIR"
rm -f "$ARCHIVE_PATH" "$CHECKSUM_PATH"

tar \
  --exclude='.git' \
  --exclude='.venv' \
  --exclude='.venv-1' \
  --exclude='recordings' \
  --exclude='releases' \
  --exclude='**/__pycache__' \
  --exclude='*.pyc' \
  -czf "$ARCHIVE_PATH" \
  .gitignore \
  README.md \
  pyproject.toml \
  .github \
  .vscode \
  scripts \
  src

shasum -a 256 "$ARCHIVE_PATH" > "$CHECKSUM_PATH"

cat <<EOF
Release bundle created:
  $ARCHIVE_PATH
Checksum file:
  $CHECKSUM_PATH

Copy both files to the target MacBook Pro M1, then run:
  tar -xzf ${ARCHIVE_BASENAME}.tar.gz
  cd sound-recorder
  chmod +x scripts/bootstrap_m1.sh
  ./scripts/bootstrap_m1.sh
EOF