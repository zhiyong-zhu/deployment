#!/usr/bin/env bash
# CICT 一键安装脚本，两种模式共用同一文件：
#   离线模式（默认）：在 build-bundle.sh 产物的解压目录内执行，加载离线镜像
#   在线模式（--online）：在仓库 deploy/ 目录下执行，自动安装 Docker、拉取/构建镜像
# 幂等：重复执行保留已有 .env 密码与 data/ 数据，仅同步项目文件并重新拉起服务。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="/data/app/cict-docker"
# 上次部署记录（安装成功后写入，升级时不带 --target-dir 则自动沿用记录的路径）
STATE_FILE="/etc/cict-docker/deploy.env"
TARGET_DIR_EXPLICIT=0
ONLINE=0
APP_JAR=""
FRONTEND=""
TAG=""
SKIP_ENGINE=0
FORCE_ENV=0
DOCKER_APT_MIRROR=""
REGISTRY_MIRRORS=""

log() { printf '[install] %s\n' "$*"; }
die() { log "错误: $*" >&2; exit 1; }

usage() {
  cat <<'USAGE'
用法（离线，默认）: 在离线安装包解压目录内执行
  sudo ./install.sh [选项]

用法（在线）: 在仓库 deploy/bundle/ 目录下执行
  sudo ./install.sh --online --app-jar <jar> --frontend <zip或目录> --tag <唯一版本号> [选项]

选项:
  --target-dir DIR   部署目录（默认 /data/app/cict-docker；未显式指定时自动沿用
                     /etc/cict-docker/deploy.env 记录的上次部署路径）
  --skip-engine     已安装 Docker 时跳过 Docker Engine 安装（离线模式跳过离线包安装，在线模式跳过官方源安装）
  --force-env       丢弃现有 .env 并重新生成随机密码（会与已有数据卷中的旧密码不一致，慎用）
  --online          在线模式，需同时提供 --app-jar / --frontend / --tag
  --docker-apt-mirror BASE  指定 Docker apt 源基地址（如 https://mirrors.aliyun.com/docker-ce）。
                           默认自动探测：官方 → 阿里云 → 中科大 → 清华，用第一个可达的源
  --registry-mirror URL    Docker 镜像加速器，可多次传入。默认自动探测：Docker Hub 直连可用则
                           不配置，否则按 1ms → DaoCloud → 1Panel → rat.dev 顺序选第一个可用加速器
  -h, --help        显示本帮助
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target-dir) TARGET_DIR="${2:-}"; TARGET_DIR_EXPLICIT=1; shift 2 ;;
    --skip-engine) SKIP_ENGINE=1; shift ;;
    --force-env) FORCE_ENV=1; shift ;;
    --online) ONLINE=1; shift ;;
    --app-jar) APP_JAR="${2:-}"; shift 2 ;;
    --frontend) FRONTEND="${2:-}"; shift 2 ;;
    --tag) TAG="${2:-}"; shift 2 ;;
    --docker-apt-mirror) DOCKER_APT_MIRROR="${2:-}"; shift 2 ;;
    --registry-mirror) REGISTRY_MIRRORS="${REGISTRY_MIRRORS:+$REGISTRY_MIRRORS,}${2:-}"; shift 2 ;;
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

# 复用上次部署路径：升级时忘带 --target-dir 会落到默认路径全新空部署（与旧数据分离），
# 因此未显式指定时沿用 STATE_FILE 记录的 TARGET_DIR；仅当记录目录仍有部署文件时复用，
# 目录已被清空则回退默认路径
if [[ "$TARGET_DIR_EXPLICIT" -ne 1 && -f "$STATE_FILE" ]]; then
  LAST_DIR="$(grep -E '^TARGET_DIR=' "$STATE_FILE" | head -1 | cut -d= -f2- || true)"
  if [[ -n "$LAST_DIR" && ( -f "$LAST_DIR/docker-compose.yml" || -f "$LAST_DIR/.env" ) ]]; then
    TARGET_DIR="$LAST_DIR"
    log "沿用上次部署路径 ${TARGET_DIR}（记录于 ${STATE_FILE}），如需更换请显式传 --target-dir"
  elif [[ -n "$LAST_DIR" ]]; then
    log "记录中的路径 $LAST_DIR 已无部署文件，本次使用默认路径 $TARGET_DIR"
  fi
