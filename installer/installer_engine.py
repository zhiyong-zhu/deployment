"""Auditable Ubuntu 24 direct-install task adapters for CICT."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import platform
import re
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


REDIS_VERSION = "7.4.10"
REDIS_SHA256 = "669ab6689b5e7d0c479e8d526ccbadae36b69a11370742ffe23822b9df8d85ba"
NGINX_VERSION = "1.28.3"
NGINX_SHA256 = "2c96a946bfb0882a21744ed429770a2123ae1828c7c48665092993ddee91a918"
RUSTFS_VERSION = "1.0.0-rc.2"
CACHE_DIR = Path("/var/cache/cict-installer")
REPORT_DIR = Path("/var/log/cict-installer")
PASSWORD_RE = re.compile(r"^[A-Za-z0-9._!@#%+=:-]{16,128}$")
ACCESS_KEY_RE = re.compile(r"^[A-Za-z0-9]{16,64}$")


class InstallError(RuntimeError):
    pass


@dataclass
class InstallContext:
    config: dict[str, Any]
    secrets: dict[str, str]
    log_callback: Callable[[str, str, str], None]
    progress_callback: Callable[[str, int, str], None]

    def log(self, task_id: str, message: str, level: str = "info") -> None:
        self.log_callback(task_id, message, level)

    def progress(self, task_id: str, value: int, message: str) -> None:
        self.progress_callback(task_id, max(0, min(100, value)), message)


def validate_payload(payload: dict[str, Any], execute: bool) -> tuple[dict[str, Any], dict[str, str]]:
    data_root = str(payload.get("data_root") or "/data/app").strip()
    if data_root != "/data/app":
        raise InstallError("当前直接安装版本固定使用 /data/app，不能修改数据根目录")

    config = {
        "server_name": clean_label(payload.get("server_name") or "cict-server"),
        "data_root": data_root,
        "backend_jar": str(payload.get("backend_jar") or "").strip(),
        "frontend_path": str(payload.get("frontend_path") or "").strip(),
    }
    secrets = {
        "db_password": str(payload.get("db_password") or ""),
        "rabbit_password": str(payload.get("rabbit_password") or ""),
        "rabbit_admin_password": str(payload.get("rabbit_admin_password") or ""),
        "rustfs_access_key": str(payload.get("rustfs_access_key") or ""),
        "rustfs_secret": str(payload.get("rustfs_secret") or ""),
    }

    if execute:
        backend = Path(config["backend_jar"])
        frontend = Path(config["frontend_path"])
        if not backend.is_file() or backend.suffix.lower() != ".jar":
            raise InstallError("后端 JAR 路径不存在或不是 .jar 文件")
        if not frontend.exists() or (frontend.is_file() and frontend.suffix.lower() != ".zip"):
            raise InstallError("前端路径必须是包含 index.html 的目录或 zip 文件")
        for key in ("db_password", "rabbit_password", "rabbit_admin_password", "rustfs_secret"):
            if not PASSWORD_RE.fullmatch(secrets[key]):
                raise InstallError(f"{key} 必须为 16-128 位，且只能使用字母、数字和 ._!@#%+=:-")
        if not ACCESS_KEY_RE.fullmatch(secrets["rustfs_access_key"]):
            raise InstallError("RustFS Access Key 必须为 16-64 位字母或数字")
        if secrets["rabbit_password"] == secrets["rabbit_admin_password"]:
            raise InstallError("RabbitMQ 应用账号与管理账号不能使用相同密码")
    return config, secrets


def clean_label(value: Any) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", str(value).strip())[:64]
    return cleaned or "cict-server"


def run_task(task_id: str, context: InstallContext) -> None:
    handlers = {
        "preflight": task_preflight,
        "repositories": task_repositories,
        "packages": task_packages,
        "mariadb": task_mariadb,
        "redis": task_redis,
        "rabbitmq": task_rabbitmq,
        "jdk": task_jdk,
        "nginx": task_nginx,
        "rustfs": task_rustfs,
        "backend": task_backend,
        "acceptance": task_acceptance,
    }
    try:
        handler = handlers[task_id]
    except KeyError as exc:
        raise InstallError(f"未知任务: {task_id}") from exc
    handler(context, task_id)


def run_command(
    context: InstallContext,
    task_id: str,
    args: list[str],
    *,
    cwd: Path | None = None,
    input_text: str | None = None,
    timeout: int = 1800,
    display: str | None = None,
) -> str:
    shown = display or " ".join(args)
    context.log(task_id, f"$ {shown}")
    env = os.environ.copy()
    env.update({"DEBIAN_FRONTEND": "noninteractive", "LC_ALL": "C.UTF-8"})
    process = subprocess.Popen(
        args,
        cwd=str(cwd) if cwd else None,
        env=env,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output: list[str] = []
    if input_text is not None and process.stdin:
        process.stdin.write(input_text)
        process.stdin.close()
    started = time.monotonic()
    assert process.stdout is not None
    while True:
        line = process.stdout.readline()
        if line:
            clean = line.rstrip()
            output.append(clean)
            if clean:
                context.log(task_id, clean[-1000:])
        elif process.poll() is not None:
            break
        elif time.monotonic() - started > timeout:
            process.kill()
            raise InstallError(f"命令执行超过 {timeout} 秒: {shown}")
    return_code = process.wait()
    if return_code != 0:
        tail = "\n".join(output[-12:])
        raise InstallError(f"命令失败（退出码 {return_code}）: {shown}\n{tail}")
    return "\n".join(output)


def write_file(path: Path, content: str, mode: int = 0o644, owner: str = "root", group: str = "root") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    os.chmod(temporary, mode)
    shutil.chown(temporary, user=owner, group=group)
    os.replace(temporary, path)


def ensure_user(context: InstallContext, task_id: str, user: str, home: str) -> None:
    result = subprocess.run(["id", "-u", user], capture_output=True, check=False)
    if result.returncode == 0:
        return
    run_command(
        context,
        task_id,
        ["useradd", "--system", "--home", home, "--shell", "/usr/sbin/nologin", user],
    )


def download(context: InstallContext, task_id: str, url: str, target: Path, expected_sha256: str | None = None) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if target.exists() and (not expected_sha256 or sha256_file(target) == expected_sha256):
        context.log(task_id, f"使用已校验缓存: {target.name}")
        return target
    request = urllib.request.Request(url, headers={"User-Agent": "CICT-Installer/1.0"})
    context.log(task_id, f"下载 {url}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response, target.open("wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                output.write(chunk)
                received += len(chunk)
                if total:
                    context.progress(task_id, min(55, 5 + int(received / total * 45)), f"下载 {target.name} {received * 100 // total}%")
    except (OSError, urllib.error.URLError) as exc:
        target.unlink(missing_ok=True)
        raise InstallError(f"下载失败: {url}: {exc}") from exc
    if expected_sha256:
        actual = sha256_file(target)
        if actual != expected_sha256:
            target.unlink(missing_ok=True)
            raise InstallError(f"{target.name} SHA-256 不匹配: {actual}")
        context.log(task_id, f"SHA-256 校验通过: {target.name}")
    return target


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_tar(archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            destination = (target / member.name).resolve()
            if target.resolve() not in destination.parents and destination != target.resolve():
                raise InstallError(f"压缩包包含非法路径: {member.name}")
        bundle.extractall(target, filter="data")


def task_preflight(context: InstallContext, task_id: str) -> None:
    context.progress(task_id, 10, "检查操作系统")
    if platform.system() != "Linux" or not Path("/etc/os-release").exists():
        raise InstallError("真实安装仅支持 Ubuntu 24.04 Linux")
    release = parse_os_release()
    if release.get("ID") != "ubuntu" or release.get("VERSION_ID") != "24.04":
        raise InstallError(f"需要 Ubuntu 24.04，当前为 {release.get('PRETTY_NAME', 'unknown')}")
    if os.geteuid() != 0:
        raise InstallError("真实安装器必须使用 sudo/root 启动")
    architecture = run_simple(["dpkg", "--print-architecture"])
    if architecture != "amd64":
        raise InstallError(f"当前版本的 RabbitMQ 软件源仅支持 amd64，检测到 {architecture}")
    context.log(task_id, f"系统: {release.get('PRETTY_NAME')} / {architecture}")
    context.progress(task_id, 35, "检查磁盘和内存")
    Path("/data").mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage("/data").free / (1024**3)
    memory_gb = total_memory_bytes() / (1024**3)
    context.log(task_id, f"可用磁盘 {free_gb:.1f} GB，物理内存 {memory_gb:.1f} GB")
    if free_gb < 12:
        raise InstallError("/data 可用空间低于 12 GB")
    if memory_gb < 3.5:
        context.log(task_id, "内存低于 4 GB，将采用较小的 MariaDB/JVM 参数", "warning")
    context.progress(task_id, 60, "检查端口")
    for port in (80, 3306, 5672, 6379, 9000, 9001, 9090, 15672):
        if port_open("127.0.0.1", port):
            context.log(task_id, f"端口 {port} 已有服务监听；安装器将按幂等方式检查现有服务", "warning")
    context.progress(task_id, 80, "检查网络")
    try:
        request = urllib.request.Request(
            f"https://download.redis.io/releases/redis-{REDIS_VERSION}.tar.gz",
            headers={"User-Agent": "CICT-Installer/1.0", "Range": "bytes=0-0"},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            context.log(task_id, f"在线软件源可访问（HTTP {response.status}）")
    except (OSError, urllib.error.URLError) as exc:
        context.log(task_id, f"在线软件源连通性预检未通过，将在下载步骤再次尝试: {exc}", "warning")
    context.progress(task_id, 100, "环境检查通过")


def task_repositories(context: InstallContext, task_id: str) -> None:
    context.progress(task_id, 5, "准备软件源密钥目录")
    Path("/etc/apt/keyrings").mkdir(parents=True, exist_ok=True)
    mariadb_key = CACHE_DIR / "mariadb_release_signing_key.asc"
    download(context, task_id, "https://mariadb.org/mariadb_release_signing_key.asc", mariadb_key)
    run_command(
        context,
        task_id,
        ["gpg", "--batch", "--yes", "--dearmor", "--output", "/etc/apt/keyrings/mariadb-keyring.gpg", str(mariadb_key)],
    )
    write_file(
        Path("/etc/apt/sources.list.d/mariadb.list"),
        "deb [arch=amd64 signed-by=/etc/apt/keyrings/mariadb-keyring.gpg] https://deb.mariadb.org/11.4/ubuntu noble main\n",
    )
    context.progress(task_id, 45, "配置 RabbitMQ 软件源")
    rabbit_key = CACHE_DIR / "rabbitmq-team.asc"
    download(
        context,
        task_id,
        "https://keys.openpgp.org/vks/v1/by-fingerprint/0A9AF2115F4687BD29803A206B73A36E6026DFCA",
        rabbit_key,
    )
    run_command(
        context,
        task_id,
        ["gpg", "--batch", "--yes", "--dearmor", "--output", "/usr/share/keyrings/com.rabbitmq.team.gpg", str(rabbit_key)],
    )
    write_file(
        Path("/etc/apt/sources.list.d/rabbitmq.list"),
        """deb [arch=amd64 signed-by=/usr/share/keyrings/com.rabbitmq.team.gpg] https://deb1.rabbitmq.com/rabbitmq-erlang/ubuntu/noble noble main
