#!/usr/bin/env bash
# 生成单文件便携安装器 cict-installer.run：
# 把 deploy/ 全部部署文件内嵌进一个自解压可执行脚本，部署现场只需携带
#   cict-installer.run + 后端 JAR + 前端 zip
# 即可在线部署（离线部署仍使用 build-bundle.sh 产出的完整离线包，镜像体积不适合内嵌）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OUT_DIR="$SCRIPT_DIR/dist"
OUT="$OUT_DIR/cict-installer.run"

mkdir -p "$OUT_DIR"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

tar -czf "$STAGE/payload.tar.gz" -C "$DEPLOY_DIR" \
  --exclude='bundle/dist' --exclude='__pycache__' --exclude='.DS_Store' .

# 头部先以占位符渲染，统计行数后回填（只改内容不改行数，tail -n 偏移恒准）。
# 提取用 tail -n 而非 awk/sed：二进制负载必须按字节切分，文本工具在部分 locale 下会损坏字节。
HEADER="$STAGE/header.sh"
cat > "$HEADER" <<'HDR'
#!/usr/bin/env bash
# CICT 便携安装器（单文件自解压，内嵌全部部署文件与安装脚本）
#
# Web 向导: sudo ./cict-installer.run            （不带参数即启动，浏览器 http://127.0.0.1:8788）
# 命令行  : sudo ./cict-installer.run --online --app-jar <jar> --frontend <zip或目录> --tag <唯一版本号>
# 其余参数与 deploy/bundle/install.sh 完全一致（--help 查看）
# 离线部署请使用 build-bundle.sh 产出的完整离线安装包（含镜像与 Engine 离线包）。
set -euo pipefail
PAYLOAD_START=__HCOUNT__
EXTRACT_DIR="$(mktemp -d)"
trap 'rm -rf "$EXTRACT_DIR"' EXIT
tail -n +"$((PAYLOAD_START + 1))" "$0" | tar -xzf - -C "$EXTRACT_DIR"
if [[ $# -eq 0 || "${1:-}" = "--gui" ]]; then
  echo "启动 Web 部署向导: http://127.0.0.1:8788"
  echo "远程操作先建隧道: ssh -L 8788:127.0.0.1:8788 <用户>@<服务器IP>"
  echo "当前用户: $(id -un)（执行安装需要 sudo 启动本程序）"
  ( sleep 1
    xdg-open http://127.0.0.1:8788 >/dev/null 2>&1 \
      || open http://127.0.0.1:8788 >/dev/null 2>&1 \
      || true ) &
  exec python3 "$EXTRACT_DIR/gui/deploy-gui.py"
fi
exec bash "$EXTRACT_DIR/bundle/install.sh" "$@"
HDR

HEADER_LINES="$(wc -l < "$HEADER" | tr -d ' ')"
sed -i '' "s/__HCOUNT__/$HEADER_LINES/" "$HEADER" 2>/dev/null || sed -i "s/__HCOUNT__/$HEADER_LINES/" "$HEADER"

cat "$HEADER" "$STAGE/payload.tar.gz" > "$OUT"
chmod 0755 "$OUT"
(cd "$OUT_DIR" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT")".sha256)

echo "便携安装器已生成: $OUT ($(du -h "$OUT" | cut -f1))"
ls -lh "$OUT" "$OUT.sha256"
