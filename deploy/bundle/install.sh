#!/usr/bin/env bash
# CICT 一键安装脚本，两种模式共用同一文件：
#   离线模式（默认）：在 build-bundle.sh 产物的解压目录内执行，加载离线镜像
#   在线模式（--online）：在仓库 deploy/ 目录下执行，自动安装 Docker、拉取/构建镜像
# 幂等：重复执行保留已有 .env 密码与 data/ 数据，仅同步项目文件并重新拉起服务。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="/data/app/cict-docker"
ONLINE=0
APP_JAR=""
FRONTEND=""
TAG=""
SKIP_ENGINE=0
FORCE_ENV=0

log() { printf '[install] %s\n' "$*"; }
die() { log "错误: $*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
用法（离线，默认）: 在离线安装包解压目录内执行
  sudo ./install.sh [选项]

用法（在线）: 在仓库 deploy/bundle/ 目录下执行
  sudo ./install.sh --online --app-jar <jar> --frontend <zip或目录> --tag <唯一版本号> [选项]

选项:
  --target-dir DIR   部署目录（默认 /data/app/cict-docker）
  --skip-engine     已安装 Docker 时跳过 Docker Engine 安装（离线模式跳过离线包安装，在线模式跳过官方源安装）
  --force-env       丢弃现有 .env 并重新生成随机密码（会与已有数据卷中的旧密码不一致，慎用）
  --online          在线模式，需同时提供 --app-jar / --frontend / --tag
  -h, --help        显示本帮助
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target-dir) TARGET_DIR="${2:-}"; shift 2 ;;
    --skip-engine) SKIP_ENGINE=1; shift ;;
    --force-env) FORCE_ENV=1; shift ;;
    --online) ONLINE=1; shift ;;
    --app-jar) APP_JAR="${2:-}"; shift 2 ;;
    --frontend) FRONTEND="${2:-}"; shift 2 ;;
    --tag) TAG="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "未知参数: $1（见 --help）" ;;
  esac
done

# ===== 模式确定与输入校验 =====
if [[ "$ONLINE" -eq 1 ]]; then
  [[ -n "$APP_JAR" && -n "$FRONTEND" && -n "$TAG" ]] || { usage >&2; die "在线模式必须提供 --app-jar、--frontend、--tag"; }
  [[ -f "$APP_JAR" ]] || die "JAR 文件不存在: $APP_JAR"
  [[ -e "$FRONTEND" ]] || die "前端产物不存在: $FRONTEND"
  [[ "$TAG" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "非法版本标签: $TAG"
  if [[ "$FRONTEND" == *.zip ]] && ! command -v unzip >/dev/null 2>&1; then
    die "解压前端 zip 需要 unzip（sudo apt install -y unzip）"
  fi
  DEPLOY_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
  [[ -f "$DEPLOY_DIR/docker-compose.yml" ]] || die "未找到部署文件目录（应在 deploy/bundle/ 下执行）"
  [[ -f "$DEPLOY_DIR/.env.example" ]] || die "缺少 $DEPLOY_DIR/.env.example"
else
  [[ -f "$SCRIPT_DIR/manifest.sha256" ]] || die "当前目录不是完整的离线安装包（缺少 manifest.sha256）；在线部署请使用 --online 模式"
fi

[[ "$(id -u)" -eq 0 ]] || die "请使用 sudo 运行（需要安装 Docker Engine 并写入 /data）"

# ===== 0. 环境预检 =====
. /etc/os-release
[[ "${ID:-}" = "ubuntu" && "${VERSION_ID:-}" = "24.04" ]] \
  || log "警告: 本安装包针对 Ubuntu 24.04 验证，当前系统 ${PRETTY_NAME:-未知}，继续执行但不承诺兼容"
[[ "$(uname -m)" = "x86_64" ]] || log "警告: 本安装包为 amd64，当前架构 $(uname -m)"

AVAIL_GB="$(( $( { df -Pk /data 2>/dev/null || df -Pk /; } | awk 'NR==2{print $4}') / 1024 / 1024 ))"
[[ "${AVAIL_GB:-0}" -ge 30 ]] || log "警告: /data 可用空间约 ${AVAIL_GB:-?}GB，低于建议值 30GB"

mkdir -p "$TARGET_DIR"
REPORT="$TARGET_DIR/install-report.txt"
exec > >(tee -a "$REPORT") 2>&1
log "安装模式: $([[ "$ONLINE" -eq 1 ]] && echo 在线 || echo 离线)；安装报告: $REPORT"

# ===== 1. Docker Engine =====
install_engine_online() {
  log "从 Docker 官方 apt 源安装 Docker Engine..."
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
$(. /etc/os-release && echo "$VERSION_CODENAME") stable" > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
}

install_engine_offline() {
  [[ -f "$SCRIPT_DIR/docker-offline/docker-packages.sha256" ]] \
    || die "bundle 未包含 Docker Engine 离线包，请去掉 --skip-engine 重新制包或手工安装 Docker"
  (cd "$SCRIPT_DIR/docker-offline" && sha256sum -c docker-packages.sha256 --quiet) \
    || die "Docker 离线包校验失败"
  echo "deb [trusted=yes] file:$SCRIPT_DIR/docker-offline ./" > /etc/apt/sources.list.d/docker-offline.list
  apt-get update -o Acquire::AllowInsecureRepositories=true -qq
  apt-get install -y --no-download docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin
  rm -f /etc/apt/sources.list.d/docker-offline.list
  systemctl enable --now docker
}

if ! command -v docker >/dev/null 2>&1; then
  [[ "$SKIP_ENGINE" -eq 1 ]] && die "未安装 Docker 且指定了 --skip-engine"
  if [[ "$ONLINE" -eq 1 ]]; then install_engine_online; else install_engine_offline; fi
else
  log "Docker 已安装: $(docker --version)"
  systemctl is-active --quiet docker || systemctl start docker
fi
docker compose version >/dev/null 2>&1 || die "缺少 docker compose 插件"

# Docker 容器日志轮转（已存在配置则不覆盖）
if [[ ! -f /etc/docker/daemon.json ]]; then
  log "配置 Docker 日志轮转..."
  cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "5"
  }
}
EOF
  systemctl restart docker
