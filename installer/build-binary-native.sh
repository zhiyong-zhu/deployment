#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUTPUT_DIR="$SCRIPT_DIR/dist"
VENV_DIR="$SCRIPT_DIR/.build-venv"
mkdir -p "$OUTPUT_DIR"

if ! python3 -m venv "$VENV_DIR"; then
  echo "无法创建构建虚拟环境。请先执行: sudo apt-get install -y python3-venv" >&2
  exit 1
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
"$VENV_DIR/bin/python" -m pip install --upgrade pyinstaller >/dev/null
cd "$SCRIPT_DIR"
"$VENV_DIR/bin/python" -m PyInstaller --clean --noconfirm --onefile \
  --name cict-installer \
  --add-data "static:static" \
  app.py
install -m 0755 dist/cict-installer "$OUTPUT_DIR/cict-installer-linux-amd64"
file "$OUTPUT_DIR/cict-installer-linux-amd64"
