#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUTPUT_DIR="$SCRIPT_DIR/dist"
mkdir -p "$OUTPUT_DIR"

if ! command -v docker >/dev/null 2>&1; then
  echo "需要 Docker 构建 Linux 二进制" >&2
  exit 1
fi

docker run --rm --platform linux/amd64 \
  -v "$SCRIPT_DIR:/src:ro" \
  -v "$OUTPUT_DIR:/out" \
  python:3.12-slim \
  sh -c 'set -eu
    work=/tmp/cict-installer-build
    mkdir -p "$work"
    cp -a /src/. "$work/"
    cd "$work"
    pip install --no-cache-dir pyinstaller==6.13.0 >/dev/null
    pyinstaller --clean --noconfirm --onefile --name cict-installer --add-data "static:static" app.py >/dev/null
    cp dist/cict-installer /out/cict-installer-linux-amd64
    chmod 0755 /out/cict-installer-linux-amd64
  '

file "$OUTPUT_DIR/cict-installer-linux-amd64"