deb [arch=amd64 signed-by=/usr/share/keyrings/com.rabbitmq.team.gpg] https://deb2.rabbitmq.com/rabbitmq-erlang/ubuntu/noble noble main
deb [arch=amd64 signed-by=/usr/share/keyrings/com.rabbitmq.team.gpg] https://deb1.rabbitmq.com/rabbitmq-server/ubuntu/noble noble main
deb [arch=amd64 signed-by=/usr/share/keyrings/com.rabbitmq.team.gpg] https://deb2.rabbitmq.com/rabbitmq-server/ubuntu/noble noble main
""",
    )
    write_file(
        Path("/etc/apt/preferences.d/rabbitmq"),
        """Package: erlang*
Pin: version 1:27.*
Pin-Priority: 1000

Package: rabbitmq-server
Pin: version 4.3.*
Pin-Priority: 1000
""",
    )
    context.progress(task_id, 100, "软件源配置完成")


def task_packages(context: InstallContext, task_id: str) -> None:
    context.progress(task_id, 5, "更新软件包索引")
    run_command(context, task_id, ["apt-get", "update"])
    packages = [
        "wget", "curl", "unzip", "tar", "build-essential", "net-tools", "ca-certificates", "gnupg", "rsync",
        "tcl", "libsystemd-dev", "pkg-config", "libpcre3", "libpcre3-dev", "zlib1g", "zlib1g-dev", "libssl-dev",
        "apparmor-utils", "mariadb-server", "mariadb-client", "openjdk-17-jdk",
        "erlang-base", "erlang-asn1", "erlang-crypto", "erlang-eldap", "erlang-ftp", "erlang-inets", "erlang-mnesia",
        "erlang-os-mon", "erlang-parsetools", "erlang-public-key", "erlang-runtime-tools", "erlang-snmp", "erlang-ssl",
        "erlang-syntax-tools", "erlang-tftp", "erlang-tools", "erlang-xmerl", "rabbitmq-server",
    ]
    context.progress(task_id, 20, "安装系统软件包")
    run_command(context, task_id, ["apt-get", "install", "-y", "--no-install-recommends", *packages], timeout=3600)
    context.progress(task_id, 100, "系统软件包安装完成")


def task_mariadb(context: InstallContext, task_id: str) -> None:
    target = Path("/data/db/mysql")
    context.progress(task_id, 5, "准备 MariaDB 数据目录")
    run_command(context, task_id, ["systemctl", "stop", "mariadb"])
    target.mkdir(parents=True, exist_ok=True)
    if not (target / "mysql").exists():
        run_command(context, task_id, ["rsync", "-aHAX", "--numeric-ids", "/var/lib/mysql/", f"{target}/"])
    shutil.chown(target, user="mysql", group="mysql")
    run_command(context, task_id, ["chown", "-R", "mysql:mysql", str(target)])
    buffer_mib = calculate_buffer_pool_mib()
    context.log(task_id, f"innodb_buffer_pool_size 自动设置为 {buffer_mib}M")
    write_file(Path("/etc/mysql/mariadb.conf.d/server.cnf"), mariadb_config(buffer_mib))
    apparmor_profile = next((path for path in (Path("/etc/apparmor.d/usr.sbin.mariadbd"), Path("/etc/apparmor.d/usr.sbin.mysqld")) if path.exists()), None)
    if apparmor_profile:
        local = Path("/etc/apparmor.d/local") / apparmor_profile.name
        write_file(local, "/data/db/mysql/ r,\n/data/db/mysql/** rwk,\n")
        if apparmor_active():
            run_command(context, task_id, ["apparmor_parser", "-r", str(apparmor_profile)])
        else:
            context.log(task_id, "AppArmor 内核接口未启用（常见于 WSL），跳过规则加载", "warning")
    context.progress(task_id, 45, "启动 MariaDB")
    run_command(context, task_id, ["systemctl", "enable", "mariadb"])
    run_command(context, task_id, ["systemctl", "restart", "mariadb"])
    password = context.secrets["db_password"]
    sql = f"""DELETE FROM mysql.global_priv WHERE User='';
