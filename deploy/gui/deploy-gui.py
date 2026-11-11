#!/usr/bin/env python3
"""CICT 运维控制台（Web UI，部署向导的常驻升级版）。

单文件、仅依赖 Python 3 标准库（Ubuntu 24.04 自带 python3，离线可用）。
- 部署：浏览器中编辑配置/密码 → 调用 install.sh 完成安装/升级（引擎逻辑不复制）
- 运维：概览/改配置一键应用（docker compose up -d）/服务独立启停重启/前后端独立升级/看日志
- 自身：由 install.sh 安装为 systemd 服务 cict-ops 常驻；支持上传新版 .run 在线自更新
- 安全：只绑定 127.0.0.1:8788（远程走 SSH 隧道）；GUI/console.env 中的 SERVER_TOKEN
  作为访问令牌（X-Token 头或 ?token=），未配置时退化为无鉴权并打警告（仅限一次性向导场景）

用法:
  systemd 常驻: systemctl start cict-ops   （install.sh 已自动安装）
  手动在线模式（仓库 deploy/gui/）: sudo python3 deploy-gui.py
  手动离线模式（bundle 解压目录内）: sudo python3 deploy-gui.py
  然后浏览器访问 http://127.0.0.1:8788
"""
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = 8788
SCRIPT_DIR = Path(__file__).resolve().parent
GITHUB_REPO = "zhiyong-zhu/deployment"
# 上传体大小上限（字节）：后端 JAR 2GB / 前端 zip 1GB / 控制台更新包 200MB
MAX_UPLOAD = {"backend": 2 << 30, "frontend": 1 << 30, "update": 200 << 20}

# ===== 模式与路径定位 =====
if (SCRIPT_DIR / "manifest.sha256").is_file():
    MODE = "offline"                       # bundle 根目录
    SRC = SCRIPT_DIR / "project"
    INSTALL_SH = SCRIPT_DIR / "install.sh"
    BUNDLE_TAG = (SCRIPT_DIR / "bundle-tag").read_text().strip() if (SCRIPT_DIR / "bundle-tag").is_file() else ""
    CONSOLE_VERSION_FILE = SCRIPT_DIR / "VERSION"
elif ((SCRIPT_DIR.parent) / "docker-compose.yml").is_file():
    MODE = "online"                        # 仓库 deploy/gui/ 或 /data/app/cict-ops/gui/
    SRC = SCRIPT_DIR.parent
    INSTALL_SH = SCRIPT_DIR.parent / "bundle" / "install.sh"
    BUNDLE_TAG = ""
    CONSOLE_VERSION_FILE = SCRIPT_DIR / "VERSION"
else:
    raise SystemExit("请在离线安装包目录、运维台目录或仓库 deploy/gui/ 下运行本程序")

CONSOLE_VERSION = CONSOLE_VERSION_FILE.read_text().strip() if CONSOLE_VERSION_FILE.is_file() else "dev"
# 访问令牌：GUI/console.env（install.sh 生成并保留）；缺失时无鉴权（临时向导可接受，常驻服务必须配）
SERVER_TOKEN = ""
_console_env = SCRIPT_DIR / "console.env"
if _console_env.is_file():
    try:
        for line in _console_env.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^SERVER_TOKEN=(.+)$", line.strip())
            if m:
                SERVER_TOKEN = m.group(1).strip()
    except OSError:
        pass

DEFAULT_TARGET_BASE = "/data/app/cict-docker"
# install.sh 安装成功后会把部署路径记录到该文件（与命令行共用，升级/GUI 默认沿用上次路径）
STATE_FILE = Path("/etc/cict-docker/deploy.env")


def recorded_target():
    """读取 install.sh 记录的上次部署路径；目录里仍有部署文件才返回，否则回退默认值。"""
    try:
        for line in STATE_FILE.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^TARGET_DIR=(.+)$", line.strip())
            if m:
                path = m.group(1).strip()
                if path.startswith("/") and (Path(path) / "docker-compose.yml").is_file():
                    return path
                return None
    except OSError:
        pass
    return None


def current_target(override=None):
    """每次请求实时解析目标目录（常驻进程不能只在 import 时算一次）。"""
    if override and override.startswith("/"):
        return override
    return recorded_target() or DEFAULT_TARGET_BASE


def read_state(key):
    try:
        for line in STATE_FILE.read_text(encoding="utf-8").splitlines():
            m = re.match(rf"^{re.escape(key)}=(.*)$", line.strip())
            if m:
                return m.group(1).strip()
    except OSError:
        pass
    return ""


def server_ip():
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5)
        return (out.stdout.strip().split() or [""])[0]
    except Exception:  # noqa: BLE001
        return ""


# 可编辑的配置文件（相对路径 → 界面标签）。读写的都是「部署目录」的副本——
# 这些文件要么是 bind mount（application.properties），要么 up -d 时同步进容器
EDITABLE_FILES = {
    "docker-compose.yml": "docker-compose.yml（服务编排）",
    "nginx/nginx.conf": "nginx.conf（Nginx 主配置）",
    "nginx/conf.d/cict.conf": "cict.conf（站点与 API 代理）",
    "mariadb/server.cnf": "server.cnf（MariaDB 调优）",
    "cict/application.properties": "application.properties（Spring Boot 后端）",
}
SECRET_KEYS = {
    "MARIADB_ROOT_PASSWORD", "MARIADB_PASSWORD", "REDIS_PASSWORD",
    "RABBITMQ_DEFAULT_PASS", "RABBITMQ_ADMIN_PASSWORD",
    "RUSTFS_ACCESS_KEY", "RUSTFS_SECRET_KEY",
}
FIELD_LABELS = {
    "MARIADB_ROOT_PASSWORD": "MariaDB root 密码",
    "MARIADB_DATABASE": "数据库名",
    "MARIADB_USER": "数据库用户",
    "MARIADB_PASSWORD": "数据库用户密码",
    "REDIS_PASSWORD": "Redis 密码",
    "RABBITMQ_DEFAULT_USER": "RabbitMQ 应用账号",
    "RABBITMQ_DEFAULT_PASS": "RabbitMQ 应用密码",
    "RABBITMQ_ADMIN_PASSWORD": "RabbitMQ 管理密码",
    "RUSTFS_ACCESS_KEY": "RustFS AccessKey",
    "RUSTFS_SECRET_KEY": "RustFS SecretKey",
    "JAVA_TOOL_OPTIONS": "JVM 参数",
    "CICT_TZ": "容器时区",
    "CICT_PUBLIC_S3_ENDPOINT": "S3 对外地址（预签 URL 用）",
    "NGINX_PORT": "Nginx 对外端口",
    "CICT_PORT": "后端 API 直连端口",
    "MARIADB_PORT": "MariaDB 直连端口",
    "ADMIN_BIND_ADDRESS": "RabbitMQ 管理端口绑定地址",
    "RABBITMQ_MGMT_PORT": "RabbitMQ 管理端口",
    "RUSTFS_BIND_ADDRESS": "RustFS 端口绑定地址",
    "RUSTFS_PORT": "RustFS S3 端口",
    "RUSTFS_CONSOLE_PORT": "RustFS 控制台端口",
    "CICT_IMAGE_TAG": "后端镜像版本",
}
# 保存 .env 前的取值校验（key → (说明, 校验函数)）：把线上踩过的坑在表单层拦住
ENV_VALIDATORS = {
    "CICT_PUBLIC_S3_ENDPOINT": (
        "必须以 http:// 或 https:// 开头，或留空（留空回落容器内地址）",
        lambda v: v == "" or v.startswith("http://") or v.startswith("https://")),
    "ADMIN_BIND_ADDRESS": ("IP 地址或留空", lambda v: v == "" or re.match(r"^\d+\.\d+\.\d+\.\d+$|^[0-9a-fA-F:]+$", v)),
    "RUSTFS_BIND_ADDRESS": ("IP 地址或留空", lambda v: v == "" or re.match(r"^\d+\.\d+\.\d+\.\d+$|^[0-9a-fA-F:]+$", v)),
    "NGINX_PORT": ("1-65535 端口号", lambda v: v.isdigit() and 1 <= int(v) <= 65535),
    "CICT_PORT": ("1-65535 端口号", lambda v: v.isdigit() and 1 <= int(v) <= 65535),
    "MARIADB_PORT": ("1-65535 端口号", lambda v: v.isdigit() and 1 <= int(v) <= 65535),
    "RABBITMQ_MGMT_PORT": ("1-65535 端口号", lambda v: v.isdigit() and 1 <= int(v) <= 65535),
    "RUSTFS_PORT": ("1-65535 端口号", lambda v: v.isdigit() and 1 <= int(v) <= 65535),
    "RUSTFS_CONSOLE_PORT": ("1-65535 端口号", lambda v: v.isdigit() and 1 <= int(v) <= 65535),
}


def parse_env(text):
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def render_env(template, values, tag):
    """按模板顺序生成 .env：payload 中的值优先，未填的密码现场随机，tag 占位符替换。"""
    lines = []
    for line in template.splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if m and m.group(1) in values and values[m.group(1)] != "":
            line = f"{m.group(1)}={values[m.group(1)]}"
        rm = re.search(r"__RANDOM_HEX_(\d+)__", line)
        if rm:
            line = re.sub(r"__RANDOM_HEX_\d+__", secrets.token_hex(int(rm.group(1))), line)
        line = line.replace("__CICT_IMAGE_TAG__", tag)
        lines.append(line)
    return "\n".join(lines) + "\n"