fi

mkdir -p "$TARGET_DIR"
REPORT="$TARGET_DIR/install-report.txt"
exec > >(tee -a "$REPORT") 2>&1
log "安装模式: $([[ "$ONLINE" -eq 1 ]] && echo 在线 || echo 离线)；安装报告: $REPORT"

# ===== 1. Docker Engine =====
install_engine_online() {
  log "从 Docker apt 源安装 Docker Engine..."
  # apt 网络操作统一加超时与重试：源连接挂死时 30 秒内失败并重试，而不是无限等待
  local apt_opts=(-o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 -o Acquire::Retries=2)
  apt-get update "${apt_opts[@]}"
  apt-get install -y "${apt_opts[@]}" ca-certificates curl
  install -m 0755 -d /etc/apt/keyrings
  # 仓库路径按发行版区分（ubuntu/debian），codename 取自当前系统
  . /etc/os-release
  DISTRO_ID="${ID:-ubuntu}"
  DISTRO_CODENAME="${VERSION_CODENAME:-}"
  # 国内网络下官方源常被重置或传输挂死：按 官方 → 阿里云 → 中科大 → 清华 顺序尝试，
  # 以"索引下载成功"为选定标准（GPG 探测可达不代表索引/包能正常下载），失败自动换源
  if [[ -n "$DOCKER_APT_MIRROR" ]]; then
    CANDIDATES=("$DOCKER_APT_MIRROR")
  else
    CANDIDATES=(
      "https://download.docker.com"
      "https://mirrors.aliyun.com/docker-ce"
      "https://mirrors.ustc.edu.cn/docker-ce"
      "https://mirrors.tuna.tsinghua.edu.cn/docker-ce"
    )
  fi
  CHOSEN=""
  for base in "${CANDIDATES[@]}"; do
    log "尝试 Docker apt 源: $base（$DISTRO_ID/$DISTRO_CODENAME）"
    if ! curl -fsSL --connect-timeout 8 --retry 1 "${base}/linux/${DISTRO_ID}/gpg" -o /etc/apt/keyrings/docker.asc; then
      log "该源不可达，尝试下一个..."
      continue
    fi
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] $base/linux/$DISTRO_ID \
$DISTRO_CODENAME stable" > /etc/apt/sources.list.d/docker.list
    if apt-get update "${apt_opts[@]}"; then
      CHOSEN="$base"
      break
    fi
    log "该源索引下载失败/超时，尝试下一个..."
  done
  [[ -n "$CHOSEN" ]] || die "所有 Docker apt 源均不可用。请检查网络，或用 --docker-apt-mirror 指定可用的源基地址"
  log "使用 Docker apt 源: $CHOSEN/linux/$DISTRO_ID"
  apt-get install -y "${apt_opts[@]}" docker-ce docker-ce-cli containerd.io \
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
  # 与文档 3.5 一致：把发起安装的用户加入 docker 组（重新登录后免 sudo；docker 组等同 root 权限，仅授予受信任运维用户）
  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]] && ! id -nG "$SUDO_USER" | grep -qw docker; then
    usermod -aG docker "$SUDO_USER"
    log "已将 $SUDO_USER 加入 docker 组（重新登录后 docker 命令免 sudo）"
  fi
else
  log "Docker 已安装: $(docker --version)"
  systemctl is-active --quiet docker || systemctl start docker
fi
docker compose version >/dev/null 2>&1 || die "缺少 docker compose 插件"