DROP DATABASE IF EXISTS test;
CREATE DATABASE IF NOT EXISTS cict CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
CREATE USER IF NOT EXISTS 'cict'@'127.0.0.1' IDENTIFIED BY '{password}';
ALTER USER 'cict'@'127.0.0.1' IDENTIFIED BY '{password}';
GRANT ALL PRIVILEGES ON cict.* TO 'cict'@'127.0.0.1';
FLUSH PRIVILEGES;
"""
    context.progress(task_id, 70, "创建业务库和账号")
    run_command(context, task_id, ["mariadb", "--protocol=socket"], input_text=sql, display="mariadb --protocol=socket < [受保护 SQL]")
    run_command(context, task_id, ["mariadb", "--protocol=socket", "-Nse", "SELECT VERSION(), @@datadir;"])
    context.progress(task_id, 100, "MariaDB 配置完成")


def task_redis(context: InstallContext, task_id: str) -> None:
    archive = download(
        context,
        task_id,
        f"https://download.redis.io/releases/redis-{REDIS_VERSION}.tar.gz",
        CACHE_DIR / f"redis-{REDIS_VERSION}.tar.gz",
        REDIS_SHA256,
    )
    binary = Path("/data/app/redis/bin/redis-server")
    version_output = run_simple([str(binary), "--version"]) if binary.exists() else ""
    if f"v={REDIS_VERSION}" not in version_output:
        build_root = Path(tempfile.mkdtemp(prefix="cict-redis-"))
        extract_tar(archive, build_root)
        source = build_root / f"redis-{REDIS_VERSION}"
        context.progress(task_id, 60, "编译 Redis")
        run_command(context, task_id, ["make", f"-j{min(os.cpu_count() or 2, 4)}", "USE_SYSTEMD=yes"], cwd=source, timeout=3600)
        run_command(context, task_id, ["make", "USE_SYSTEMD=yes", "install", "PREFIX=/data/app/redis"], cwd=source)
        shutil.rmtree(build_root)
    ensure_user(context, task_id, "redis", "/nonexistent")
    Path("/etc/redis").mkdir(parents=True, exist_ok=True)
    Path("/data/app/redis/data").mkdir(parents=True, exist_ok=True)
    Path("/data/app/redis/logs").mkdir(parents=True, exist_ok=True)
    run_command(context, task_id, ["chown", "-R", "redis:redis", "/data/app/redis"])
    write_file(Path("/etc/redis/redis.conf"), redis_config(), mode=0o640, owner="root", group="redis")
    write_file(Path("/etc/systemd/system/redis.service"), redis_service())
    context.progress(task_id, 85, "启动 Redis")
    run_command(context, task_id, ["systemctl", "daemon-reload"])
    run_command(context, task_id, ["systemctl", "enable", "--now", "redis"])
    run_command(context, task_id, ["/data/app/redis/bin/redis-cli", "ping"])
    context.progress(task_id, 100, "Redis 配置完成")


def task_rabbitmq(context: InstallContext, task_id: str) -> None:
    context.progress(task_id, 10, "启动 RabbitMQ")
    run_command(context, task_id, ["systemctl", "enable", "--now", "rabbitmq-server"])
    run_command(context, task_id, ["rabbitmq-plugins", "enable", "rabbitmq_management"])
    users = rabbitmq_users()
    app_password = context.secrets["rabbit_password"]
    admin_password = context.secrets["rabbit_admin_password"]
    if "cict" not in users:
        run_command(context, task_id, ["rabbitmqctl", "add_user", "cict", app_password], display="rabbitmqctl add_user cict [REDACTED]")
    else:
        run_command(context, task_id, ["rabbitmqctl", "change_password", "cict", app_password], display="rabbitmqctl change_password cict [REDACTED]")
    run_command(context, task_id, ["rabbitmqctl", "set_permissions", "-p", "/", "cict", ".*", ".*", ".*"])
    if "cict_admin" not in users:
        run_command(context, task_id, ["rabbitmqctl", "add_user", "cict_admin", admin_password], display="rabbitmqctl add_user cict_admin [REDACTED]")
    else:
        run_command(context, task_id, ["rabbitmqctl", "change_password", "cict_admin", admin_password], display="rabbitmqctl change_password cict_admin [REDACTED]")
    run_command(context, task_id, ["rabbitmqctl", "set_user_tags", "cict_admin", "administrator"])
    run_command(context, task_id, ["rabbitmqctl", "set_permissions", "-p", "/", "cict_admin", ".*", ".*", ".*"])
    if "guest" in users:
        run_command(context, task_id, ["rabbitmqctl", "delete_user", "guest"])
    write_file(Path("/etc/rabbitmq/rabbitmq.conf"), rabbitmq_config())
    context.progress(task_id, 85, "重启 RabbitMQ")
    run_command(context, task_id, ["systemctl", "restart", "rabbitmq-server"])
    run_command(context, task_id, ["rabbitmq-diagnostics", "-q", "ping"])
    context.progress(task_id, 100, "RabbitMQ 配置完成")


def task_jdk(context: InstallContext, task_id: str) -> None:
    architecture = run_simple(["dpkg", "--print-architecture"])
    write_file(
        Path("/etc/profile.d/java.sh"),
        f"export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-{architecture}\nexport PATH=$JAVA_HOME/bin:$PATH\n",
    )
    output = run_command(context, task_id, ["java", "-version"])
    if "17." not in output:
        raise InstallError("未检测到 Java 17")
    context.progress(task_id, 100, "JDK 17 配置完成")


def task_nginx(context: InstallContext, task_id: str) -> None:
    archive = download(
        context,
        task_id,
        f"https://nginx.org/download/nginx-{NGINX_VERSION}.tar.gz",
        CACHE_DIR / f"nginx-{NGINX_VERSION}.tar.gz",
        NGINX_SHA256,
    )
    binary = Path("/data/app/nginx/sbin/nginx")
    version_output = run_simple([str(binary), "-v"]) if binary.exists() else ""
    if f"nginx/{NGINX_VERSION}" not in version_output:
        build_root = Path(tempfile.mkdtemp(prefix="cict-nginx-"))
        extract_tar(archive, build_root)
        source = build_root / f"nginx-{NGINX_VERSION}"
        context.progress(task_id, 60, "编译 Nginx")
        run_command(context, task_id, ["./configure", "--prefix=/data/app/nginx", "--with-http_ssl_module"], cwd=source)
        run_command(context, task_id, ["make", f"-j{min(os.cpu_count() or 2, 4)}"], cwd=source, timeout=3600)
        run_command(context, task_id, ["make", "install"], cwd=source)
        shutil.rmtree(build_root)
    Path("/data/app/nginx/conf/conf.d").mkdir(parents=True, exist_ok=True)
    write_file(Path("/data/app/nginx/conf/nginx.conf"), nginx_config())
    write_file(Path("/data/app/nginx/conf/conf.d/cict.conf"), nginx_site())
    Path("/data/app/nginx/conf/conf.d/frontend.conf").unlink(missing_ok=True)
    Path("/data/app/nginx/conf/conf.d/backend.conf").unlink(missing_ok=True)
    write_file(Path("/etc/systemd/system/nginx.service"), nginx_service())
    context.progress(task_id, 85, "启动 Nginx")
    run_command(context, task_id, [str(binary), "-t"])
    run_command(context, task_id, ["systemctl", "daemon-reload"])
    run_command(context, task_id, ["systemctl", "enable", "--now", "nginx"])
    run_command(context, task_id, ["systemctl", "reload", "nginx"])
    context.progress(task_id, 100, "Nginx 配置完成")


def task_rustfs(context: InstallContext, task_id: str) -> None:
    machine = platform.machine()
    arch = {"x86_64": "x86_64", "aarch64": "aarch64"}.get(machine)
    if not arch:
        raise InstallError(f"RustFS 不支持当前架构: {machine}")
    archive_name = f"rustfs-linux-{arch}-musl-v{RUSTFS_VERSION}.zip"
    release_root = f"https://github.com/rustfs/rustfs/releases/download/{RUSTFS_VERSION}"
    archive = download(context, task_id, f"{release_root}/{archive_name}", CACHE_DIR / archive_name)
    sums = download(context, task_id, f"{release_root}/SHA256SUMS", CACHE_DIR / f"rustfs-{RUSTFS_VERSION}-SHA256SUMS")
    expected = rustfs_checksum(sums, archive_name)
    actual = sha256_file(archive)
    if actual != expected:
        raise InstallError(f"RustFS SHA-256 不匹配: {actual}")
    context.log(task_id, f"SHA-256 校验通过: {archive_name}")
    with zipfile.ZipFile(archive) as bundle:
        member = next((name for name in bundle.namelist() if Path(name).name == "rustfs"), None)
        if not member:
            raise InstallError("RustFS 压缩包中未找到 rustfs 二进制")
        with bundle.open(member) as source, tempfile.NamedTemporaryFile(delete=False) as output:
            shutil.copyfileobj(source, output)
            temporary = Path(output.name)
    os.chmod(temporary, 0o755)
    os.replace(temporary, "/usr/local/bin/rustfs")
    ensure_user(context, task_id, "rustfs", "/nonexistent")
    Path("/data/rustfs0").mkdir(parents=True, exist_ok=True)
    Path("/var/log/rustfs").mkdir(parents=True, exist_ok=True)
    run_command(context, task_id, ["chown", "-R", "rustfs:rustfs", "/data/rustfs0", "/var/log/rustfs"])
    os.chmod("/data/rustfs0", 0o750)
    os.chmod("/var/log/rustfs", 0o750)
    write_file(Path("/etc/default/rustfs"), rustfs_environment(context.secrets), mode=0o640, owner="root", group="rustfs")
    write_file(Path("/etc/systemd/system/rustfs.service"), rustfs_service())
    context.progress(task_id, 80, "启动 RustFS")
    run_command(context, task_id, ["systemctl", "daemon-reload"])
    run_command(context, task_id, ["systemctl", "enable", "rustfs"])
    # RustFS reads its credentials only at process startup. A rerun may have
    # replaced /etc/default/rustfs, so an already-running service must restart.
    run_command(context, task_id, ["systemctl", "restart", "rustfs"])
    wait_for_service_port(
        context,
        "rustfs",
        "127.0.0.1",
        9000,
        45,
        log_paths=(Path("/var/log/rustfs/rustfs.log"),),
    )
    context.progress(task_id, 88, "等待 RustFS S3 服务就绪")
    create_s3_bucket(context, task_id, context.secrets["rustfs_access_key"], context.secrets["rustfs_secret"], "pike")
    context.log(task_id, "RustFS 存储桶 pike 已就绪")
    context.progress(task_id, 100, "RustFS 配置完成")


def task_backend(context: InstallContext, task_id: str) -> None:
    ensure_user(context, task_id, "cict", "/data/app/cict")
    for path in (Path("/etc/cict"), Path("/data/app/cict/releases"), Path("/data/app/cict/logs")):
        path.mkdir(parents=True, exist_ok=True)
    run_command(context, task_id, ["chown", "-R", "cict:cict", "/data/app/cict"])
    shutil.chown("/etc/cict", user="root", group="cict")
    os.chmod("/etc/cict", 0o750)
    xms, xmx = calculate_jvm_memory()
    context.log(task_id, f"JVM 参数自动设置为 -Xms{xms}m -Xmx{xmx}m")
    write_file(Path("/etc/cict/cict.conf"), cict_environment(context.secrets, xms, xmx), mode=0o640, owner="root", group="cict")
    write_file(Path("/etc/cict/application.properties"), cict_properties(), mode=0o640, owner="root", group="cict")
    write_file(Path("/etc/systemd/system/cict.service"), cict_service())
    context.progress(task_id, 35, "部署后端 JAR")
    source_jar = Path(context.config["backend_jar"]).resolve()
    release_name = clean_release_name(source_jar.name)
    release_target = Path("/data/app/cict/releases") / release_name
    shutil.copy2(source_jar, release_target)
    shutil.chown(release_target, user="cict", group="cict")
    os.chmod(release_target, 0o644)
    current = Path("/data/app/cict/current.jar")
    temporary_link = current.with_name("current.jar.new")
    temporary_link.unlink(missing_ok=True)
    temporary_link.symlink_to(Path("releases") / release_name)
    os.replace(temporary_link, current)
    context.progress(task_id, 60, "部署前端文件")
    frontend_source, temporary_dir = resolve_frontend(Path(context.config["frontend_path"]))
    run_command(context, task_id, ["rsync", "-a", "--delete", f"{frontend_source}/", "/data/app/nginx/html/"])
    if temporary_dir:
        shutil.rmtree(temporary_dir)
    context.progress(task_id, 78, "启动 CICT 后端")
    run_command(context, task_id, ["systemctl", "daemon-reload"])
    run_command(context, task_id, ["systemctl", "enable", "cict"])
    run_command(context, task_id, ["systemctl", "restart", "cict"])
    run_command(context, task_id, ["systemctl", "reload", "nginx"])
    wait_for_port("127.0.0.1", 9090, 90)
    context.progress(task_id, 100, "CICT 应用部署完成")


def task_acceptance(context: InstallContext, task_id: str) -> None:
    services = ("mariadb", "redis", "rabbitmq-server", "nginx", "rustfs", "cict")
    for index, service in enumerate(services, 1):
        run_command(context, task_id, ["systemctl", "is-active", "--quiet", service])
        context.log(task_id, f"服务正常: {service}", "success")
        context.progress(task_id, index * 70 // len(services), f"检查 {service}")
    run_command(context, task_id, ["/data/app/redis/bin/redis-cli", "ping"])
    run_command(context, task_id, ["mariadb", "--protocol=socket", "-Nse", "SELECT 1;"])
    run_command(context, task_id, ["rabbitmq-diagnostics", "-q", "ping"])
    for port in (80, 3306, 5672, 6379, 9000, 9001, 9090, 15672):
        if not port_open("127.0.0.1", port):
            raise InstallError(f"验收失败：端口 {port} 未监听")
    context.progress(task_id, 90, "检查 Web 首页")
    request = urllib.request.Request("http://127.0.0.1/", headers={"User-Agent": "CICT-Installer/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status >= 400:
                raise InstallError(f"Nginx 首页返回 HTTP {response.status}")
    except urllib.error.URLError as exc:
        raise InstallError(f"Nginx 首页访问失败: {exc}") from exc
    context.progress(task_id, 100, "全部验收检查通过")


def parse_os_release() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value.strip().strip('"')
    return values


def run_simple(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    return (result.stdout + result.stderr).strip()


def total_memory_bytes() -> int:
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")


def apparmor_active() -> bool:
    return Path("/sys/kernel/security/apparmor/profiles").exists()


def calculate_buffer_pool_mib() -> int:
    total_mib = total_memory_bytes() // (1024**2)
    return max(256, min(4096, int(total_mib * 0.45)))


def calculate_jvm_memory() -> tuple[int, int]:
    total_mib = total_memory_bytes() // (1024**2)
    xmx = max(512, min(4096, int(total_mib * 0.25)))
    xms = max(256, min(1024, xmx // 2))
    return xms, xmx


def port_open(host: str, port: int) -> bool:
    with socket.socket() as client:
        client.settimeout(0.4)
        return client.connect_ex((host, port)) == 0


def wait_for_port(host: str, port: int, timeout: int) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_open(host, port):
            return
        time.sleep(1)
    raise InstallError(f"等待 {host}:{port} 启动超时（{timeout} 秒）")


def wait_for_service_port(
    context: InstallContext,
    service: str,
    host: str,
    port: int,
    timeout: int,
    *,
    log_paths: tuple[Path, ...] = (),
) -> None:
    deadline = time.monotonic() + timeout
    failure_seen_at: float | None = None
    while time.monotonic() < deadline:
        if port_open(host, port):
            return
        state = systemd_service_state(service)
        failed = "ActiveState=failed" in state or "SubState=auto-restart" in state
        if failed:
            failure_seen_at = failure_seen_at or time.monotonic()
            if time.monotonic() - failure_seen_at >= 2:
                detail = service_diagnostics(service, state, log_paths)
                raise InstallError(f"{service} 启动失败\n{redact_secrets(detail, context.secrets)}")
        else:
            failure_seen_at = None
        time.sleep(1)
    state = systemd_service_state(service)
    detail = service_diagnostics(service, state, log_paths)
    raise InstallError(
        f"等待 {host}:{port} 启动超时（{timeout} 秒）\n"
        f"{redact_secrets(detail, context.secrets)}"
    )


def systemd_service_state(service: str) -> str:
    return run_simple([
        "systemctl",
        "show",
        service,
        "--property=ActiveState",
        "--property=SubState",
        "--property=Result",
        "--property=ExecMainStatus",
        "--no-pager",
    ])


def service_diagnostics(service: str, state: str, log_paths: tuple[Path, ...]) -> str:
    sections = [f"systemd 状态:\n{state or 'unknown'}"]
    journal = run_simple(["journalctl", "-u", service, "-n", "20", "--no-pager", "--output=cat"])
    if journal:
        sections.append(f"最近的 journal 日志:\n{journal[-6000:]}")
    for path in log_paths:
        tail = read_file_tail(path)
        if tail:
            sections.append(f"{path} 末尾:\n{tail}")
    return "\n".join(sections)


def read_file_tail(path: Path, max_bytes: int = 6000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            data = handle.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace").strip()


def redact_secrets(value: str, secrets: dict[str, str]) -> str:
    for secret in secrets.values():
        if secret:
            value = value.replace(secret, "[REDACTED]")
    return value


def rabbitmq_users() -> set[str]:
    output = run_simple(["rabbitmqctl", "list_users", "--silent"])
    return {line.split()[0] for line in output.splitlines() if line.strip()}


def rustfs_checksum(path: Path, archive_name: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[-1].lstrip("*") == archive_name:
            return fields[0]
    raise InstallError(f"SHA256SUMS 中未找到 {archive_name}")


def resolve_frontend(source: Path) -> tuple[Path, Path | None]:
    source = source.resolve()
    if source.is_dir():
        candidate = source if (source / "index.html").is_file() else source / "dist"
        if not (candidate / "index.html").is_file():
            raise InstallError("前端目录中未找到 index.html 或 dist/index.html")
        return candidate, None
    temporary = Path(tempfile.mkdtemp(prefix="cict-frontend-"))
    with zipfile.ZipFile(source) as bundle:
        for name in bundle.namelist():
            destination = (temporary / name).resolve()
            if temporary.resolve() not in destination.parents and destination != temporary.resolve():
                raise InstallError(f"前端压缩包包含非法路径: {name}")
        bundle.extractall(temporary)
    candidate = temporary if (temporary / "index.html").is_file() else temporary / "dist"
    if not (candidate / "index.html").is_file():
        shutil.rmtree(temporary)
        raise InstallError("前端 zip 中未找到 index.html 或 dist/index.html")
    return candidate, temporary


def clean_release_name(filename: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]", "-", filename)
    if not stem.endswith(".jar"):
        stem += ".jar"
    return f"{datetime.now().strftime('%Y%m%d%H%M%S')}-{stem}"


def create_s3_bucket(context: InstallContext, task_id: str, access_key: str, secret_key: str, bucket: str) -> None:
    method = "PUT"
    host = "127.0.0.1:9000"
    region = "us-east-1"
    service = "s3"
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_uri = f"/{bucket}"
    signed_headers = "host;x-amz-content-sha256;x-amz-date"
    last_error = ""
    for attempt in range(1, 31):
        now_utc = datetime.now(timezone.utc)
        amz_date = now_utc.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now_utc.strftime("%Y%m%d")
        canonical_headers = f"host:{host}\nx-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
        canonical_request = f"{method}\n{canonical_uri}\n\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
        scope = f"{date_stamp}/{region}/{service}/aws4_request"
        string_to_sign = f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n{hashlib.sha256(canonical_request.encode()).hexdigest()}"
        key_date = hmac.new(("AWS4" + secret_key).encode(), date_stamp.encode(), hashlib.sha256).digest()
        key_region = hmac.new(key_date, region.encode(), hashlib.sha256).digest()
        key_service = hmac.new(key_region, service.encode(), hashlib.sha256).digest()
        signing_key = hmac.new(key_service, b"aws4_request", hashlib.sha256).digest()
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        authorization = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed_headers}, Signature={signature}"
        request = urllib.request.Request(
            f"http://{host}{canonical_uri}",
            method=method,
            data=b"",
            headers={
                "Authorization": authorization,
                "Host": host,
                "x-amz-content-sha256": payload_hash,
                "x-amz-date": amz_date,
            },
        )
        try:
            urllib.request.urlopen(request, timeout=15).close()
            return
        except urllib.error.HTTPError as exc:
            error_detail = format_s3_error(exc.code, exc.read(8192))
            if exc.code == 409:
                return
            last_error = error_detail
            if exc.code not in {429, 500, 502, 503, 504}:
                raise InstallError(f"创建 RustFS 存储桶失败: {last_error}") from exc
        except urllib.error.URLError as exc:
            last_error = str(exc.reason)
        context.log(task_id, f"RustFS 尚未 ready（{last_error}），{attempt}/30，2 秒后重试", "warning")
        time.sleep(2)
    raise InstallError(f"RustFS 在 60 秒内未就绪，无法创建存储桶: {last_error}")


def format_s3_error(status: int, payload: bytes) -> str:
    """Return a useful S3 error without exposing request credentials."""
    try:
        root = ET.fromstring(payload)
        code = (root.findtext("Code") or "").strip()
        message = (root.findtext("Message") or "").strip()
    except ET.ParseError:
        code = ""
        message = ""
    detail = ": ".join(value for value in (code, message) if value)
    return f"HTTP {status}" + (f" ({detail})" if detail else "")


def mariadb_config(buffer_mib: int) -> str:
    return f"""[mysqld]
