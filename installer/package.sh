#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VERSION=$(cat "$SCRIPT_DIR/VERSION")
OUTPUT_DIR="$SCRIPT_DIR/dist"
PACKAGE_NAME="cict-ubuntu-installer-$VERSION"
mkdir -p "$OUTPUT_DIR"

if [ ! -x "$OUTPUT_DIR/cict-installer-linux-amd64" ]; then
  echo "缺少 Linux 二进制，请先执行 ./build-binary.sh" >&2
  exit 1
fi

tar -czf "$OUTPUT_DIR/$PACKAGE_NAME.tar.gz" \
  -C "$OUTPUT_DIR" cict-installer-linux-amd64 \
  -C "$SCRIPT_DIR" install.sh uninstall.sh config.example.json VERSION README.md

sha256sum "$OUTPUT_DIR/$PACKAGE_NAME.tar.gz" > "$OUTPUT_DIR/$PACKAGE_NAME.tar.gz.sha256"
printf 'Created %s\n' "$OUTPUT_DIR/$PACKAGE_NAME.tar.gz"
cat "$OUTPUT_DIR/$PACKAGE_NAME.tar.gz.sha256"