def target_env(target):
    """读部署目录 .env；不存在返回空 dict。"""
    path = Path(target) / ".env"
    return parse_env(path.read_text(encoding="utf-8")) if path.is_file() else {}


# ===== 长任务（安装/应用/升级/服务操作共用一套输出流） =====
class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.lines = deque(maxlen=5000)
        self.running = False
        self.exit_code = None
        self.cmd = ""

    def start(self, cmd, cwd=None, label="task"):
        """启动子任务并流式收集输出；cwd 缺省为 install.sh 所在目录。"""
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.exit_code = None
            self.cmd = " ".join(cmd) if isinstance(cmd, list) else str(cmd)

        def run():
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(cwd or INSTALL_SH.parent), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1, errors="replace")
                for line in proc.stdout:
                    with self.lock:
                        self.lines.append(line.rstrip("\n"))
                code = proc.wait()
                with self.lock:
                    self.exit_code = code
                    self.lines.append(f"[deploy-gui] {label} 退出码: {code}")
            except Exception as exc:  # noqa: BLE001
                with self.lock:
                    self.exit_code = 127
                    self.lines.append(f"[deploy-gui] 启动失败: {exc}")
            finally:
                with self.lock:
                    self.running = False

        threading.Thread(target=run, daemon=True).start()
        return True


JOB = Job()
# 跨请求的界面回显状态（Handler 实例每连接新建，不能放 self）
UI_STATE = {"app_jar": "", "frontend": ""}


def http_status(url, timeout=5):
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return None


def port_open(port, host="127.0.0.1"):
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


CONTAINERS = [
    ("cict-mariadb", True), ("cict-redis", True), ("cict-rabbitmq", True),
    ("cict-rustfs", False), ("cict-app", False), ("cict-nginx", False),
]

# 服务清单：compose 服务名 → 容器名 → 界面标签（日志/启停操作只接受这里的服务名）
SERVICES = [
    ("mariadb", "cict-mariadb", "MariaDB"),
    ("redis", "cict-redis", "Redis"),
    ("rabbitmq", "cict-rabbitmq", "RabbitMQ"),
    ("rustfs", "cict-rustfs", "RustFS"),
    ("cict", "cict-app", "CICT 后端"),
    ("nginx", "cict-nginx", "Nginx"),
]
SERVICE_NAMES = {svc for svc, _, _ in SERVICES}


def run_verify(target_dir):
    results = []
    for name, need_health in CONTAINERS:
        try:
            out = subprocess.run(
                ["docker", "inspect", "-f",
                 "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}", name],
                capture_output=True, text=True, timeout=10)
            state = out.stdout.strip()
        except Exception:
            state = ""
        ok = (state.split("|")[0:1] or [""])[0] == "running" and (
            (not need_health) or state.endswith("|healthy"))
        results.append({"name": name, "ok": ok, "detail": state.replace("|", " / ") or "容器不存在"})

    env = target_env(target_dir)
    nginx_port = env.get("NGINX_PORT", "80")
    front = http_status(f"http://127.0.0.1:{nginx_port}/")
    results.append({"name": f"前端页面 (:{nginx_port})", "ok": front == 200, "detail": f"HTTP {front}"})
    api = http_status(f"http://127.0.0.1:{nginx_port}/api/auth/login")
    results.append({"name": "后端 API 探活", "ok": api is not None and api < 500, "detail": f"HTTP {api}（非 5xx 即在响应）"})
    for port, label in ((env.get("RABBITMQ_MGMT_PORT", "15672"), "RabbitMQ 管理端口"),
                        (env.get("RUSTFS_PORT", "9000"), "RustFS S3 端口"),
                        (env.get("RUSTFS_CONSOLE_PORT", "9001"), "RustFS 控制台端口")):
        results.append({"name": f"{label} (:{port} 仅本机)", "ok": port_open(int(port)), "detail": "监听 127.0.0.1" if port_open(int(port)) else "未监听"})
    for port, label in ((env.get("CICT_PORT", "9090"), "后端 API 直连端口"),
                        (env.get("MARIADB_PORT", "3306"), "MariaDB 直连端口")):
        ok = port_open(int(port))
        results.append({"name": f"{label} (:{port} 对外)", "ok": ok, "detail": "已监听" if ok else "未监听"})
    return results


# ===== 服务状态 / 概览数据源 =====

def run_docker(args, timeout=10):
    """执行 docker 子命令；命令不存在/超时/其他异常返回 (None, 错误信息)。"""
    try:
        proc = subprocess.run(["docker", *args], capture_output=True, text=True,
                              timeout=timeout, errors="replace")
    except FileNotFoundError:
        return None, "未安装 Docker"
    except subprocess.SubprocessError as exc:
        return None, str(exc)
    return proc, ""


def human_uptime(started_at):
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        seconds = int((datetime.now(timezone.utc) - started).total_seconds())
    except ValueError:
        return ""
    days, rem = divmod(max(seconds, 0), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} 天 {hours} 时"
    if hours:
        return f"{hours} 时 {minutes} 分"
    return f"{minutes} 分"


def get_services(target_dir):
    """容器状态 + docker port 映射 + 按 .env 端口配置的本机监听检测。"""
    env = target_env(target_dir)
    version_proc, docker_error = run_docker(["--version"])
    docker_version = version_proc.stdout.strip() if version_proc and version_proc.returncode == 0 else ""
    # CLI 存在 ≠ 守护进程在跑：daemon 未启动时跳过逐容器查询，避免全部误报"容器不存在"
    daemon_ok = False
    if docker_version:
        proc, _ = run_docker(["version", "--format", "{{.Server.Version}}"])
        daemon_ok = bool(proc and proc.returncode == 0)
        if not daemon_ok:
            stderr_tail = proc.stderr.strip().splitlines()[-1:] if proc and proc.stderr else []
            docker_error = stderr_tail[0] if stderr_tail else "Docker 守护进程未运行"
    services = []
    for service, cname, label in SERVICES:
        state = health = restarts = started_at = ""
        ports = []
        if daemon_ok:
            proc, _ = run_docker(["inspect", "-f",
                "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{.RestartCount}}|{{.State.StartedAt}}",
                cname])
            if proc and proc.returncode == 0 and proc.stdout.strip():
                state, health, restarts, started_at = (proc.stdout.strip().split("|") + ["", "", "", ""])[:4]
            proc, _ = run_docker(["port", cname])
            if proc and proc.returncode == 0:
                ports = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
        # 同一容器端口同时绑定 0.0.0.0 与 [::] 时只显示 IPv4 行
        ipv4 = [p for p in ports if "[::]" not in p]
        services.append({
            "service": service, "label": label, "container": cname,
            "state": state, "health": health, "restarts": restarts,
            "uptime": human_uptime(started_at) if state == "running" else "",
            "ports": ipv4 or ports,
        })
    admin_bind = env.get("ADMIN_BIND_ADDRESS", "127.0.0.1")
    rustfs_bind = env.get("RUSTFS_BIND_ADDRESS", "0.0.0.0")
    port_checks = []
    for name, key, default, bind in (
            ("Nginx 对外端口", "NGINX_PORT", "80", "0.0.0.0"),
            ("后端 API 直连端口", "CICT_PORT", "9090", "0.0.0.0"),
            ("MariaDB 直连端口", "MARIADB_PORT", "3306", "0.0.0.0"),
            ("RabbitMQ 管理端口", "RABBITMQ_MGMT_PORT", "15672", admin_bind),
            ("RustFS S3 端口", "RUSTFS_PORT", "9000", rustfs_bind),
            ("RustFS 控制台端口", "RUSTFS_CONSOLE_PORT", "9001", rustfs_bind)):
        try:
            port = int(env.get(key, default) or default)
        except ValueError:
            continue
        port_checks.append({"name": name, "port": port, "bind": bind, "ok": port_open(port)})
    return {
        "ok": True,
        "docker": docker_version,
        "docker_error": docker_error,
        "target_dir": target_dir,
        "services": services,
        "port_checks": port_checks,
    }


def get_overview(target_dir):
    """概览页：版本信息 + 服务/端口（复用 get_services）+ 访问地址总表。"""
    env = target_env(target_dir)
    ip = server_ip() or "<服务器IP>"
    nginx_port = env.get("NGINX_PORT", "80")
    cict_port = env.get("CICT_PORT", "9090")
    mariadb_port = env.get("MARIADB_PORT", "3306")
    rustfs_port = env.get("RUSTFS_PORT", "9000")
    rustfs_console = env.get("RUSTFS_CONSOLE_PORT", "9001")
    rabbit_port = env.get("RABBITMQ_MGMT_PORT", "15672")
    s3_public = env.get("CICT_PUBLIC_S3_ENDPOINT", "")
    urls = [
        {"name": "Web 入口（前端）", "url": f"http://{ip}:{nginx_port}", "note": "浏览器统一入口"},
        {"name": "运维控制台（本程序）", "url": f"http://127.0.0.1:{PORT}", "note": "远程先建隧道 ssh -L 8788:127.0.0.1:8788"},
        {"name": "后端 API 直连", "url": f"http://{ip}:{cict_port}", "note": "绕过 nginx，供外部系统直连"},
        {"name": "MariaDB 直连", "url": f"{ip}:{mariadb_port}", "note": "账号密码见 .env 的 MARIADB_USER/MARIADB_PASSWORD"},
        {"name": "RustFS S3 API", "url": s3_public or f"http://{ip}:{rustfs_port}", "note": "预签 URL / 外部 S3 工具；AK/SK 见 .env"},
        {"name": "RustFS 控制台", "url": f"http://{ip}:{rustfs_console}", "note": "登录使用 .env 的 RUSTFS_ACCESS_KEY/SECRET_KEY"},
        {"name": "RabbitMQ 管理", "url": f"http://127.0.0.1:{rabbit_port}", "note": "仅本机绑定，远程走 SSH 隧道"},
    ]
    return {
        "ok": True,
        "console_version": CONSOLE_VERSION,
        "console_token_configured": bool(SERVER_TOKEN),
        "deploy": {
            "target_dir": target_dir,
            "image_tag": env.get("CICT_IMAGE_TAG", "-"),
            "last_install_at": read_state("LAST_INSTALL_AT") or "-",
            "mode": read_state("LAST_MODE") or "-",
        },
        "urls": urls,
        **get_services(target_dir),
    }