# 镜像源探测与测速（仅在线模式）：Docker Hub 直连与各公共加速器逐一探测并测速，
# 打印对比后选用响应最快者写入 REGISTRY_MIRRORS；直连最快则不配置加速器。
# Registry v2 协议端点 /v2/ 返回 200（匿名）或 401（要求认证）都视为存活。
probe_registry_mirrors() {
  if ! command -v curl >/dev/null 2>&1; then
    apt-get install -y -qq curl >/dev/null 2>&1 || { log "警告: 无 curl 且安装失败，跳过镜像源探测"; return; }
  fi
  # 输出 "HTTP状态码 响应总秒数"
  probe_v2() {
    curl -s -o /dev/null -w '%{http_code} %{time_total}' --connect-timeout 6 "$1/v2/" 2>/dev/null || echo "000 0"
  }
  # 对存活源测速两次取较快一次，输出毫秒整数；两次均失败输出空。
  # 注意必须显式 return 0：本函数在赋值语句的命令替换中调用，源不可达时若以
  # 非零退出，set -e 会误杀整个安装脚本
  mirror_latency_ms() {
    local out code t best=""
    for _ in 1 2; do
      out="$(probe_v2 "$1")"
      code="${out%% *}" t="${out##* }"
      [[ "$code" = "200" || "$code" = "401" ]] || continue
      if [[ -z "$best" ]] || awk -v a="$t" -v b="$best" 'BEGIN{exit !(a<b)}'; then best="$t"; fi
    done
    if [[ -n "$best" ]]; then awk -v t="$best" 'BEGIN{printf "%d", t*1000}'; fi
    return 0
  }
  if [[ -n "$REGISTRY_MIRRORS" ]]; then
    local out code
    out="$(probe_v2 "${REGISTRY_MIRRORS%%,*}")"
    code="${out%% *}"
    if [[ "$code" = "200" || "$code" = "401" ]]; then
      log "镜像加速器探测可达: $REGISTRY_MIRRORS"
    else
      log "警告: 指定的镜像加速器探测不可达，仍按指定值配置（内部源可能不支持匿名探测）"
    fi
    return
  fi
  local m ms summary fastest
  local -a rows=()
  ms="$(mirror_latency_ms https://registry-1.docker.io)"
  if [[ -n "$ms" ]]; then
    rows+=("Docker Hub 直连|$ms|")
  else
    log "镜像源检测: Docker Hub 直连不可用"
  fi
  for m in "https://docker.1ms.run" "https://docker.m.daocloud.io" \
           "https://docker.1panel.live" "https://hub.rat.dev"; do
    ms="$(mirror_latency_ms "$m")"
    if [[ -n "$ms" ]]; then
      rows+=("$m|$ms|$m")
    else
      log "镜像源 $m 不可达，跳过"
    fi
  done
  if [[ "${#rows[@]}" -eq 0 ]]; then
    log "警告: 所有镜像源均不可达，后续拉取镜像可能失败；可用 --registry-mirror 指定内部镜像源"
    return
  fi
  summary="$(printf '%s\n' "${rows[@]}" | LC_ALL=C sort -t'|' -k2 -n | awk -F'|' '{printf "%s(%sms), ", $1, $2}')"
  fastest="$(printf '%s\n' "${rows[@]}" | LC_ALL=C sort -t'|' -k2 -n | head -1 | cut -d'|' -f3)"
  log "镜像源测速对比: ${summary%, }"
  if [[ -z "$fastest" ]]; then
    log "镜像源检测: Docker Hub 直连最快，不配置加速器"
  else
    REGISTRY_MIRRORS="$fastest"
    log "镜像源检测: 使用加速器 $fastest（响应最快）"
  fi
}

if [[ "$ONLINE" -eq 1 ]]; then
  probe_registry_mirrors
fi

# Docker 守护进程配置：合并日志轮转与镜像加速器。
# 显式传入 --registry-mirror 时覆盖已有加速器（用于更换失效地址）；未传入时保留现有配置。
DAEMON_UPDATED="$(python3 - "$REGISTRY_MIRRORS" <<'PYEOF'
import json, os, sys
path = "/etc/docker/daemon.json"
cfg = {}
if os.path.exists(path):
    try:
        cfg = json.load(open(path))
    except Exception:
        cfg = {}
