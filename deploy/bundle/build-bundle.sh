#!/usr/bin/env bash
# 在联网的 Ubuntu（与目标机同架构、同系统版本）上构建 CICT 离线安装包。
# 产物：cict-offline-bundle-<tag>.tar.gz + 同名 .sha256 校验文件。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

APP_JAR=""
FRONTEND=""
TAG=""
OUT_DIR="$SCRIPT_DIR/dist"
INCLUDE_ENGINE=1

# 与 docker-compose.yml 保持一致的官方镜像清单
OFFICIAL_IMAGES=(
  "mariadb:11.4"
  "redis:7.4.10"
  "rabbitmq:4.3.4-management"
  "nginx:1.28.3"
  "eclipse-temurin:17-jre-jammy"
  "rustfs/rustfs:1.0.0-rc.2"
)

usage() {
  cat <<'USAGE'
用法: build-bundle.sh --app-jar <jar路径> --frontend <zip或目录> --tag <版本号> [选项]

必选参数:
  --app-jar PATH      后端 JAR 包（会被复制进 bundle，固定命名 app.jar）
  --frontend PATH     前端产物：包含 index.html 的 zip 或目录
  --tag VERSION       后端镜像版本标签，如 2.1.2；每次发布必须唯一

可选参数:
  --out DIR           输出目录（默认 ./dist）
  --skip-engine       不打包 Docker Engine 离线 deb 包（目标机已装 Docker 时使用）
  -h, --help          显示本帮助
USAGE
}

log() { printf '[build-bundle] %s\n' "$*"; }
die() { log "错误: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --app-jar) APP_JAR="${2:-}"; shift 2 ;;
    --frontend) FRONTEND="${2:-}"; shift 2 ;;
    --tag) TAG="${2:-}"; shift 2 ;;
    --out) OUT_DIR="${2:-}"; shift 2 ;;
    --skip-engine) INCLUDE_ENGINE=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1（见 --help）" ;;
  esac
done