def get_logs(service, lines):
    containers = {svc: cname for svc, cname, _ in SERVICES}
    cname = containers.get(service)
    if not cname:
        return {"ok": False, "error": f"未知服务: {service}"}
    lines = max(20, min(lines, 2000))
    try:
        # stderr 合并进 stdout：nginx 等服务的日志分写两个流，合并后按产生顺序交错便于阅读
        proc = subprocess.run(["docker", "logs", "--tail", str(lines), cname],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, timeout=60, errors="replace")
    except FileNotFoundError:
        return {"ok": False, "error": "未安装 Docker"}
    except subprocess.SubprocessError as exc:
        return {"ok": False, "error": f"读取日志超时: {exc}"}
    output = proc.stdout or ""
    if proc.returncode != 0:
        detail = output.strip().splitlines()
        return {"ok": False, "error": detail[-1] if detail else f"docker logs 退出码 {proc.returncode}"}
    # 截断到 400KB，避免超长日志拖垮浏览器渲染
    return {"ok": True, "service": service, "container": cname, "log": output[-400000:]}


# ===== 部署目录配置文件读写（应用闭环） =====

def compose_validate(target_dir, nginx_changed=False):
    """docker compose config 预检 + nginx 配置预检；返回 (True, "") 或 (False, 错误文本)。"""
    proc, err = run_docker(["compose", "config", "--quiet"], timeout=30)
    if err:
        return False, f"docker compose 不可用: {err}"
    if proc.returncode != 0:
        return False, "docker compose config 校验失败:\n" + (proc.stdout + proc.stderr).strip()[-2000:]
    if nginx_changed:
        proc, err = run_docker(["compose", "run", "--rm", "--no-deps", "nginx", "nginx", "-t"], timeout=60)
        if err or not proc or proc.returncode != 0:
            detail = (proc.stdout + proc.stderr if proc else err or "").strip()[-2000:]
            return False, "nginx -t 语法校验失败:\n" + detail
    return True, ""


def target_files_read(target_dir):
    base = Path(target_dir)
    files = []
    for rel, label in EDITABLE_FILES.items():
        path = base / rel
        files.append({
            "path": rel, "label": label,
            "exists": path.is_file(),
            "content": path.read_text(encoding="utf-8", errors="replace") if path.is_file() else "",
        })
    return {"ok": True, "target_dir": target_dir, "files": files,
            "hint": "这些是部署目录的副本：application.properties 已挂载进容器，其余文件在「应用变更」(up -d) 时生效"}


def target_files_save(target_dir, files):
    base = Path(target_dir)
    if not (base / "docker-compose.yml").is_file():
        return {"ok": False, "error": f"{target_dir} 不是有效的部署目录（缺少 docker-compose.yml）"}
    saved, nginx_changed, compose_changed = [], False, False
    try:
        for rel, content in files.items():
            if rel not in EDITABLE_FILES:
                return {"ok": False, "error": f"不允许写入: {rel}"}
            path = base / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            saved.append(rel)
            if rel.startswith("nginx/"):
                nginx_changed = True
            if rel == "docker-compose.yml":
                compose_changed = True
        # 保存即预检：坏配置当场暴露，不带病进入 up -d
        if compose_changed or nginx_changed:
            ok, err = compose_validate(target_dir, nginx_changed=nginx_changed)
            if not ok:
                return {"ok": False, "error": err, "saved": saved,
                        "note": "文件已写入但校验未通过，请修正后再点「应用变更」"}
        return {"ok": True, "saved": saved}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc), "saved": saved}


def apply_changes(target_dir):
    """应用配置变更：docker compose up -d（只重建配置有变化的容器）。"""
    if JOB.running:
        return {"ok": False, "error": "已有任务在运行，请等它结束"}
    ok, err = compose_validate(target_dir)
    if not ok:
        return {"ok": False, "error": err}
    # best-effort 预览待重建容器（compose 2.19+ 支持 --dry-run，旧版忽略）
    preview = ""
    proc, _ = run_docker(["compose", "up", "-d", "--dry-run"], timeout=30)
    if proc and proc.returncode == 0:
        preview = "\n".join(l for l in proc.stdout.splitlines() if "Recreate" in l or "Create" in l)[:2000]
    # application.properties 是 bind mount：内容变更不会触发 up -d 重建容器，
    # 而 JVM 只在启动时读它 —— 文件 mtime 晚于容器启动时间就必须补一次 restart cict
    restart_cict = False
    props = Path(target_dir) / "cict" / "application.properties"
    proc, _ = run_docker(["inspect", "-f", "{{.State.StartedAt}}", "cict-app"])
    if props.is_file() and proc and proc.returncode == 0 and proc.stdout.strip():
        try:
            started = datetime.fromisoformat(proc.stdout.strip().replace("Z", "+00:00"))
            restart_cict = datetime.fromtimestamp(props.stat().st_mtime, timezone.utc) > started
        except ValueError:
            restart_cict = False
    script = "docker compose up -d"
    if restart_cict:
        script += " && docker compose restart cict"
    script += " && docker compose ps"
    started = JOB.start(["bash", "-c", script], cwd=target_dir, label="apply (docker compose up -d)")
    note = "检测到 application.properties 有更新，将在 up -d 后自动重启 cict 使其生效" if restart_cict else ""
    return {"ok": started, "preview": preview or "（当前 compose 版本不支持 --dry-run 预览，已直接执行）", "note": note}


def service_action(target_dir, service, action):
    if service not in SERVICE_NAMES:
        return {"ok": False, "error": f"未知服务: {service}"}
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "error": f"不支持的操作: {action}"}
    if JOB.running:
        return {"ok": False, "error": "已有任务在运行，请等它结束"}
    started = JOB.start(["docker", "compose", action, service], cwd=target_dir,
                        label=f"{action} {service}")
    return {"ok": started, "note": "重启 cict 只重启 Java 进程，不会重读 .env；改 .env 后请用「应用变更」"}


# ===== 独立升级 =====

def backup_rotate(path, keep=3):
    """把现有文件备份为 <name>.bak-<ts>，只保留最近 keep 份。"""
    if not path.is_file():
        return
    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
    shutil.copy2(path, path.with_name(f"{path.name}.bak-{stamp}"))
    baks = sorted(path.parent.glob(f"{path.name}.bak-*"))
    for old in baks[:-keep]:
        old.unlink(missing_ok=True)


def upgrade_backend(target_dir, temp_path, new_tag=""):
    """独立升级后端：校验 JAR → 备份 → 替换 → 重新构建镜像并重建 cict 容器。"""
    base = Path(target_dir)
    if not (base / "cict" / "Dockerfile").is_file():
        return {"ok": False, "error": f"{target_dir}/cict 不是有效的部署目录"}
    if not zipfile.is_zipfile(temp_path):
        return {"ok": False, "error": "不是有效的 JAR 文件（zip 结构校验失败）"}
    jar = base / "cict" / "app.jar"
    jar.parent.mkdir(parents=True, exist_ok=True)
    backup_rotate(jar)
    shutil.copy2(temp_path, jar)
    os.chmod(jar, 0o644)
    if new_tag:
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", new_tag):
            return {"ok": False, "error": f"非法版本标签: {new_tag}（JAR 已替换，请手工修正 .env 的 CICT_IMAGE_TAG）"}
        env_path = base / ".env"
        if env_path.is_file():
            text = env_path.read_text(encoding="utf-8")
            new = re.sub(r"^CICT_IMAGE_TAG=.*$", f"CICT_IMAGE_TAG={new_tag}", text, flags=re.M)
            if new != text:
                env_path.write_text(new, encoding="utf-8")
    if JOB.running:
        return {"ok": False, "error": "JAR 已替换，但有任务在运行，未启动构建；请稍后手动执行「应用变更」"}
    started = JOB.start(
        ["bash", "-c", "docker compose build cict && docker compose up -d --no-deps cict && docker compose ps cict"],
        cwd=target_dir, label="upgrade-backend")
    return {"ok": started, "note": "构建镜像约需 1-3 分钟，完成后 cict 容器自动重建"}