changed = False
if "log-driver" not in cfg:
    cfg["log-driver"] = "json-file"
    cfg["log-opts"] = {"max-size": "50m", "max-file": "5"}
    changed = True
mirrors = [m.strip() for m in (sys.argv[1] or "").split(",") if m.strip()]
if mirrors and cfg.get("registry-mirrors") != mirrors:
    cfg["registry-mirrors"] = mirrors
    changed = True
if changed:
    with open(path, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    print("updated")
else:
    print("noop")
PYEOF
)"
if [[ "$DAEMON_UPDATED" = "updated" ]]; then
  log "已更新 /etc/docker/daemon.json（日志轮转${REGISTRY_MIRRORS:+ + 镜像加速器}）"
  systemctl restart docker
else
  log "/etc/docker/daemon.json 无需修改（已包含日志轮转与加速器配置）"
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
mkdir -p "$TARGET_DIR"/cict "$TARGET_DIR"/nginx/conf.d "$TARGET_DIR"/mariadb \
         "$TARGET_DIR"/data/{mariadb,redis,rabbitmq,rustfs} \
         "$TARGET_DIR"/logs/{cict,nginx,rustfs}
install -m 0644 "$SRC/docker-compose.yml" "$TARGET_DIR/"
install -m 0644 "$SRC/.env.example" "$TARGET_DIR/"
install -m 0644 "$SRC/cict/Dockerfile" "$TARGET_DIR/cict/"
install -m 0644 "$SRC/cict/application.properties" "$TARGET_DIR/cict/"
install -m 0644 "$SRC_APP_JAR" "$TARGET_DIR/cict/app.jar.new"
mv -f "$TARGET_DIR/cict/app.jar.new" "$TARGET_DIR/cict/app.jar"
install -m 0644 "$SRC/nginx/nginx.conf" "$TARGET_DIR/nginx/"
install -m 0644 "$SRC/nginx/conf.d/cict.conf" "$TARGET_DIR/nginx/conf.d/"
# 就地同步前端（清空内容后复制，不替换目录本身）：
# 目录 inode 保持不变，运行中容器的 bind mount 才不会指向已删除的孤儿目录（症状: 宿主机文件正常但容器 500）
mkdir -p "$TARGET_DIR/nginx/html"
find "$TARGET_DIR/nginx/html" -mindepth 1 -delete
cp -a "$SRC_HTML/." "$TARGET_DIR/nginx/html/"
# mktemp -d 产生的 700 权限会被 cp -a 带入，容器内 nginx 用户不可读 → try_files 循环 → 500
[[ -f "$TARGET_DIR/nginx/html/index.html" ]] || die "前端目录缺少 index.html，请检查 --frontend 产物"
chmod -R a+rX "$TARGET_DIR/nginx/html"
if [[ "$ONLINE" -eq 1 && -n "${FRONTEND_TMP:-}" && "$FRONTEND_TMP" != "$FRONTEND" ]]; then
  rm -rf "$FRONTEND_TMP"
fi
install -m 0644 "$SRC/mariadb/server.cnf" "$TARGET_DIR/mariadb/"
# 数据库初始化脚本（可选）：放入 deploy/db/init/*.sql，首次初始化数据卷时由 MariaDB 镜像自动执行
if [[ -d "$SRC/db/init" ]]; then
  mkdir -p "$TARGET_DIR/db/init"
  cp -a "$SRC/db/init/." "$TARGET_DIR/db/init/"