else
  log "已存在 /etc/docker/daemon.json，跳过日志轮转配置"
fi

# ===== 2. 离线模式：校验并加载镜像；在线模式：校验安装包 =====
if [[ "$ONLINE" -eq 0 ]]; then
  log "校验安装包完整性..."
  (cd "$SCRIPT_DIR" && sha256sum -c manifest.sha256 --quiet) \
    || die "安装包校验失败，请重新传输并核对 .sha256 文件"
  log "校验并加载离线镜像..."
  (cd "$SCRIPT_DIR/images" && sha256sum -c images.sha256 --quiet) || die "镜像校验失败"
  for image_tar in "$SCRIPT_DIR"/images/*.tar; do
    log "加载 $(basename "$image_tar") ..."
    docker load -i "$image_tar"
  done
fi

# ===== 3. 项目文件来源（两种模式在此统一） =====
if [[ "$ONLINE" -eq 1 ]]; then
  SRC="$DEPLOY_DIR"
  SRC_APP_JAR="$APP_JAR"
  # 前端产物解压/定位
  FRONTEND_TMP="$(mktemp -d)"
  if [[ "$FRONTEND" == *.zip ]]; then
    unzip -q "$FRONTEND" -d "$FRONTEND_TMP"
  else
    FRONTEND_TMP="$FRONTEND"
  fi
  if [[ -f "$FRONTEND_TMP/index.html" ]]; then
    SRC_HTML="$FRONTEND_TMP"
  elif [[ -f "$FRONTEND_TMP/dist/index.html" ]]; then
    SRC_HTML="$FRONTEND_TMP/dist"
  else
    die "前端产物中找不到 index.html: $FRONTEND"
  fi
  BUNDLE_TAG="$TAG"
else
  SRC="$SCRIPT_DIR/project"
  SRC_APP_JAR="$SRC/cict/app.jar"
  SRC_HTML="$SRC/nginx/html"
  BUNDLE_TAG="$(cat "$SCRIPT_DIR/bundle-tag")"
fi

log "同步项目文件到 $TARGET_DIR ..."
install -m 0644 "$SRC/docker-compose.yml" "$TARGET_DIR/"
install -m 0644 "$SRC/.env.example" "$TARGET_DIR/"
install -m 0644 "$SRC/cict/Dockerfile" "$TARGET_DIR/cict/"
install -m 0644 "$SRC/cict/application.properties" "$TARGET_DIR/cict/"
install -m 0644 "$SRC_APP_JAR" "$TARGET_DIR/cict/app.jar.new"
mv -f "$TARGET_DIR/cict/app.jar.new" "$TARGET_DIR/cict/app.jar"
install -m 0644 "$SRC/nginx/nginx.conf" "$TARGET_DIR/nginx/"
install -m 0644 "$SRC/nginx/conf.d/cict.conf" "$TARGET_DIR/nginx/conf.d/"
rm -rf "$TARGET_DIR/nginx/html.new"
cp -a "$SRC_HTML" "$TARGET_DIR/nginx/html.new"
rm -rf "$TARGET_DIR/nginx/html"
mv "$TARGET_DIR/nginx/html.new" "$TARGET_DIR/nginx/html"
if [[ "$ONLINE" -eq 1 && -n "${FRONTEND_TMP:-}" && "$FRONTEND_TMP" != "$FRONTEND" ]]; then
  rm -rf "$FRONTEND_TMP"
fi
install -m 0644 "$SRC/mariadb/server.cnf" "$TARGET_DIR/mariadb/"

mkdir -p "$TARGET_DIR"/data/{mariadb,redis,rabbitmq,rustfs} \
         "$TARGET_DIR"/logs/{cict,nginx,rustfs}

# ===== 4. .env 密码管理 =====
if [[ ! -f "$TARGET_DIR/.env" || "$FORCE_ENV" -eq 1 ]]; then
  command -v openssl >/dev/null 2>&1 || die "生成随机密码需要 openssl"
  log "生成随机密码并写入 .env ..."
  umask 077
  : > "$TARGET_DIR/.env"
  while IFS= read -r line; do
    if [[ "$line" =~ __RANDOM_HEX_([0-9]+)__ ]]; then
      n="${BASH_REMATCH[1]}"
      line="${line//__RANDOM_HEX_${n}__/$(openssl rand -hex "$n")}"
    fi
    line="${line//__CICT_IMAGE_TAG__/$BUNDLE_TAG}"
    printf '%s\n' "$line" >> "$TARGET_DIR/.env"
  done < "$SRC/.env.example"
  chmod 600 "$TARGET_DIR/.env"
  log "密码仅保存在 $TARGET_DIR/.env（权限 600），请立即备份到密码管理系统"
else
  log "保留现有 .env，同步镜像版本标签为 $BUNDLE_TAG"
  sed -i "s/^CICT_IMAGE_TAG=.*/CICT_IMAGE_TAG=$BUNDLE_TAG/" "$TARGET_DIR/.env"