def upgrade_frontend(target_dir, temp_path):
    """独立升级前端：解压 zip → 定位 index.html → 就地同步 nginx/html（bind mount 实时生效）。"""
    base = Path(target_dir)
    html_dir = base / "nginx" / "html"
    if not (base / "docker-compose.yml").is_file():
        return {"ok": False, "error": f"{target_dir} 不是有效的部署目录"}
    tmp = Path(tempfile.mkdtemp(prefix="cict-fe-"))
    try:
        try:
            with zipfile.ZipFile(temp_path) as zf:
                zf.extractall(tmp)
        except zipfile.BadZipFile:
            return {"ok": False, "error": "不是有效的 zip 文件"}
        if (tmp / "index.html").is_file():
            src = tmp
        elif (tmp / "dist" / "index.html").is_file():
            src = tmp / "dist"
        else:
            return {"ok": False, "error": "压缩包内找不到 index.html（根目录或 dist/ 下）"}
        # 就地同步（清空内容后复制，不替换目录本身）：目录 inode 不变，
        # 运行中容器的 bind mount 才不会指向孤儿目录；并放开读权限（mktemp 的 700 会让 nginx 用户读不到）
        html_dir.mkdir(parents=True, exist_ok=True)
        for child in html_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        count = 0
        for item in src.iterdir():
            if item.is_dir():
                shutil.copytree(item, html_dir / item.name)
                count += 1
            else:
                shutil.copy2(item, html_dir / item.name)
                count += 1
        subprocess.run(["chmod", "-R", "a+rX", str(html_dir)], check=False)
        if not (html_dir / "index.html").is_file():
            return {"ok": False, "error": "同步后 index.html 缺失（意外）"}
        return {"ok": True, "note": f"已同步 {count} 个文件/目录到 nginx/html，前端为 bind mount 实时生效，浏览器强刷（Ctrl+F5）即可"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ===== 控制台自更新 =====

def console_root():
    return SCRIPT_DIR.parent


UPDATE_FILE_SET = [
    "gui/deploy-gui.py", "gui/VERSION", "bundle/install.sh",
    "docker-compose.yml", ".env.example",
    "cict/Dockerfile", "cict/application.properties",
    "nginx/nginx.conf", "nginx/conf.d/cict.conf", "mariadb/server.cnf",
]


def extract_run_payload(run_path, dest):
    """按 make-portable.sh 的 stub 协议解出 .run 内嵌的 deploy 树（PAYLOAD_START 行定位 + tar.gz）。"""
    data = Path(run_path).read_bytes()
    m = re.search(rb"^PAYLOAD_START=(\d+)$", data, re.M)
    if not m:
        raise ValueError("不是有效的 cict-installer.run（缺少 PAYLOAD_START 标记）")
    offset = 0
    for _ in range(int(m.group(1))):
        idx = data.find(b"\n", offset)
        if idx < 0:
            raise ValueError("安装器头部损坏")
        offset = idx + 1
    with tarfile.open(fileobj=io.BytesIO(data[offset:]), mode="r:gz") as tar:
        try:
            tar.extractall(dest, filter="data")     # Python 3.12+
        except TypeError:
            tar.extractall(dest)                    # 兼容旧版


def console_update_from(extracted, new_version):
    """把解包出的新版本文件覆盖进控制台目录（保留 console.env），并调度服务重启。"""
    root = console_root()
    copied = []
    for rel in UPDATE_FILE_SET:
        src = Path(extracted) / rel
        if not src.is_file():
            continue
        dst = root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied.append(rel)
    if "gui/deploy-gui.py" not in copied:
        raise ValueError("更新包中缺少 gui/deploy-gui.py，放弃更新")
    # 清掉旧字节码，避免解释器用到过期缓存
    shutil.rmtree(SCRIPT_DIR / "__pycache__", ignore_errors=True)
    restarted = False
    if Path("/etc/systemd/system/cict-ops.service").is_file():
        subprocess.Popen(["/bin/sh", "-c", "sleep 1.5; systemctl restart cict-ops"],
                         start_new_session=True)
        restarted = True
    note = "控制台服务将在 1-2 秒后自动重启，稍候刷新本页即可（访问令牌不变）" if restarted else \
        "已更新文件；当前控制台非 systemd 服务运行，请手动重启本程序后生效"
    return {"ok": True, "version": new_version, "copied": copied, "note": note}


def update_check():
    """查 GitHub Releases 最新版本；服务器不通外网时给出明确错误。"""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": "cict-ops-console"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"GitHub 不可达（{exc}）；内网环境请用「上传 .run 更新」"}
    asset = ""
    for a in data.get("assets", []):
        if a.get("name", "").endswith(".run"):
            asset = a.get("browser_download_url", "")
            break
    latest = data.get("tag_name", "")
    return {"ok": True, "current": CONSOLE_VERSION, "latest": latest, "asset_url": asset,
            "up_to_date": latest == CONSOLE_VERSION or CONSOLE_VERSION.startswith(f"{latest}-")}


def update_apply_github(asset_url):
    req = urllib.request.Request(asset_url, headers={"User-Agent": "cict-ops-console"})
    tmp = tempfile.NamedTemporaryFile(prefix="cict-update-", suffix=".run", delete=False)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp, tmp:
            shutil.copyfileobj(resp, tmp)
    except Exception as exc:  # noqa: BLE001
        os.unlink(tmp.name)
        return {"ok": False, "error": f"下载新版安装器失败: {exc}"}
    return update_apply_file(tmp.name)


def update_apply_file(run_path):
    dest = Path(tempfile.mkdtemp(prefix="cict-update-x-"))
    try:
        extract_run_payload(run_path, dest)
        if not (dest / "gui" / "deploy-gui.py").is_file() or not (dest / "docker-compose.yml").is_file():
            return {"ok": False, "error": "安装器内容不完整（缺 gui/deploy-gui.py 或 docker-compose.yml）"}
        new_version = (dest / "gui" / "VERSION").read_text().strip() if (dest / "gui" / "VERSION").is_file() else "?"
        return console_update_from(dest, new_version)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"更新失败: {exc}"}
    finally:
        shutil.rmtree(dest, ignore_errors=True)