fi

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
  # 存量部署补齐 .env.example 新增的变量（保留已有值不动）：
  # 否则新增配置项（如 CICT_PUBLIC_S3_ENDPOINT）永远到不了已有 .env，对应功能静默失效
  local_env_key() { grep -E "^$1=" "$TARGET_DIR/.env" >/dev/null 2>&1 && return 0 || return 1; }
  while IFS= read -r ex_line || [[ -n "$ex_line" ]]; do
    [[ "$ex_line" =~ ^([A-Za-z_][A-Za-z0-9_]*)= ]] || continue
    ex_key="${BASH_REMATCH[1]}"
    if ! local_env_key "$ex_key"; then
      if [[ "$ex_line" =~ __RANDOM_HEX_([0-9]+)__ ]]; then
        n="${BASH_REMATCH[1]}"
        ex_line="${ex_line//__RANDOM_HEX_${n}__/$(openssl rand -hex "$n")}"
      fi
      ex_line="${ex_line//__CICT_IMAGE_TAG__/$BUNDLE_TAG}"
      printf '%s\n' "$ex_line" >> "$TARGET_DIR/.env"
      log "已补齐 .env 缺失变量: $ex_key"
    fi
  done < "$SRC/.env.example"
  chmod 600 "$TARGET_DIR/.env"
fi

# ===== 5. 目录属主 =====
# 只授权项目文件。data/ 下数据目录的属主由容器自管理（MariaDB=mysql 用户、RabbitMQ=rabbitmq 用户），
# 严禁递归 chown 整个部署目录：数据目录被改成部署用户后守护进程报 errno 13
# （症状: show tables 报 Can't read dir of './cict/'，Liquibase 建表全部失败）
RUN_USER="${SUDO_USER:-root}"
RUN_GROUP="$(id -gn "$RUN_USER")"
chown -R "$RUN_USER:$RUN_GROUP" \
  "$TARGET_DIR"/cict "$TARGET_DIR"/nginx "$TARGET_DIR"/mariadb \
  "$TARGET_DIR"/db "$TARGET_DIR"/logs/nginx 2>/dev/null || true
for f in "$TARGET_DIR/.env" "$TARGET_DIR/.env.example" \
         "$TARGET_DIR/docker-compose.yml" "$TARGET_DIR/install-report.txt"; do
  [[ -e "$f" ]] && chown "$RUN_USER:$RUN_GROUP" "$f"
done
# Dockerfile 中后端用户固定为 UID/GID 10001
chown -R 10001:10001 "$TARGET_DIR/logs/cict"
# RustFS 官方镜像以非 root 用户运行（本版本为 UID 10001），数据与日志目录必须允许其写入，
# 否则启动时报 Permission denied 并进入重启循环。优先从镜像 Config.User 读取实际 UID。
RUSTFS_IMAGE="$(grep -E '^    image: *rustfs/rustfs:' "$TARGET_DIR/docker-compose.yml" | awk '{print $2}' | head -1)"
RUSTFS_UID="$(docker image inspect "${RUSTFS_IMAGE:-rustfs/rustfs:1.0.0-rc.2}" --format '{{.Config.User}}' 2>/dev/null || true)"
case "$RUSTFS_UID" in ''|*[!0-9]*) RUSTFS_UID="10001" ;; esac
log "RustFS 容器运行用户 UID: $RUSTFS_UID，授权其数据与日志目录"
chown -R "$RUSTFS_UID:$RUSTFS_UID" "$TARGET_DIR/data/rustfs" "$TARGET_DIR/logs/rustfs"

# ===== 6. 启动服务 =====
cd "$TARGET_DIR"
log "校验 Compose 配置..."
docker compose config --quiet

if [[ "$ONLINE" -eq 1 ]]; then
  # 镜像拉取直接消费 Docker API 的 NDJSON 进度流。不能靠 docker pull 的输出做进度：
  # 非 TTY 下它只打印层状态变化、不输出逐层百分比（管道过滤器无从聚合，纯静默时慢速
  # 拉取全程无反馈）；且镜像源连接挂死时 docker 会无限等待。改为：每 10 秒聚合输出一行
  # 真实进度；--speed-limit/--speed-time 在持续近零吞吐时中断本次拉取，配合最多 3 次
  # 重试自动逃离卡死的连接（已完成的层在 daemon 内容库有缓存，重试只补缺失部分）。
  PULL_PARSER="$(mktemp /tmp/pull-parser.XXXXXX.py)"
  cat >"$PULL_PARSER" <<'PYEOF'
