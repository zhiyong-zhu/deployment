#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUTPUT_DIR="$SCRIPT_DIR/dist"
mkdir -p "$OUTPUT_DIR"

python3 -m pip install --user --upgrade pyinstaller==6.13.0
cd "$SCRIPT_DIR"
python3 -m PyInstaller --clean --noconfirm --onefile \
  --name cict-installer \
  --add-data "static:static" \
  app.py
install -m 0755 dist/cict-installer "$OUTPUT_DIR/cict-installer-linux-amd64"
file "$OUTPUT_DIR/cict-installer-linux-amd64"
