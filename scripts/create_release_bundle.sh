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
PACKAGE_DIR_NAME="recorder"
STAGING_ROOT=$(mktemp -d)
STAGING_DIR="$STAGING_ROOT/$PACKAGE_DIR_NAME"

cleanup() {
  rm -rf "$STAGING_ROOT"
}

trap cleanup EXIT

mkdir -p "$RELEASE_DIR"
rm -f "$ARCHIVE_PATH" "$CHECKSUM_PATH"

mkdir -p "$STAGING_DIR"
cp -R \
  .gitignore \
  README.md \
  pyproject.toml \
  .github \
  .vscode \
  scripts \
  src \
  "$STAGING_DIR"

tar \
  -czf "$ARCHIVE_PATH" \
  -C "$STAGING_ROOT" \
  "$PACKAGE_DIR_NAME"

shasum -a 256 "$ARCHIVE_PATH" > "$CHECKSUM_PATH"

cat <<EOF
Release bundle created:
  $ARCHIVE_PATH
Checksum file:
  $CHECKSUM_PATH

Copy both files to the target MacBook Pro M1, then run:
  tar -xzf ${ARCHIVE_BASENAME}.tar.gz
  cd recorder
  chmod +x scripts/bootstrap_m1.sh
  ./scripts/bootstrap_m1.sh
EOF