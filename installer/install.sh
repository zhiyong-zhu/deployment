#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 sudo ./install.sh" >&2
  exit 1
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VERSION=$(cat "$SCRIPT_DIR/VERSION")

install -d -m 0755 /etc/cict-installer /var/cache/cict-installer /var/log/cict-installer
install -m 0755 "$SCRIPT_DIR/cict-installer-linux-amd64" /usr/local/bin/cict-installer
if [ ! -f /etc/cict-installer/config.json ]; then
  install -m 0600 "$SCRIPT_DIR/config.example.json" /etc/cict-installer/config.json
  echo "已创建 /etc/cict-installer/config.json，请先填写密码和发布文件路径。"
else
  echo "保留现有 /etc/cict-installer/config.json"
fi
chown root:root /usr/local/bin/cict-installer /etc/cict-installer /var/cache/cict-installer /var/log/cict-installer
chmod 0700 /var/cache/cict-installer /var/log/cict-installer

echo "CICT Ubuntu Installer $VERSION 已安装。"
echo "运行: sudo cict-installer --execute"
echo "页面: http://127.0.0.1:8787"