datadir=/data/db/mysql
performance_schema=1
bind-address=127.0.0.1
skip-name-resolve=ON
log_bin_trust_function_creators=1
max_sp_recursion_depth=10
character-set-server=utf8mb4
collation-server=utf8mb4_general_ci
init_connect='SET NAMES utf8mb4 COLLATE utf8mb4_general_ci'
skip-character-set-client-handshake
transaction_isolation=READ-COMMITTED
back_log=1000
max_connections=500
thread_cache_size=100
thread_stack=256K
table_open_cache=8192
table_definition_cache=8192
max_allowed_packet=64M
binlog_cache_size=1M
max_heap_table_size=128M
tmp_table_size=64M
read_buffer_size=1M
read_rnd_buffer_size=1M
join_buffer_size=1M
innodb_buffer_pool_size={buffer_mib}M
innodb_log_file_size=512M
innodb_flush_log_at_trx_commit=1
innodb_io_capacity=2000
log-bin=mysql-bin
binlog_format=row
binlog_expire_logs_seconds=259200
slow_query_log=OFF
long_query_time=2
innodb_ft_min_token_size=4
ft_min_word_len=4
"""


def redis_config() -> str:
    return """bind 127.0.0.1 ::1
protected-mode yes
port 6379
daemonize no
supervised systemd
dir /data/app/redis/data
logfile "/data/app/redis/logs/redis.log"
pidfile /run/redis/redis-server.pid
appendonly yes
"""


def redis_service() -> str:
    return """[Unit]