import json, sys, time
image = sys.argv[1]
last, cur, tot, done = 0.0, {}, {}, False
for line in sys.stdin:
    try:
        ev = json.loads(line)
    except Exception:
        continue
    if "error" in ev:
        print("[install] 拉取 %s 失败: %s" % (image, ev["error"]), flush=True)
        sys.exit(1)
    status, lid = ev.get("status", ""), ev.get("id", "")
    if status == "Downloading":
        d = ev.get("progressDetail") or {}
        if "current" in d and "total" in d:
            cur[lid], tot[lid] = d["current"], d["total"]
            now = time.time()
            if now - last >= 10:
                last = now
                c, t = sum(cur.values()), sum(tot.values())
                print("[install] 拉取进度: %.1fMB / %.1fMB（%.0f%%）" % (c/1e6, t/1e6, (c/t*100 if t else 0)), flush=True)
        continue
    if lid:
        print("%s: %s" % (lid, status), flush=True)
    elif status:
        print(status, flush=True)
    if status.startswith("Status"):
        done = True
print("[install] 拉取完成: %s" % image if done else "[install] 拉取 %s 的进度流提前结束" % image, flush=True)
sys.exit(0 if done else 1)
PYEOF
  docker_pull_progress() {
    local image="$1" name tag attempt
    name="${image%:*}"; tag="${image##*:}"
    [[ "$name" = "$image" ]] && tag="latest"
    for attempt in 1 2 3; do
      [[ "$attempt" -gt 1 ]] && log "重试拉取 $image（第 $attempt/3 次）..."
      if curl -sN --max-time 3600 --speed-limit 64 --speed-time 180 \
           --unix-socket /var/run/docker.sock \
           -X POST "http://localhost/images/create?fromImage=${name}&tag=${tag}" \
           | python3 "$PULL_PARSER" "$image"; then
        return 0
      fi
      sleep 3
    done
    return 1
  }
  BASE_IMAGE="$(sed -n 's/^FROM[[:space:]]\{1,\}\([^[:space:]]\{1,\}\).*/\1/p' "$TARGET_DIR/cict/Dockerfile" | head -1)"
  if [[ -n "$BASE_IMAGE" ]]; then
    log "拉取后端基础镜像 $BASE_IMAGE ..."
    docker_pull_progress "$BASE_IMAGE" \
      || die "拉取基础镜像 $BASE_IMAGE 失败（已重试 3 次），可重跑安装器继续（已下载的层不会重复下载）"
  fi
  # 官方镜像逐个经同一通道拉取（排除本地构建的 cict/backend）
  mapfile -t OFFICIAL_IMAGES < <(docker compose config --images | grep -v '^cict/backend' || true)
  log "拉取官方镜像 ${OFFICIAL_IMAGES[*]-（无）} ..."
  for img in "${OFFICIAL_IMAGES[@]}"; do
    docker_pull_progress "$img" || die "拉取 $img 失败（已重试 3 次），可重跑安装器继续"
  done
  # 构建输出整体落盘避免 buildkit 逐行进度刷屏，另起心跳每 15 秒报告一次最近进度
  BUILD_LOG="$(mktemp /tmp/cict-build.XXXXXX)"
  log "构建后端镜像 cict/backend:$BUNDLE_TAG ..."
  docker compose build cict >"$BUILD_LOG" 2>&1 &
  BUILD_PID=$!
  ELAPSED=0
  while kill -0 "$BUILD_PID" 2>/dev/null; do
    sleep 15
    ELAPSED=$((ELAPSED + 15))
    LAST_PROGRESS="$(grep -oE '[0-9.]+[kMG]?B */ *[0-9.]+[kMG]?B' "$BUILD_LOG" 2>/dev/null | tail -1 || true)"
    if [[ -n "$LAST_PROGRESS" ]]; then
      log "构建进行中 ${ELAPSED}s（最近下载进度: $LAST_PROGRESS）"
    else
      log "构建进行中 ${ELAPSED}s ..."
    fi
  done
  if ! wait "$BUILD_PID"; then
    tail -n 40 "$BUILD_LOG" || true
    die "后端镜像构建失败，完整日志: $BUILD_LOG"
  fi
  log "后端镜像构建完成（约 ${ELAPSED}s，日志: $BUILD_LOG）"