# ===== HTTP 服务 =====
HTML = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>CICT 运维控制台</title>
<style>
 body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:#f4f6f8;color:#1f2937;padding-bottom:46px}
 header{background:#111827;color:#fff;padding:14px 24px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
 header h1{font-size:17px;margin:0;font-weight:600}
 .badge{font-size:12px;padding:2px 10px;border-radius:10px;background:#374151}
 .badge.ok{background:#059669}.badge.warn{background:#b45309}
 nav{display:flex;gap:4px;padding:0 24px;background:#fff;border-bottom:1px solid #e5e7eb}
 nav button{border:none;background:none;padding:12px 18px;font-size:14px;cursor:pointer;color:#6b7280;border-bottom:2px solid transparent}
 nav button.active{color:#111827;border-bottom-color:#2563eb;font-weight:600}
 main{padding:24px;max-width:1060px;margin:0 auto}
 .card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:20px;margin-bottom:16px}
 .card h2{font-size:15px;margin:0 0 14px;color:#111827}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
 .field label{display:block;font-size:12px;color:#6b7280;margin-bottom:4px}
 .field input{width:100%;box-sizing:border-box;padding:7px 9px;border:1px solid #d1d5db;border-radius:6px;font-size:13px;font-family:inherit}
 .field input.bad{border-color:#dc2626;background:#fef2f2}
 .field .row{display:flex;gap:6px}
 .mini{font-size:12px;padding:6px 10px;border:1px solid #d1d5db;background:#f9fafb;border-radius:6px;cursor:pointer;white-space:nowrap}
 .mini:hover{background:#eff6ff;border-color:#93c5fd}
 .mini.danger{color:#b91c1c}
 .btn{padding:9px 20px;border:none;border-radius:6px;background:#2563eb;color:#fff;font-size:14px;cursor:pointer}
 .btn:disabled{background:#9ca3af;cursor:not-allowed}
 .btn.green{background:#059669}.btn.orange{background:#ea580c}
 select{padding:7px 9px;border:1px solid #d1d5db;border-radius:6px;font-size:13px;background:#fff;color:#1f2937;font-family:inherit}
 pre#svclog{background:#0b1021;color:#d1fae5;font-size:12px;line-height:1.55;padding:14px;border-radius:8px;
   height:420px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}
 table{width:100%;border-collapse:collapse;font-size:13px}
 td,th{padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top}
 .st{font-weight:700}.st.ok{color:#059669}.st.bad{color:#dc2626}
 details{margin-top:14px}summary{cursor:pointer;font-size:13px;color:#374151}
 textarea{width:100%;box-sizing:border-box;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;
   min-height:180px;padding:8px;border:1px solid #d1d5db;border-radius:6px}
 textarea.mod{border-color:#f59e0b;background:#fffbeb}
 .hint{font-size:12px;color:#6b7280;margin-top:10px;line-height:1.7}
 .warnbox{background:#fffbeb;border:1px solid #fcd34d;color:#92400e;font-size:12px;padding:8px 12px;border-radius:6px;margin-top:10px;display:none}
 #drawer{position:fixed;left:0;right:0;bottom:0;background:#111827;color:#e5e7eb;z-index:40}
 #drawer .bar{display:flex;gap:10px;align-items:center;padding:8px 24px;cursor:pointer;font-size:13px}
 #drawer pre{background:#0b1021;color:#d1fae5;font-size:12px;line-height:1.5;padding:12px 24px;height:240px;
   overflow-y:auto;white-space:pre-wrap;word-break:break-all;margin:0}
 .up{border:2px dashed #d1d5db;border-radius:8px;padding:14px;font-size:13px;color:#374151;margin-bottom:10px}
 .up input[type=file]{font-size:12px}
 #gate{position:fixed;inset:0;background:rgba(17,24,39,.85);z-index:99;display:none;align-items:center;justify-content:center}
 #gate .box{background:#fff;border-radius:10px;padding:28px;width:340px}
 #toast{position:fixed;bottom:60px;right:24px;background:#111827;color:#fff;padding:10px 18px;border-radius:8px;font-size:13px;display:none;z-index:50}
 a{color:#2563eb}
</style></head><body>
<header><h1>CICT 运维控制台</h1>
 <span class="badge" id="mode"></span><span class="badge" id="root"></span>
 <span class="badge" id="token-state"></span>
 <span class="badge" id="ver" style="margin-left:auto"></span></header>
<nav><button class="active" data-tab="overview">① 概览</button>
 <button data-tab="cfg">② 配置</button><button data-tab="upgrade">③ 升级</button>
 <button data-tab="status">④ 服务</button><button data-tab="logs">⑤ 日志</button></nav>
<main>

<section id="tab-overview">
 <div class="card"><h2>部署信息</h2><div class="grid" id="ov-deploy"></div>
  <div style="display:flex;gap:10px;margin-top:14px;flex-wrap:wrap">
   <button class="btn" onclick="loadOverview()">刷新概览</button>
   <button class="btn" onclick="doVerify()">健康验证</button>
   <span class="hint" id="docker-info" style="display:inline;margin:0"></span></div>
  <table id="vtab" style="margin-top:14px;display:none"><thead><tr><th>检查项</th><th style="width:80px">状态</th><th>详情</th></tr></thead><tbody></tbody></table>
 </div>
 <div class="card"><h2>访问地址总表</h2><table id="ov-urls"><thead><tr><th>名称</th><th>地址</th><th>说明</th></tr></thead><tbody></tbody></table></div>
 <div class="card"><h2>服务状态</h2>
  <table id="stab"><thead><tr><th>服务</th><th>容器</th><th style="width:90px">状态</th><th>健康</th><th>重启</th><th>已运行</th><th>端口映射</th></tr></thead><tbody></tbody></table>
  <table id="ptab" style="margin-top:10px"><thead><tr><th>端口检测</th><th>地址</th><th style="width:90px">状态</th></tr></thead><tbody></tbody></table>
  <div class="hint">显示"容器不存在"表示尚未安装或容器未创建。</div></div>
</section>

<section id="tab-cfg" style="display:none">
 <div class="card"><h2>部署参数（写入 .env）</h2><div class="grid" id="params"></div></div>
 <div class="card"><h2>密码（留空的项保持现有值不动）</h2>
  <div class="grid" id="secrets"></div>
  <button class="mini" onclick="fillRandomAll()">全部生成随机值</button>
  <button class="mini" onclick="toggleShow()">显示/隐藏密码</button>
  <div class="warnbox" id="dollar-warn">密码包含 $：Compose 的 .env 中需写成 $$，建议改用随机十六进制值</div>
  <div class="warnbox" id="env-invalid"></div>
 </div>
 <div class="card"><h2>部署目录配置文件（docker-compose / nginx / application.properties）</h2>
  <div id="tfiles"></div>
  <button class="btn" onclick="saveTargetFiles()">保存配置文件</button>
  <div class="hint">保存会先做 <code>docker compose config</code> / <code>nginx -t</code> 校验；改完还需要点下方「应用变更」让容器重建。</div>
 </div>
 <div class="card"><h2>应用变更</h2>
  <button class="btn green" onclick="applyChanges()">应用变更（docker compose up -d）</button>
  <div class="hint">会重建配置有变化的容器（改 .env / 端口 / application.properties 后点这里）。
   注意：<b>重启容器不会重读 .env</b>——改 .env 必须走「应用变更」。任务输出见页面底部抽屉。</div>
 </div>
 <button class="btn" onclick="saveEnv()">保存 .env</button>
 <div class="hint">保存 .env 只写文件（权限 600），不重建容器；要生效点「应用变更」。</div>
</section>

<section id="tab-upgrade" style="display:none">
 <div class="card"><h2>独立升级后端（JAR）</h2>
  <div class="up">选择新的后端 JAR 文件（Spring Boot 可执行 jar）：
   <input type="file" id="jar-file" accept=".jar"></div>
  <div class="field" style="max-width:420px"><label>新版本标签（写入镜像 tag，留空则用日期时间自动生成）</label>
   <input id="jar-tag" type="text" placeholder="如 20261111-1"></div>
  <button class="btn" onclick="upgradeBackend()">上传并升级后端</button>
  <div class="hint">流程：替换 <code>&lt;部署目录&gt;/cict/app.jar</code>（旧包自动备份，保留 3 份）→ 重建镜像 → 重建 cict 容器。构建约 1-3 分钟，输出见底部抽屉。</div>
 </div>
 <div class="card"><h2>独立升级前端</h2>
  <div class="up">选择前端产物 zip（根目录或 dist/ 下需含 index.html）：
   <input type="file" id="fe-file" accept=".zip"></div>
  <button class="btn" onclick="upgradeFrontend()">上传并升级前端</button>
  <div class="hint">同步到 <code>nginx/html</code>（bind mount 实时生效，无需重启）；完成后浏览器 Ctrl+F5 强刷。</div>
 </div>
 <div class="card"><h2>完整安装 / 重新部署</h2>
  <div class="grid" id="online-box" style="display:none"></div>
  <label style="font-size:13px"><input type="checkbox" id="skip-engine"> 已安装 Docker，跳过 Engine 安装（--skip-engine）</label>
  <div class="grid" style="margin-top:12px">
   <div class="field"><label>Docker apt 源基地址（留空自动探测）</label>
    <input id="k:docker_apt_mirror" type="text" placeholder="https://mirrors.aliyun.com/docker-ce"></div>
   <div class="field"><label>镜像加速器（留空自动探测）</label>
    <input id="k:registry_mirror" type="text" placeholder="https://<你的镜像加速地址>"></div>
  </div><br>
  <button class="btn orange" onclick="runInstall()">执行安装/升级</button>
  <span class="hint" id="runstate" style="display:inline;margin-left:12px"></span>
  <div class="hint">与命令行重跑 install.sh 等价：保留已有密码与数据，支持只填 JAR 或只填前端实现单项升级（留空的项不动）。</div>
 </div>
 <div class="card"><h2>更新本控制台</h2>
  <div class="hint" id="upd-state">当前版本: <b id="upd-cur"></b></div>
  <div style="display:flex;gap:10px;margin-top:10px;flex-wrap:wrap">
   <button class="btn" onclick="updateCheck()">检查更新（GitHub）</button>
   <button class="btn green" id="upd-github-btn" style="display:none" onclick="updateApplyGithub()">一键更新到最新版</button>
  </div>
  <div class="up" style="margin-top:14px">内网/离线环境：上传新版 cict-installer.run 自更新：
   <input type="file" id="run-file" accept=".run">
   <button class="btn" onclick="updateApplyUpload()">上传并更新</button></div>
  <div class="hint">更新只覆盖控制台自身文件（gui/bundle/compose 模板），不动部署目录、.env 密码与访问令牌。</div>
 </div>
</section>

<section id="tab-status" style="display:none">
 <div class="card"><h2>服务控制</h2>
  <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
   <button class="btn" onclick="loadServices()">立即刷新</button>
   <button class="btn green" onclick="applyChanges()">应用配置变更</button>
   <label style="font-size:13px"><input type="checkbox" id="auto-refresh" onchange="toggleAuto()"> 每 5 秒自动刷新</label>
  </div>
  <table id="ctab" style="margin-top:14px"><thead><tr><th>服务</th><th>容器</th><th style="width:90px">状态</th><th>健康</th><th>重启</th><th>已运行</th><th style="width:230px">操作</th></tr></thead><tbody></tbody></table>
  <div class="hint">「重启」= <code>docker compose restart</code>（Java 进程重启，<b>不重读 .env</b>）；改配置后请点「应用配置变更」。
   启停操作互不干扰其他组件；cict 依赖的 mariadb/redis/rabbitmq/rustfs 停止后 cict 会报错，属正常。</div>
 </div>
 <div class="card"><h2>端口监听</h2><table id="ptab2"><thead><tr><th>端口检测</th><th>地址</th><th style="width:90px">状态</th></tr></thead><tbody></tbody></table></div>
</section>

<section id="tab-logs" style="display:none">
 <div class="card"><h2>容器日志</h2>
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
   <select id="log-svc"></select>
   <select id="log-lines"><option>100</option><option selected>300</option><option>1000</option><option>2000</option></select>
   <button class="btn" onclick="loadLogs()">加载日志</button>
   <label style="font-size:13px"><input type="checkbox" id="log-follow" onchange="toggleFollow()"> 跟随（每 2 秒刷新）</label>
   <span class="hint" id="log-meta"></span>
  </div>
  <pre id="svclog" style="margin-top:12px">选择服务后点击"加载日志"。</pre>
  <div class="hint">来自 <code>docker logs --tail</code>（最多 2000 行），只读。跟随模式 = 每 2 秒重新拉取尾部。</div>
 </div>
</section>

</main>
<div id="drawer"><div class="bar" onclick="toggleDrawerBody()">
 <b>任务输出</b><span id="job-state" class="hint" style="color:#9ca3af;margin:0"></span>
 <span style="margin-left:auto" id="drawer-caret">收起 ▲</span></div>
 <pre id="joblog" style="display:none"></pre></div>
<div id="gate"><div class="box"><h3 style="margin:0 0 10px;font-size:15px">访问令牌</h3>
 <div class="field"><label>请输入 install.sh 输出的 token（或用带 ?token= 的 URL 访问）</label>
 <input id="gate-input" type="password"></div>
 <button class="btn" style="margin-top:10px" onclick="saveGateToken()">进入</button></div></div>
<div id="toast"></div>
<script>
let CFG=null, logFrom=0, timer=null, svcTimer=null, followTimer=null;
const $=id=>document.getElementById(id);
const TOKEN_KEY='cict_ops_token';
const qs=new URLSearchParams(location.search);
if(qs.get('token'))localStorage.setItem(TOKEN_KEY,qs.get('token'));
function tk(){return localStorage.getItem(TOKEN_KEY)||''}
function showGate(){$('gate').style.display='flex'}
function saveGateToken(){localStorage.setItem(TOKEN_KEY,$('gate-input').value.trim());location.search='';location.reload()}
async function api(path,opts={}){
 const headers=Object.assign({},opts.headers||{});
 if(tk())headers['X-Token']=tk();
 const r=await fetch(path,Object.assign({},opts,{headers:headers}));
 if(r.status===401){showGate();throw new Error('unauthorized')}
 return r}
function toast(m,ok=true){const t=$('toast');t.textContent=m;t.style.background=ok?'#059669':'#dc2626';t.style.display='block';setTimeout(()=>t.style.display='none',3200)}
const RANDOM_BYTES={MARIADB_ROOT_PASSWORD:24,MARIADB_PASSWORD:24,REDIS_PASSWORD:24,RABBITMQ_DEFAULT_PASS:24,RABBITMQ_ADMIN_PASSWORD:24,RUSTFS_ACCESS_KEY:16,RUSTFS_SECRET_KEY:32};
function rnd(n){const a=new Uint8Array(n);crypto.getRandomValues(a);return[...a].map(b=>b.toString(16).padStart(2,'0')).join('')}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('nav button').forEach(x=>x.classList.remove('active'));b.classList.add('active');
 ['overview','cfg','upgrade','status','logs'].forEach(t=>$('tab-'+t).style.display=t===b.dataset.tab?'':'none');
 if(b.dataset.tab==='overview')loadOverview();
 if(b.dataset.tab==='status')loadServices();});
function field(k,label,val,secret){return `<div class="field"><label>${label}</label><div class="row">
 <input id="k:${k}" type="${secret?'password':'text'}" value="${String(val).replace(/"/g,'&quot;')}" autocomplete="off">
 ${secret?`<button class="mini" onclick="fillRandom('${k}')">随机</button>`:''}</div></div>`}
function fillRandom(k){$('k:'+k).value=rnd(RANDOM_BYTES[k]||24);bindInputs()}
function fillRandomAll(){for(const k in RANDOM_BYTES)fillRandom(k)}
function toggleShow(){document.querySelectorAll('#secrets input').forEach(i=>i.type=i.type==='password'?'text':'password')}
function collect(){const o={};document.querySelectorAll('input[id^="k:"]').forEach(i=>o[i.id.slice(2)]=i.value.trim());return o}
function bindInputs(){
 const has$=Object.values(collect()).some(v=>v.includes('$'));$('dollar-warn').style.display=has$?'block':'none';
 const bad=[];
 const RULES={CICT_PUBLIC_S3_ENDPOINT:v=>v===''||/^https?:\/\//.test(v),
  NGINX_PORT:portRule,CICT_PORT:portRule,MARIADB_PORT:portRule,RABBIT_MGMT:portRule,
  RABBITMQ_MGMT_PORT:portRule,RUSTFS_PORT:portRule,RUSTFS_CONSOLE_PORT:portRule};
 function portRule(v){return /^\d+$/.test(v)&&+v>0&&+v<65536}
 for(const k in RULES){const el=$('k:'+k);if(!el)continue;
  if(RULES[k](el.value.trim())){el.classList.remove('bad')}else{el.classList.add('bad');bad.push(k)}}
 $('env-invalid').style.display=bad.length?'block':'none';
 $('env-invalid').textContent=bad.length?('以下配置项取值不合法（红框标注，保存 .env 会被拒绝）: '+bad.join(', ')):''}
async function load(){
 CFG=await (await api('/api/config')).json();
 $('mode').textContent=CFG.mode==='online'?'在线模式':'离线模式';
 $('mode').className='badge '+(CFG.mode==='online'?'ok':'warn');
 $('root').textContent=CFG.is_root?'root 运行':'非 root（无法执行安装）';
 $('root').className='badge '+(CFG.is_root?'ok':'warn');
 $('token-state').textContent=CFG.token_configured?'已启用令牌':'未启用令牌';
 $('token-state').className='badge '+(CFG.token_configured?'ok':'warn');
 $('ver').textContent='控制台 '+CFG.console_version;
 $('upd-cur').textContent=CFG.console_version;
 const p=$('params'),s=$('secrets');p.innerHTML='';s.innerHTML='';
 for(const[k,label,val]of CFG.params){if(k==='CICT_IMAGE_TAG')continue;
  p.insertAdjacentHTML('beforeend',field(k,label,val,false));}
 for(const[k,label,val]of CFG.secrets){s.insertAdjacentHTML('beforeend',field(k,label,val,true));}
 if(CFG.mode==='online'){const o=$('online-box');o.style.display='grid';
  o.innerHTML=field('app_jar','后端 JAR 路径（服务器上的路径，留空=不升级后端）',CFG.app_jar||'',false)
   +field('frontend','前端产物路径（zip 或目录，留空=不升级前端）',CFG.frontend||'',false)
   +field('tag','版本号（升级后端时必填）',CFG.tag||'',false);}
 $('log-svc').innerHTML=[['mariadb','MariaDB'],['redis','Redis'],['rabbitmq','RabbitMQ'],
  ['rustfs','RustFS'],['cict','CICT 后端'],['nginx','Nginx']].map(([v,l])=>`<option value="${v}">${l}</option>`).join('');
 bindInputs();loadTargetFiles();refreshJob();if(!timer)startPoll();}
async function saveEnv(){
 if(document.querySelectorAll('input.bad').length)return toast('有红框标注的不合法取值，请先修正',false);
 const body={target_dir:collect().target_dir||'',values:collect()};
 const r=await(await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
 if(!r.ok)return toast('保存失败: '+r.error,false);
 toast('已保存 .env'+(r.warnings?'，警告: '+r.warnings:'')+'；要点生效请到②配置页底部或④服务页点「应用变更」')}
let TFILES={};
async function loadTargetFiles(){
 const r=await(await api('/api/target-files')).json();if(!r.ok)return;
 const box=$('tfiles');box.innerHTML='';TFILES={};
 for(const f of r.files){
  TFILES[f.path]=f.content;
  box.insertAdjacentHTML('beforeend',
   `<details><summary>${f.label}${f.exists?'':'（文件不存在，新建）'}</summary>
    <textarea id="tf:${f.path}" oninput="markMod('${f.path}')">${f.content.replace(/</g,'&lt;')}</textarea></details>`)}}
function markMod(p){const ta=$('tf:'+p);ta.classList.toggle('mod',ta.value!==TFILES[p])}
async function saveTargetFiles(){
 const files={};const changed=[];
 document.querySelectorAll('textarea[id^="tf:"]').forEach(t=>{const p=t.id.slice(3);
  if(t.value!==TFILES[p]){files[p]=t.value;changed.push(p)}});
 if(!changed.length)return toast('没有检测到改动');
 if(!confirm('将写入以下文件:\n'+changed.join('\n')+'\n确认保存?'))return;
 const r=await(await api('/api/target-files',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({target_dir:'',files:files})})).json();
 if(!r.ok)return toast('保存失败: '+String(r.error).split('\n')[0],false);
 toast('配置文件已保存'+(r.saved?(' ('+r.saved.join(', ')+')'):'')+(r.note?'；'+r.note:''));
 loadTargetFiles()}
async function applyChanges(){
 if(!confirm('应用变更将执行 docker compose up -d，重建配置有变化的容器（其余容器不受影响）。继续?'))return;
 const r=await(await api('/api/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})).json();
 if(!r.ok)return toast('无法应用: '+String(r.error).split('\n')[0],false);
 toast('已启动应用变更'+(r.preview?('\n'+r.preview):''));logFrom=0;$('joblog').textContent='';openDrawer();startPoll()}
async function runInstall(){
 const v=collect();
 const body={skip_engine:$('skip-engine').checked,target_dir:v.target_dir||'',
  app_jar:v.app_jar||'',frontend:v.frontend||'',tag:v.tag||'',
  docker_apt_mirror:v.docker_apt_mirror||'',registry_mirror:v.registry_mirror||''};
 const r=await(await api('/api/install',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
 if(!r.ok)return toast('无法启动: '+r.error,false);
 toast('安装/升级已启动');logFrom=0;$('joblog').textContent='';openDrawer();startPoll()}
async function upgradeBackend(){
 const f=$('jar-file').files[0];if(!f)return toast('请先选择 JAR 文件',false);
 const tag=$('jar-tag').value.trim();
 const r=await(await api('/api/upgrade/backend?tag='+encodeURIComponent(tag),{method:'POST',body:f})).json();
 if(!r.ok)return toast('升级失败: '+r.error,false);
 toast('JAR 已上传，开始构建镜像');logFrom=0;$('joblog').textContent='';openDrawer();startPoll()}
async function upgradeFrontend(){
 const f=$('fe-file').files[0];if(!f)return toast('请先选择前端 zip',false);
 toast('上传同步中...',true);
 const r=await(await api('/api/upgrade/frontend',{method:'POST',body:f})).json();
 toast(r.ok?r.note:'失败: '+r.error,r.ok)}
async function updateCheck(){
 $('upd-state').textContent='检查中...';
 const r=await(await api('/api/update/check')).json();
 if(!r.ok){$('upd-state').textContent='检查失败: '+r.error;$('upd-github-btn').style.display='none';return}
 $('upd-state').innerHTML=r.up_to_date?('当前版本 <b>'+r.current+'</b> 已是最新（'+r.latest+'）')
  :('当前版本 <b>'+r.current+'</b>，最新版本 <b>'+r.latest+'</b>');
 $('upd-github-btn').style.display=r.up_to_date?'none':'inline-block';
 $('upd-github-btn').dataset.url=r.asset_url||''}
async function updateApplyGithub(){
 const url=$('upd-github-btn').dataset.url;if(!url)return toast('未获取到下载地址，请先检查更新',false);
 const r=await(await api('/api/update/apply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode:'github',url:url})})).json();
 if(!r.ok)return toast('更新失败: '+r.error,false);
 toast(r.note);setTimeout(()=>location.reload(),4000)}
async function updateApplyUpload(){
 const f=$('run-file').files[0];if(!f)return toast('请先选择 cict-installer.run',false);
 toast('上传更新包中...',true);
 const r=await(await api('/api/update/apply',{method:'POST',body:f})).json();
 if(!r.ok)return toast('更新失败: '+r.error,false);
 toast(r.note);setTimeout(()=>location.reload(),5000)}
function startPoll(){if(timer)clearInterval(timer);timer=setInterval(refreshJob,1200)}
async function refreshJob(){
 let s;try{s=await(await api('/api/status?since='+logFrom)).json()}catch(e){return}
 if(s.lines.length){const log=$('joblog');s.lines.forEach(l=>log.textContent+=l+'\n');log.scrollTop=log.scrollHeight;logFrom+=s.lines.length}
 const st=s.running?'运行中...':(s.exit_code===null?'空闲':(s.exit_code===0?'已完成 ✓':'失败（退出码 '+s.exit_code+'）'));
 $('job-state').textContent=st;
 $('runstate').textContent=s.running?'任务运行中...':st;
 if(!s.running&&timer&&followTimer===null){clearInterval(timer);timer=null}}
function openDrawer(){$('drawer').classList.add('open');$('joblog').style.display='block';$('drawer-caret').textContent='收起 ▲'}
function toggleDrawerBody(){const p=$('joblog');const show=p.style.display==='none';p.style.display=show?'block':'none';$('drawer-caret').textContent=show?'收起 ▲':'展开 ▼'}
const MONO='font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px';
async function doVerify(){
 const r=await(await api('/api/verify',{method:'POST'})).json();
 $('vtab').style.display='table';const tb=$('vtab').querySelector('tbody');tb.innerHTML='';
 for(const c of r.results)tb.insertAdjacentHTML('beforeend',
  `<tr><td>${c.name}</td><td class="st ${c.ok?'ok':'bad'}">${c.ok?'✓ 通过':'✗ 失败'}</td><td>${c.detail}</td></tr>`)}
async function loadOverview(){
 let r;try{r=await(await api('/api/overview')).json()}catch(e){return}
 $('docker-info').textContent=r.docker?(r.docker_error?r.docker+' — '+r.docker_error:r.docker):('Docker 不可用: '+(r.docker_error||''));
 $('ov-deploy').innerHTML=
  `<div class="field"><label>部署目录</label><input type="text" value="${r.deploy.target_dir}" readonly></div>
   <div class="field"><label>后端镜像版本</label><input type="text" value="${r.deploy.image_tag}" readonly></div>
   <div class="field"><label>上次安装时间</label><input type="text" value="${r.deploy.last_install_at}" readonly></div>
   <div class="field"><label>上次安装模式</label><input type="text" value="${r.deploy.mode}" readonly></div>`;
 const ut=$('ov-urls').querySelector('tbody');ut.innerHTML='';
 for(const u of r.urls)ut.insertAdjacentHTML('beforeend',
  `<tr><td>${u.name}</td><td style="${MONO}"><a href="${u.url}" target="_blank">${u.url}</a></td><td>${u.note}</td></tr>`);
 renderServiceTables(r)}
function renderServiceTables(r){
 const missing=r.docker&&r.docker_error?'无法查询':'容器不存在';
 const tb=$('ctab')?$('ctab').querySelector('tbody'):null;if(tb){tb.innerHTML='';
  for(const s of r.services){const run=s.state==='running';
   tb.insertAdjacentHTML('beforeend',
   `<tr><td>${s.label}</td><td style="${MONO}">${s.container}</td>
    <td class="st ${run?'ok':'bad'}">${run?'运行中':(s.state||missing)}</td>
    <td>${s.health||'-'}</td><td>${s.restarts||'-'}</td><td>${s.uptime||'-'}</td>
    <td><button class="mini" onclick="svcAction('${s.service}','start')">启动</button>
     <button class="mini danger" onclick="svcAction('${s.service}','stop')">停止</button>
     <button class="mini" onclick="svcAction('${s.service}','restart')">重启</button></td></tr>`)}}
 for(const id of ['stab','ptab','ptab2']){const el=$(id);if(!el)continue;
  if(el.id==='stab'){const t=el.querySelector('tbody');t.innerHTML='';
   for(const s of r.services){const run=s.state==='running';
    t.insertAdjacentHTML('beforeend',
    `<tr><td>${s.label}</td><td style="${MONO}">${s.container}</td>
     <td class="st ${run?'ok':'bad'}">${run?'运行中':(s.state||missing)}</td>
     <td>${s.health||'-'}</td><td>${s.restarts||'-'}</td><td>${s.uptime||'-'}</td>
     <td style="${MONO}">${s.ports.length?s.ports.join('<br>'):'-'}</td></tr>`)}}
  else{const t=el.querySelector('tbody');t.innerHTML='';
   for(const p of r.port_checks)t.insertAdjacentHTML('beforeend',
    `<tr><td>${p.name}</td><td style="${MONO}">${p.bind}:${p.port}</td>
     <td class="st ${p.ok?'ok':'bad'}">${p.ok?'监听中':'未监听'}</td></tr>`)}}}
async function loadServices(){
 let r;try{r=await(await api('/api/services')).json()}catch(e){return}
 $('docker-info').textContent=r.docker?(r.docker_error?r.docker+' — '+r.docker_error:r.docker):('Docker 不可用: '+(r.docker_error||''));
 renderServiceTables(r)}
async function svcAction(svc,act){
 const zh={start:'启动',stop:'停止',restart:'重启'}[act];
 if(act==='stop'&&!confirm(`确认停止 ${svc}？依赖它的服务会报错。`))return;
 if(act==='restart'&&!confirm(`确认重启 ${svc}？（改 .env 后重启不生效，需「应用配置变更」）`))return;
 const r=await(await api('/api/service-action',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({service:svc,action:act})})).json();
 if(!r.ok)return toast('操作失败: '+r.error,false);
 toast(zh+' '+svc+' 已发起');logFrom=0;$('joblog').textContent='';openDrawer();startPoll();
 setTimeout(loadServices,3000)}
function toggleAuto(){
 if($('auto-refresh').checked){loadServices();svcTimer=setInterval(loadServices,5000)}
 else if(svcTimer){clearInterval(svcTimer);svcTimer=null}}
async function loadLogs(){
 const svc=$('log-svc').value||'mariadb',lines=$('log-lines').value;
 $('log-meta').textContent='加载中...';$('svclog').textContent='';
 const r=await(await api('/api/logs?service='+encodeURIComponent(svc)+'&lines='+lines)).json();
 if(!r.ok){$('svclog').textContent='加载失败: '+r.error;$('log-meta').textContent='';return}
 $('svclog').textContent=r.log||'（暂无日志输出）';
 $('log-meta').textContent=r.container+' 最近 '+lines+' 行';
 $('svclog').scrollTop=$('svclog').scrollHeight}
function toggleFollow(){
 if($('log-follow').checked){loadLogs();followTimer=setInterval(loadLogs,2000)}
 else if(followTimer){clearInterval(followTimer);followTimer=null}}
load();
</script></body></html>"""

PAGE = HTML


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默访问日志
        pass

    # ---- 基础响应 ----
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forbidden(self):
        self._json({"error": "unauthorized: 缺少或错误的访问令牌（X-Token 头或 ?token=）"}, 401)

    def _check_token(self):
        if not SERVER_TOKEN:
            return True
        supplied = self.headers.get("X-Token", "")
        if not supplied:
            query = parse_qs(urlparse(self.path).query)
            supplied = (query.get("token") or [""])[0]
        return bool(supplied) and secrets.compare_digest(supplied, SERVER_TOKEN)

    def stream_body_to_file(self, cap):
        """把请求体流式写入临时文件（大文件不进内存）；返回 (path, None) 或 (None, 错误)。"""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            return None, "非法的 Content-Length"
        if length <= 0:
            return None, "空请求体"
        if length > cap:
            return None, f"文件过大（{length >> 20}MB，上限 {cap >> 20}MB）"
        tmp = tempfile.NamedTemporaryFile(prefix="cict-upload-", delete=False)
        try:
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                tmp.write(chunk)
                remaining -= len(chunk)
            if remaining > 0:
                return None, "请求体不完整"
            tmp.close()
            return tmp.name, None
        except Exception as exc:  # noqa: BLE001
            tmp.close()
            os.unlink(tmp.name)
            return None, str(exc)

    def read_json_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        return json.loads(self.rfile.read(length) or b"{}")

    # ---- 路由 ----
    def do_GET(self):
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        if path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif not self._check_token():
            self._forbidden()
        elif path == "/api/config":
            self._json(self.get_config())
        elif path == "/api/overview":
            self._json(get_overview(current_target((query.get("target_dir") or [None])[0])))
        elif path.startswith("/api/status"):
            try:
                since = int((query.get("since") or ["0"])[0] or 0)
            except ValueError:
                since = 0
            with JOB.lock:
                lines = list(JOB.lines)[since:]
                self._json({"running": JOB.running, "exit_code": JOB.exit_code, "lines": lines})
        elif path.startswith("/api/services"):
            self._json(get_services(current_target((query.get("target_dir") or [None])[0])))
        elif path.startswith("/api/logs"):
            try:
                lines = int((query.get("lines") or ["300"])[0])
            except ValueError:
                lines = 300
            self._json(get_logs((query.get("service") or [""])[0], lines))
        elif path == "/api/target-files":
            self._json(target_files_read(current_target((query.get("target_dir") or [None])[0])))
        elif path == "/api/update/check":
            self._json(update_check())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        if not self._check_token():
            # 上传请求的 body 也要消费掉，避免连接错位
            try:
                length = int(self.headers.get("Content-Length", 0))
                while length > 0:
                    chunk = self.rfile.read(min(length, 1 << 20))
                    if not chunk:
                        break
                    length -= len(chunk)
            except Exception:  # noqa: BLE001
                pass
            self._forbidden()
            return

        if path == "/api/config":
            self._json(self.save_config(self.read_json_body()))
        elif path == "/api/verify":
            self._json({"results": run_verify(current_target((query.get("target_dir") or [None])[0]))})
        elif path == "/api/target-files":
            payload = self.read_json_body()
            target = payload.get("target_dir") or current_target()
            self._json(target_files_save(target, payload.get("files", {})))
        elif path == "/api/apply":
            payload = self.read_json_body()
            self._json(apply_changes(payload.get("target_dir") or current_target()))
        elif path == "/api/service-action":
            payload = self.read_json_body()
            self._json(service_action(payload.get("target_dir") or current_target(),
                                      payload.get("service", ""), payload.get("action", "")))
        elif path == "/api/install":
            self._json(self.start_install(self.read_json_body()))
        elif path == "/api/upgrade/backend":
            tag = (query.get("tag") or [""])[0]
            target = current_target((query.get("target_dir") or [None])[0])
            temp, err = self.stream_body_to_file(MAX_UPLOAD["backend"])
            if err:
                self._json({"ok": False, "error": err})
                return
            try:
                if not tag:
                    tag = datetime.now().strftime("%Y%m%d-%H%M%S")
                self._json(upgrade_backend(target, temp, new_tag=tag))
            finally:
                os.unlink(temp)
        elif path == "/api/upgrade/frontend":
            target = current_target((query.get("target_dir") or [None])[0])
            temp, err = self.stream_body_to_file(MAX_UPLOAD["frontend"])
            if err:
                self._json({"ok": False, "error": err})
                return
            try:
                self._json(upgrade_frontend(target, temp))
            finally:
                os.unlink(temp)
        elif path == "/api/update/apply":
            content_type = self.headers.get("Content-Type", "")
            if "application/json" in content_type:
                payload = self.read_json_body()
                if payload.get("mode") == "github":
                    self._json(update_apply_github(payload.get("url", "")))
                else:
                    self._json({"ok": False, "error": "未知更新模式"})
            else:
                temp, err = self.stream_body_to_file(MAX_UPLOAD["update"])
                if err:
                    self._json({"ok": False, "error": err})
                    return
                try:
                    self._json(update_apply_file(temp))
                finally:
                    os.unlink(temp)
        else:
            self._json({"error": "not found"}, 404)

    # ---- 业务 ----
    def get_config(self):
        target = current_target()
        template = (SRC / ".env.example").read_text()
        env = parse_env(template)
        target_env_path = Path(target) / ".env"
        if target_env_path.is_file():
            env.update({k: v for k, v in parse_env(target_env_path.read_text()).items()})
        params, secrets = [], []
        for k, v in env.items():
            label = FIELD_LABELS.get(k, k)
            (secrets if k in SECRET_KEYS else params).append([k, label, v])
        params.insert(0, ["target_dir", "部署目录", target])
        return {
            "mode": MODE, "is_root": os.geteuid() == 0 if hasattr(os, "geteuid") else True,
            "bundle_tag": BUNDLE_TAG, "params": params, "secrets": secrets,
            "console_version": CONSOLE_VERSION, "token_configured": bool(SERVER_TOKEN),
            "app_jar": UI_STATE["app_jar"],
            "frontend": UI_STATE["frontend"],
            "tag": "",
        }

    def save_config(self, payload):
        warnings = []
        values = payload.get("values", {})
        target = payload.get("target_dir") or current_target()
        if not target.startswith("/"):
            return {"ok": False, "error": "部署目录必须是绝对路径"}
        # 取值校验：把线上踩过的坑拦在保存前
        for key, (desc, check) in ENV_VALIDATORS.items():
            val = values.get(key)
            if val is not None and not check(val):
                return {"ok": False, "error": f"{key} 取值不合法: {desc}（当前: {val!r}）"}
        for k, v in values.items():
            if k in SECRET_KEYS and "$" in v:
                warnings.append(f"{k} 含 $")
        existing = target_env(target)
        tag = values.get("tag") or existing.get("CICT_IMAGE_TAG") or BUNDLE_TAG \
            or parse_env((SRC / ".env.example").read_text()).get("CICT_IMAGE_TAG", "")
        if MODE == "online" and not existing and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", tag or ""):
            return {"ok": False, "error": "首次部署需要有效的唯一版本号"}
        try:
            template = (SRC / ".env.example").read_text()
            content = render_env(template, values, tag)
            target_dir = Path(target)
            target_dir.mkdir(parents=True, exist_ok=True)
            env_path = target_dir / ".env"
            env_path.write_text(content, encoding="utf-8")
            os.chmod(env_path, 0o600)
            # 记住 JAR/前端路径，安装页回显
            UI_STATE["app_jar"] = values.get("app_jar", "")
            UI_STATE["frontend"] = values.get("frontend", "")
            return {"ok": True, "warnings": "；".join(warnings)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def start_install(self, payload):
        if os.geteuid() != 0:
            return {"ok": False, "error": "本程序未以 sudo/root 运行，install.sh 需要 root"}
        if JOB.running:
            return {"ok": False, "error": "已有任务在运行"}
        target = payload.get("target_dir") or current_target()
        cmd = [str(INSTALL_SH), "--target-dir", target]
        if payload.get("skip_engine"):
            cmd.append("--skip-engine")
        values = target_env(target)
        if MODE == "online":
            app_jar = payload.get("app_jar") or ""
            frontend = payload.get("frontend") or ""
            tag = payload.get("tag") or values.get("CICT_IMAGE_TAG", "")
            if not (app_jar or frontend):
                return {"ok": False, "error": "在线模式需要提供 JAR 路径和/或前端路径（完整安装两者都要）"}
            if app_jar and not tag:
                return {"ok": False, "error": "升级后端需要版本号（tag）"}
            cmd.append("--online")
            if app_jar:
                cmd += ["--app-jar", app_jar]
            if frontend:
                cmd += ["--frontend", frontend]
            if app_jar:
                cmd += ["--tag", tag]
        if not INSTALL_SH.is_file():
            return {"ok": False, "error": f"找不到 {INSTALL_SH}"}
        if payload.get("docker_apt_mirror"):
            cmd += ["--docker-apt-mirror", payload["docker_apt_mirror"]]
        if payload.get("registry_mirror"):
            cmd += ["--registry-mirror", payload["registry_mirror"]]
        return {"ok": JOB.start(cmd, label="install.sh"), "cmd": " ".join(cmd)}


def main():
    print(f"CICT 运维控制台 | 模式: {MODE} | 版本: {CONSOLE_VERSION} | http://127.0.0.1:{PORT}")
    print("远程使用请先建立 SSH 隧道: ssh -L 8788:127.0.0.1:8788 <用户>@<服务器IP>")
    if not SERVER_TOKEN:
        print("警告: 未配置访问令牌（gui/console.env 缺失）。作为常驻 systemd 服务运行前请先配置！")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