[[ -n "$APP_JAR" && -n "$FRONTEND" && -n "$TAG" ]] || { usage >&2; die "--app-jar、--frontend、--tag 均为必填"; }
[[ -f "$APP_JAR" ]] || die "JAR 文件不存在: $APP_JAR"
[[ -e "$FRONTEND" ]] || die "前端产物不存在: $FRONTEND"
[[ "$TAG" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "非法版本标签: $TAG（只允许字母数字、点、下划线、连字符）"

command -v docker >/dev/null 2>&1 || die "本机未安装 Docker"
docker info >/dev/null 2>&1 || die "Docker 守护进程未运行"
docker compose version >/dev/null 2>&1 || die "缺少 docker compose 插件"

if [[ "$FRONTEND" == *.zip ]]; then
  command -v unzip >/dev/null 2>&1 || die "解压前端 zip 需要 unzip（sudo apt install -y unzip）"
fi

if [[ "$INCLUDE_ENGINE" -eq 1 ]]; then
  command -v dpkg-scanpackages >/dev/null 2>&1 \
    || die "打包 Docker Engine 需要 dpkg-dev（sudo apt install -y dpkg-dev）"
  apt-cache show docker-ce >/dev/null 2>&1 \
    || die "apt 中找不到 docker-ce，请先按部署指南 3.5 配置 Docker 官方 apt 源，或使用 --skip-engine"
fi

log "输出目录: $OUT_DIR"
STAGE_ROOT="$OUT_DIR/staging"
BUNDLE_DIR="$STAGE_ROOT/cict-offline-bundle"
rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_DIR"/{images,docker-offline,project/cict,project/nginx/conf.d,project/mariadb}

# ===== 1. 组装项目文件 =====
log "组装项目文件..."
install -m 0644 "$DEPLOY_DIR/docker-compose.yml" "$BUNDLE_DIR/project/"
sed "s/__CICT_IMAGE_TAG__/$TAG/" "$DEPLOY_DIR/.env.example" > "$BUNDLE_DIR/project/.env.example"
install -m 0644 "$DEPLOY_DIR/cict/Dockerfile" "$BUNDLE_DIR/project/cict/"
install -m 0644 "$DEPLOY_DIR/cict/application.properties" "$BUNDLE_DIR/project/cict/"
install -m 0644 "$DEPLOY_DIR/nginx/nginx.conf" "$BUNDLE_DIR/project/nginx/"
install -m 0644 "$DEPLOY_DIR/nginx/conf.d/cict.conf" "$BUNDLE_DIR/project/nginx/conf.d/"
install -m 0644 "$DEPLOY_DIR/mariadb/server.cnf" "$BUNDLE_DIR/project/mariadb/"
if [[ -d "$DEPLOY_DIR/db/init" ]]; then
  mkdir -p "$BUNDLE_DIR/project/db/init"
  cp -a "$DEPLOY_DIR/db/init/." "$BUNDLE_DIR/project/db/init/"
fi
install -m 0644 "$APP_JAR" "$BUNDLE_DIR/project/cict/app.jar"

FRONTEND_TMP=""
cleanup() { [[ -n "$FRONTEND_TMP" ]] && rm -rf "$FRONTEND_TMP"; }
trap cleanup EXIT
if [[ "$FRONTEND" == *.zip ]]; then
  FRONTEND_TMP="$(mktemp -d)"
  unzip -q "$FRONTEND" -d "$FRONTEND_TMP"
else
  FRONTEND_TMP="$FRONTEND"
fi
if [[ -f "$FRONTEND_TMP/index.html" ]]; then
  FRONTEND_SOURCE="$FRONTEND_TMP"
elif [[ -f "$FRONTEND_TMP/dist/index.html" ]]; then
  FRONTEND_SOURCE="$FRONTEND_TMP/dist"
else
  die "前端产物中找不到 index.html: $FRONTEND"
fi
cp -a "$FRONTEND_SOURCE/." "$BUNDLE_DIR/project/nginx/html/"
chmod -R a+rX "$BUNDLE_DIR/project/nginx/html"
FRONTEND_TMP=""

install -m 0755 "$SCRIPT_DIR/install.sh" "$BUNDLE_DIR/install.sh"
if [[ -f "$DEPLOY_DIR/gui/deploy-gui.py" ]]; then
  install -m 0755 "$DEPLOY_DIR/gui/deploy-gui.py" "$BUNDLE_DIR/deploy-gui.py"
fi
printf '%s\n' "$TAG" > "$BUNDLE_DIR/bundle-tag"

# ===== 2. 构建后端镜像 =====
CICT_IMAGE="cict/backend:$TAG"
log "构建后端镜像 $CICT_IMAGE ..."
docker build -t "$CICT_IMAGE" "$BUNDLE_DIR/project/cict"

# ===== 3. 拉取并导出全部镜像 =====
log "拉取并导出官方镜像..."
for image in "${OFFICIAL_IMAGES[@]}"; do
  docker pull "$image"
  filename="$(printf '%s' "$image" | tr '/:' '__').tar"
  docker save -o "$BUNDLE_DIR/images/$filename" "$image"
done
docker save -o "$BUNDLE_DIR/images/cict-backend.tar" "$CICT_IMAGE"
(cd "$BUNDLE_DIR/images" && sha256sum ./*.tar > images.sha256)
log "镜像导出完成: $(ls "$BUNDLE_DIR/images"/*.tar | wc -l | tr -d ' ') 个 tar"

# ===== 4. Docker Engine 离线 deb 包 =====
if [[ "$INCLUDE_ENGINE" -eq 1 ]]; then
  log "下载 Docker Engine 离线 deb 包..."
  (
    cd "$BUNDLE_DIR/docker-offline"
    apt-get download "$(apt-cache depends --recurse --no-recommends --no-suggests \
      --no-conflicts --no-breaks --no-replaces --no-enhances \
      docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
      | grep '^\w' | sort -u)"
    dpkg-scanpackages . /dev/null | gzip -9c > Packages.gz
    sha256sum ./*.deb Packages.gz > docker-packages.sha256
  )
else
  log "跳过 Docker Engine 打包（--skip-engine）"
  rmdir "$BUNDLE_DIR/docker-offline"
fi

# ===== 5. 生成清单并打包 =====
log "生成校验清单..."
(
  cd "$BUNDLE_DIR"
  find . -type f ! -name manifest.sha256 -print0 | LC_ALL=C sort -z | xargs -0 sha256sum > manifest.sha256
)

log "压缩最终安装包..."
BUNDLE_TGZ="$OUT_DIR/cict-offline-bundle-$TAG.tar.gz"
(cd "$STAGE_ROOT" && tar -czf "$BUNDLE_TGZ" cict-offline-bundle)
(cd "$OUT_DIR" && sha256sum "$(basename "$BUNDLE_TGZ")" > "$(basename "$BUNDLE_TGZ")".sha256)
rm -rf "$STAGE_ROOT"

log "完成。产物："
ls -lh "$BUNDLE_TGZ" "$BUNDLE_TGZ.sha256"
log "目标机安装方式：解压后执行  sudo ./install.sh"