fi

# 离线模式镜像已在第 2 步 docker load 载入，在线模式刚拉取完成
log "校验 Nginx 语法..."
docker compose run --rm --no-deps nginx nginx -t

log "启动全部服务..."
docker compose up -d
# conf.d 是目录 bind mount，其内容更新不会触发 compose 重建容器：
# 显式强制重建 nginx，确保加载的是本轮刚同步的配置而非残留旧配置
docker compose up -d --force-recreate nginx

log "等待服务就绪..."
READY=0
FAIL_FAST=""
for _ in $(seq 1 60); do
  ok=1
  for name in cict-mariadb cict-redis cict-rabbitmq cict-rustfs cict-app cict-nginx; do
    state="$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo missing)"
    [[ "$state" = "running" ]] || ok=0
    # 重启循环 = 确定性崩溃，继续等待没有意义，快速失败
    if [[ "$state" = "restarting" ]]; then
      rc="$(docker inspect -f '{{.RestartCount}}' "$name" 2>/dev/null || echo 0)"
      if [[ "${rc:-0}" -ge 3 ]]; then
        FAIL_FAST="$name（已重启 $rc 次）"
        break 2
      fi
    fi
  done
  for name in cict-mariadb cict-redis cict-rabbitmq; do
    [[ "$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null || echo none)" = "healthy" ]] || ok=0
  done
  # cict 容器无 healthcheck，用端口探测确认 Spring Boot 已真正监听 9090（容器 running ≠ 应用就绪）
  if [[ "$ok" -eq 1 ]]; then
    docker exec cict-app bash -c 'exec 3<>/dev/tcp/127.0.0.1/9090' 2>/dev/null || ok=0
  fi
  if [[ "$ok" -eq 1 ]]; then READY=1; break; fi
  sleep 5
done
docker compose ps
if [[ "$READY" -ne 1 ]]; then
  log "以下服务未就绪，打印最近日志辅助排障："
  for name in cict-mariadb cict-redis cict-rabbitmq cict-rustfs cict-app cict-nginx; do
    state="$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo missing)"
    health="$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null || true)"
    if [[ "$state" = "running" && ( -z "$health" || "$health" = "healthy" ) ]]; then
      continue
    fi
    log "===== $name（状态: $state${health:+，健康: $health}）最近 30 行日志 ====="
    docker logs --tail 30 "$name" 2>&1 || true
  done
  [[ -n "$FAIL_FAST" ]] && die "服务处于确定性重启循环: $FAIL_FAST，详见上方日志"
  die "部分服务未在 5 分钟内就绪，详见上方日志；完整日志: docker compose logs <服务>"
fi

# 空库检测：应用不会自动建表，空库将导致启动即崩溃（典型报错 Table 'cict.sys_dict_data' doesn't exist）
TABLE_COUNT="$(docker compose exec -T mariadb sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" -N -B -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()" cict' 2>/dev/null || echo -1)"
if [[ "${TABLE_COUNT:-"-1"}" = "0" ]]; then
  log "提示: cict 数据库当前为空。应用集成了 Liquibase，首次成功启动时会自动建表；"
  log "      若启动仍报 Table ... doesn't exist，通常是 MariaDB 数据目录属主被破坏（errno 13，见第 5 步说明）或版本未带自动建表（此时按部署指南“数据库初始化”手工导入）"
fi

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

