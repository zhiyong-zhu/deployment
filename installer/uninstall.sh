#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 sudo ./uninstall.sh" >&2
  exit 1
fi

rm -f /usr/local/bin/cict-installer
echo "CICT Ubuntu Installer 已移除。"
echo "业务服务、安装器配置和数据目录未删除。"
