#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUTPUT_DIR="$SCRIPT_DIR/dist"
VENV_DIR="$SCRIPT_DIR/.build-venv"
PYINSTALLER_VERSION=6.13.0
PRIMARY_INDEX=${CICT_PYPI_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}
FALLBACK_INDEX=https://pypi.org/simple
PIP_TIMEOUT=${CICT_PIP_TIMEOUT:-120}
PIP_RETRIES=${CICT_PIP_RETRIES:-8}
mkdir -p "$OUTPUT_DIR"

if ! python3 -m venv "$VENV_DIR"; then
  echo "无法创建构建虚拟环境。请先执行: sudo apt-get install -y python3-venv" >&2
  exit 1
fi

if ! "$VENV_DIR/bin/python" -c "import PyInstaller; raise SystemExit(PyInstaller.__version__ != '$PYINSTALLER_VERSION')" 2>/dev/null; then
  echo "安装构建依赖 PyInstaller $PYINSTALLER_VERSION（镜像: $PRIMARY_INDEX）"
  if ! "$VENV_DIR/bin/python" -m pip install \
    --disable-pip-version-check \
    --timeout "$PIP_TIMEOUT" \
    --retries "$PIP_RETRIES" \
    --index-url "$PRIMARY_INDEX" \
    "pyinstaller==$PYINSTALLER_VERSION"; then
    echo "首选镜像失败，回退到 $FALLBACK_INDEX" >&2
    "$VENV_DIR/bin/python" -m pip install \
      --disable-pip-version-check \
      --timeout "$PIP_TIMEOUT" \
      --retries "$PIP_RETRIES" \
      --index-url "$FALLBACK_INDEX" \
      "pyinstaller==$PYINSTALLER_VERSION"
  fi
fi
cd "$SCRIPT_DIR"
"$VENV_DIR/bin/python" -m PyInstaller --clean --noconfirm --onefile \
  --name cict-installer \
  --add-data "static:static" \
  app.py
install -m 0755 dist/cict-installer "$OUTPUT_DIR/cict-installer-linux-amd64"
file "$OUTPUT_DIR/cict-installer-linux-amd64"