# ===== 8. 创建 RustFS pike 存储桶（幂等，标准 S3 API + curl SigV4 签名） =====
env_val() { grep -E "^$1=" "$TARGET_DIR/.env" | cut -d= -f2; }
RUSTFS_BIND="$(env_val ADMIN_BIND_ADDRESS)"; RUSTFS_BIND="${RUSTFS_BIND:-127.0.0.1}"
RUSTFS_PORT_V="$(env_val RUSTFS_PORT)"; RUSTFS_PORT_V="${RUSTFS_PORT_V:-9000}"
BUCKET_CODE="$(curl -s -o /dev/null -w '%{http_code}' --aws-sigv4 aws:amz:us-east-1:s3 \
  --user "$(env_val RUSTFS_ACCESS_KEY):$(env_val RUSTFS_SECRET_KEY)" \
  -X PUT "http://${RUSTFS_BIND}:${RUSTFS_PORT_V}/pike" || echo 000)"
case "$BUCKET_CODE" in
  200) log "已创建 RustFS 存储桶 pike" ;;
  409) log "RustFS 存储桶 pike 已存在，跳过" ;;
  *) log "警告: 创建 pike 桶返回 HTTP $BUCKET_CODE，请按部署指南 3.10 手工创建" ;;
esac

# ===== 9. 记录部署状态并输出安装报告 =====
# 记录本次部署路径与版本：后续升级不带 --target-dir 时自动沿用（见第 0 步的复用逻辑）
install -d -m 0755 "$(dirname "$STATE_FILE")"
cat > "$STATE_FILE" <<STATEEOF
# CICT Docker 部署记录（install.sh 自动维护；升级未显式传 --target-dir 时沿用 TARGET_DIR）
TARGET_DIR=$TARGET_DIR
LAST_MODE=$([[ "$ONLINE" -eq 1 ]] && echo online || echo offline)
LAST_BUNDLE_TAG=$BUNDLE_TAG
LAST_INSTALL_AT=$(date '+%Y-%m-%d %H:%M:%S %z')
STATEEOF
log "已记录部署路径到 ${STATE_FILE}（下次升级不带 --target-dir 时自动沿用）"

SERVER_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
NGINX_PORT="$(env_val NGINX_PORT)"; NGINX_PORT="${NGINX_PORT:-80}"
CICT_PORT_V="$(env_val CICT_PORT)"; CICT_PORT_V="${CICT_PORT_V:-9090}"
MARIADB_PORT_V="$(env_val MARIADB_PORT)"; MARIADB_PORT_V="${MARIADB_PORT_V:-3306}"
cat <<EOF

==================== CICT 安装完成 ====================
访问地址      : http://${SERVER_IP:-<服务器IP>}:${NGINX_PORT}
后端 API 直连 : http://${SERVER_IP:-<服务器IP>}:${CICT_PORT_V}（绕过 nginx，供外部系统直连）
数据库直连    : ${SERVER_IP:-<服务器IP>}:${MARIADB_PORT_V}（账号与密码见 .env 的 MARIADB_USER/MARIADB_PASSWORD）
密码文件      : $TARGET_DIR/.env （权限 600，含全部随机密码，请立即备份）
后端镜像版本  : $BUNDLE_TAG

后续步骤:
  1. RustFS 存储桶 pike 已在本安装中自动创建；如需控制台核对:
     ssh -L 9001:127.0.0.1:9001 <用户>@${SERVER_IP:-<服务器IP>} 后访问 http://127.0.0.1:9001
  2. 后端 ${CICT_PORT_V} 与 MariaDB ${MARIADB_PORT_V} 已绑定 0.0.0.0 对外直连，请在防火墙限制来源网段；
     管理端口默认只绑定 127.0.0.1，远程访问一律走 SSH 隧道（RabbitMQ 15672、RustFS 9000/9001）
  3. 查看服务状态: cd $TARGET_DIR 后执行 docker compose ps
     跟踪服务日志: docker compose logs -f <服务名>（服务名: mariadb/redis/rabbitmq/rustfs/cict/nginx）
  4. 后续升级: 在线环境重跑本命令并换新 tag；离线环境用新包重跑；不带 --target-dir 时自动沿用本次
     部署路径（记录于 ${STATE_FILE}）。详见部署指南附录 A.3

=======================================================
EOF
log "安装完成。"
