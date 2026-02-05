#!/usr/bin/env bash
set -euo pipefail

MODE="symlink"
FORCE="false"
TARGET_DIR="${HOME}/.local/bin"
TARGET_NAME="memlog"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --force)
      FORCE="true"
      shift
      ;;
    --mode)
      MODE="$2"
      shift 2
      ;;
    --target-dir)
      TARGET_DIR="$2"
      shift 2
      ;;
    --target-name)
      TARGET_NAME="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      exit 2
      ;;
  esac
done

if [[ "$MODE" != "symlink" && "$MODE" != "copy" ]]; then
  echo "--mode must be 'symlink' or 'copy'" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_FILE="${SCRIPT_DIR}/memlog"
TARGET_PATH="${TARGET_DIR}/${TARGET_NAME}"

mkdir -p "$TARGET_DIR"

if [[ -e "$TARGET_PATH" || -L "$TARGET_PATH" ]]; then
  if [[ "$FORCE" != "true" ]]; then
    echo "Target exists: ${TARGET_PATH} (use --force to replace)" >&2
    exit 1
  fi
  rm -f "$TARGET_PATH"
fi

if [[ "$MODE" == "symlink" ]]; then
  ln -s "$SOURCE_FILE" "$TARGET_PATH"
else
  cp "$SOURCE_FILE" "$TARGET_PATH"
  chmod +x "$TARGET_PATH"
fi

echo "Installed ${TARGET_PATH}"
echo "Ensure ${TARGET_DIR} is in PATH"