fi

# ===== 5. 目录属主 =====
RUN_USER="${SUDO_USER:-root}"
RUN_GROUP="$(id -gn "$RUN_USER")"
chown -R "$RUN_USER:$RUN_GROUP" "$TARGET_DIR"
# Dockerfile 中后端用户固定为 UID/GID 10001
chown -R 10001:10001 "$TARGET_DIR/logs/cict"

# ===== 6. 启动服务 =====
cd "$TARGET_DIR"
log "校验 Compose 配置与 Nginx 语法..."
docker compose config --quiet
docker compose run --rm --no-deps nginx nginx -t

if [[ "$ONLINE" -eq 1 ]]; then
  log "构建后端镜像 cict/backend:$BUNDLE_TAG（官方镜像由 compose 自动拉取）..."
  docker compose build cict
fi

log "启动全部服务..."
docker compose up -d

log "等待服务就绪..."
READY=0
for _ in $(seq 1 60); do
  ok=1
  for name in cict-mariadb cict-redis cict-rabbitmq cict-rustfs cict-app cict-nginx; do
    [[ "$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo missing)" = "running" ]] || ok=0
  done
  for name in cict-mariadb cict-redis cict-rabbitmq; do
    [[ "$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null || echo none)" = "healthy" ]] || ok=0
  done
  if [[ "$ok" -eq 1 ]]; then READY=1; break; fi
  sleep 5
done
docker compose ps
[[ "$READY" -eq 1 ]] || die "部分服务未在 5 分钟内就绪，请查看上方状态与 docker compose logs 排障"

# ===== 7. 创建 RabbitMQ 管理账号（幂等） =====
log "配置 RabbitMQ 管理账号..."
admin_ok=0
for _ in $(seq 1 6); do
  if docker compose exec -T rabbitmq sh -c '
    rabbitmqctl set_user_tags "$RABBITMQ_DEFAULT_USER" &&
    if ! rabbitmqctl list_users --silent | awk "{print \$1}" | grep -qx cict_admin; then
      rabbitmqctl add_user cict_admin "$RABBITMQ_ADMIN_PASSWORD"
    fi &&
    rabbitmqctl set_user_tags cict_admin administrator &&
    rabbitmqctl set_permissions -p / cict_admin ".*" ".*" ".*"
  '; then admin_ok=1; break; fi
  sleep 10
done
[[ "$admin_ok" -eq 1 ]] || log "警告: RabbitMQ 管理账号创建未完成，请按部署指南 3.9 手工执行"

# ===== 8. 安装报告 =====
SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
NGINX_PORT="$(grep -E '^NGINX_PORT=' "$TARGET_DIR/.env" | cut -d= -f2)"
cat <<EOF

==================== CICT 安装完成 ====================
访问地址      : http://${SERVER_IP:-<服务器IP>}:${NGINX_PORT:-80}
密码文件      : $TARGET_DIR/.env （权限 600，含全部随机密码，请立即备份）
后端镜像版本  : $BUNDLE_TAG

后续步骤:
  1. 创建 RustFS 存储桶 pike:
     ssh -L 9001:127.0.0.1:9001 <用户>@${SERVER_IP:-<服务器IP>}
     浏览器访问 http://127.0.0.1:9001，使用 .env 中的 RUSTFS_ACCESS_KEY/SECRET_KEY 登录后创建
  2. 管理端口默认只绑定 127.0.0.1，远程访问一律走 SSH 隧道（RabbitMQ 15672、RustFS 9000/9001）
  3. 日常管理: cd $TARGET_DIR && docker compose ps / logs -f <服务>
  4. 后续升级: 在线环境重跑本命令并换新 tag；离线环境用新包重跑。详见部署指南附录 A.3

=======================================================
EOF
log "安装完成。"