Description=Redis In-Memory Data Store
After=network.target

[Service]
Type=notify
User=redis
Group=redis
RuntimeDirectory=redis
RuntimeDirectoryMode=0750
ExecStart=/data/app/redis/bin/redis-server /etc/redis/redis.conf
ExecStop=/data/app/redis/bin/redis-cli shutdown
Restart=always
LimitNOFILE=10032

[Install]
WantedBy=multi-user.target
"""


def rabbitmq_config() -> str:
    return """listeners.tcp.default = 127.0.0.1:5672
management.tcp.ip = 127.0.0.1
management.tcp.port = 15672
"""


def nginx_config() -> str:
    return """user www-data;
worker_processes auto;
error_log /data/app/nginx/logs/error.log warn;
pid /data/app/nginx/logs/nginx.pid;

events {
    worker_connections 10240;
}

http {
    include mime.types;
    default_type application/octet-stream;
    gzip on;
    gzip_comp_level 6;
    gzip_min_length 1k;
    gzip_proxied any;
    gzip_vary on;
    gzip_types text/plain text/css application/json application/javascript application/xml application/xml+rss image/svg+xml;
    include conf.d/*.conf;
}
"""


def nginx_site() -> str:
    return """server {
    listen 80 default_server;
    server_name _;
    root /data/app/nginx/html;
    index index.html;

    location /api/ {
        proxy_pass http://127.0.0.1:9090;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_connect_timeout 60s;
        proxy_read_timeout 120s;
    }

    location / {
        try_files $uri /index.html;
    }

    location ~* \\.(ico|jpg|jpeg|png|gif|svg|css|js|woff|woff2|ttf|eot)$ {
        expires 30d;
        access_log off;
    }
}
"""


def nginx_service() -> str:
    return """[Unit]
Description=The NGINX HTTP and reverse proxy server
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
PIDFile=/data/app/nginx/logs/nginx.pid
ExecStartPre=/data/app/nginx/sbin/nginx -t -q -g 'daemon on; master_process on;'
ExecStart=/data/app/nginx/sbin/nginx -g 'daemon on; master_process on;'
ExecReload=/data/app/nginx/sbin/nginx -s reload
ExecStop=/data/app/nginx/sbin/nginx -s quit
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""


def rustfs_environment(secrets: dict[str, str]) -> str:
    return f"""RUSTFS_ACCESS_KEY={secrets['rustfs_access_key']}
RUSTFS_SECRET_KEY={secrets['rustfs_secret']}
RUSTFS_VOLUMES="/data/rustfs0"
RUSTFS_ADDRESS="127.0.0.1:9000"
RUSTFS_CONSOLE_ADDRESS="127.0.0.1:9001"
RUSTFS_CONSOLE_ENABLE=true
RUST_LOG=error
RUSTFS_OBS_LOG_DIRECTORY="/var/log/rustfs/"
"""


def rustfs_service() -> str:
    return """[Unit]
Description=RustFS Object Storage Server
Documentation=https://rustfs.com/docs/
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=rustfs
Group=rustfs
WorkingDirectory=/data/rustfs0
EnvironmentFile=-/etc/default/rustfs
ExecStart=/usr/local/bin/rustfs ${RUSTFS_VOLUMES}
LimitNOFILE=1048576
LimitNPROC=32768
TasksMax=infinity
Restart=always
RestartSec=10s
OOMScoreAdjust=-1000
SendSIGKILL=no
TimeoutStartSec=30s
TimeoutStopSec=30s
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=true
PrivateTmp=true
PrivateDevices=true
ProtectClock=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictRealtime=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def cict_environment(secrets: dict[str, str], xms: int, xmx: int) -> str:
    return f"""JAVA_OPTS="-Xms{xms}m -Xmx{xmx}m"
CICT_DATABASE_PASSWORD={secrets['db_password']}
RABBITMQ_APP_PASSWORD={secrets['rabbit_password']}
RUSTFS_ACCESS_KEY={secrets['rustfs_access_key']}
RUSTFS_SECRET_KEY={secrets['rustfs_secret']}
"""


def cict_properties() -> str:
    return """pike.api.white-list[0]=/api/generator/**
pike.api.white-list[1]=/ssoLogin/sso/social-callback/**
pike.api.white-list[2]=/ssoLogin/auth-binding/**
pike.api.white-list[3]=/py
pike.api.white-list[4]=/api/v1/**
pike.api.white-list[5]=/webhook/trigger/**
pike.api.white-list[6]=/brand-config/public
pike.api.white-list[7]=/brand-config/asset/**
autocrud.base-packages=com.polelink
spring.datasource.url=jdbc:mariadb://127.0.0.1:3306/cict
spring.datasource.username=cict
spring.datasource.password=${CICT_DATABASE_PASSWORD}
spring.quartz.properties.org.quartz.jobStore.tablePrefix=qrtz_
spring.redis.host=127.0.0.1
spring.redis.port=6379
spring.rabbitmq.host=127.0.0.1
spring.rabbitmq.port=5672
spring.rabbitmq.username=cict
spring.rabbitmq.password=${RABBITMQ_APP_PASSWORD}
pike.file.s3.endpoint=http://127.0.0.1:9000
pike.file.s3.bucket=pike
pike.file.s3.accessKey=${RUSTFS_ACCESS_KEY}
pike.file.s3.secretKey=${RUSTFS_SECRET_KEY}
logging.file.path=/data/app/cict/logs
logging.file.name=/data/app/cict/logs/cict.log
server.address=127.0.0.1
server.port=9090
"""


def cict_service() -> str:
    return """[Unit]
Description=CICT Java Application
Wants=network-online.target mariadb.service redis.service rabbitmq-server.service rustfs.service
After=network-online.target mariadb.service redis.service rabbitmq-server.service rustfs.service
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
User=cict
Group=cict
EnvironmentFile=/etc/cict/cict.conf
WorkingDirectory=/data/app/cict
ExecStart=/bin/sh -c 'exec java $JAVA_OPTS -jar /data/app/cict/current.jar --spring.config.additional-location=/etc/cict/application.properties'
StandardOutput=journal
StandardError=journal
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
