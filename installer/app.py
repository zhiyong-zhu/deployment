#!/usr/bin/env python3
"""Local web interface and dependency scheduler for the Ubuntu installer."""

from __future__ import annotations

import argparse
import copy
import json
import os
import platform
import secrets as secret_module
import threading
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from installer_engine import InstallContext, InstallError, run_task, validate_payload


ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
HOST = os.environ.get("CICT_INSTALLER_HOST", "127.0.0.1")
PORT = int(os.environ.get("CICT_INSTALLER_PORT", "8787"))
ALLOW_MODIFY = False
REQUEST_TOKEN = secret_module.token_urlsafe(32)
REPORT_DIR = Path("/var/log/cict-installer")
CONFIG_PATH = Path("/etc/cict-installer/config.json")
INSTALL_DEFAULTS: dict[str, Any] = {}
RUNS: dict[str, dict[str, Any]] = {}
RUNS_LOCK = threading.Lock()


TASKS = (
    {"id": "preflight", "title": "环境检查", "detail": "检查 Ubuntu 24、架构、权限、磁盘、端口和网络", "group": "准备", "dependencies": [], "weight": 2},
    {"id": "repositories", "title": "配置软件源", "detail": "配置 MariaDB、RabbitMQ 官方软件源和密钥", "group": "基础环境", "dependencies": ["preflight"], "weight": 2},
    {"id": "packages", "title": "安装系统软件包", "detail": "安装数据库、消息队列、JDK 和编译依赖", "group": "基础环境", "dependencies": ["repositories"], "weight": 4},
    {"id": "mariadb", "title": "配置 MariaDB", "detail": "迁移数据目录、创建业务库和账号", "group": "服务配置", "dependencies": ["packages"], "weight": 3},
    {"id": "redis", "title": "安装 Redis", "detail": "下载校验源码、编译并注册 systemd 服务", "group": "服务配置", "dependencies": ["packages"], "weight": 3},
    {"id": "rabbitmq", "title": "配置 RabbitMQ", "detail": "启用管理插件并创建应用和运维账号", "group": "服务配置", "dependencies": ["packages"], "weight": 2},
    {"id": "jdk", "title": "配置 JDK", "detail": "确认 Java 17 并写入系统环境配置", "group": "服务配置", "dependencies": ["packages"], "weight": 1},
    {"id": "nginx", "title": "安装 Nginx", "detail": "下载校验源码、编译并配置前端/API 站点", "group": "服务配置", "dependencies": ["packages"], "weight": 3},
    {"id": "rustfs", "title": "安装 RustFS", "detail": "校验二进制、配置对象存储并创建 pike 桶", "group": "服务配置", "dependencies": ["packages"], "weight": 3},
    {"id": "backend", "title": "部署 CICT 应用", "detail": "写入 Spring Boot 配置、部署 JAR 和前端文件", "group": "应用部署", "dependencies": ["mariadb", "redis", "rabbitmq", "jdk", "nginx", "rustfs"], "weight": 4},
    {"id": "acceptance", "title": "执行验收", "detail": "检查 systemd、端口、数据库、对象存储和首页", "group": "验收", "dependencies": ["backend"], "weight": 3},
)
TASK_MAP = {task["id"]: task for task in TASKS}


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def task_state(task: dict[str, Any]) -> dict[str, Any]:
    return {
        **task,
        "status": "pending",
        "progress": 0,
        "started_at": None,
        "finished_at": None,
        "message": "等待依赖任务完成",
        "logs": [],
    }


def append_log(run: dict[str, Any], task_id: str, message: str, level: str = "info") -> None:
    entry = {"task": task_id, "time": now(), "level": level, "message": message}
    with RUNS_LOCK:
        run["logs"].append(entry)
        if task_id in run["tasks"]:
            run["tasks"][task_id]["logs"].append({key: value for key, value in entry.items() if key != "task"})
        run["logs"] = run["logs"][-1000:]


def set_task(run: dict[str, Any], task_id: str, **changes: Any) -> None:
    with RUNS_LOCK:
        run["tasks"][task_id].update(changes)
        run["progress"] = calculate_progress(run)


def calculate_progress(run: dict[str, Any]) -> int:
    total = sum(task["weight"] for task in TASKS)
    value = 0.0
    for task in TASKS:
        state = run["tasks"][task["id"]]
        if state["status"] == "completed":
            value += task["weight"]
        elif state["status"] == "running":
            value += task["weight"] * state["progress"] / 100
    return round(value / total * 100)


def public_run(run: dict[str, Any]) -> dict[str, Any]:
    with RUNS_LOCK:
        snapshot = copy.deepcopy(run)
    snapshot.pop("_secrets", None)
    return snapshot


def execute_run(run: dict[str, Any]) -> None:
    run["status"] = "running"
    append_log(run, "system", "真实安装计划开始执行", "warning")
    pending = set(TASK_MAP)
    active: dict[Any, str] = {}
    failed = False
    context = InstallContext(
        config=run["config"],
        secrets=run["_secrets"],
        log_callback=lambda task_id, message, level="info": append_log(run, task_id, message, level),
        progress_callback=lambda task_id, value, message: set_task(run, task_id, progress=value, message=message),
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        while pending or active:
            blocked = [
                task_id for task_id in pending
                if any(run["tasks"][dependency]["status"] in {"failed", "blocked"} for dependency in TASK_MAP[task_id]["dependencies"])
            ]
            for task_id in blocked:
                pending.remove(task_id)
                set_task(run, task_id, status="blocked", message="依赖任务失败，未执行")
                append_log(run, task_id, "依赖任务失败，跳过", "error")
                failed = True
            ready = [
                task_id for task_id in pending
                if all(run["tasks"][dependency]["status"] == "completed" for dependency in TASK_MAP[task_id]["dependencies"])
            ]
            for task_id in ready:
                pending.remove(task_id)
                set_task(run, task_id, status="running", progress=0, started_at=now(), message="正在执行")
                active[executor.submit(run_one_task, run, context, task_id)] = task_id
            if not active:
                if pending:
                    failed = True
                    for task_id in pending:
                        set_task(run, task_id, status="blocked", message="任务依赖图无法继续")
                break
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                task_id = active.pop(future)
                try:
                    future.result()
                except Exception as exc:  # surfaced in the UI and report
                    failed = True
                    set_task(run, task_id, status="failed", finished_at=now(), message=str(exc))
                    append_log(run, task_id, str(exc), "error")
    run["status"] = "failed" if failed else "completed"
    run["finished_at"] = now()
    append_log(run, "system", "真实安装失败" if failed else "真实安装完成", "error" if failed else "success")
    write_report(run)


def run_one_task(run: dict[str, Any], context: InstallContext, task_id: str) -> None:
    append_log(run, task_id, f"开始：{TASK_MAP[task_id]['detail']}")
    run_task(task_id, context)
    set_task(run, task_id, status="completed", progress=100, finished_at=now(), message="已完成")
    append_log(run, task_id, "完成", "success")


def write_report(run: dict[str, Any]) -> None:
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        report_path = REPORT_DIR / f"{run['id']}.json"
        report_path.write_text(json.dumps(public_run(run), ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(report_path, 0o600)
    except OSError:
        pass


def make_run(payload: dict[str, Any]) -> dict[str, Any]:
    overrides = {key: value for key, value in payload.items() if value not in (None, "")}
    merged = {**INSTALL_DEFAULTS, **overrides}
    config, secrets = validate_payload(merged, execute=True)
    run_id = uuid.uuid4().hex[:12]
    run = {
        "id": run_id,
        "status": "queued",
        "created_at": now(),
        "finished_at": None,
        "progress": 0,
        "config": config,
        "tasks": {task["id"]: task_state(task) for task in TASKS},
        "logs": [],
        "_secrets": secrets,
    }
    with RUNS_LOCK:
        RUNS[run_id] = run
    threading.Thread(target=execute_run, args=(run,), daemon=True).start()
    return public_run(run)


class Handler(BaseHTTPRequestHandler):
    server_version = "CICTInstaller/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/status":
            self.send_json({
                "app": "CICT Ubuntu Installer",
                "version": "1.0.0",
                "platform": platform.system(),
                "architecture": platform.machine(),
                "allow_modify": ALLOW_MODIFY,
                "root": os.geteuid() == 0,
                "request_token": REQUEST_TOKEN,
                "config_defaults": {
                    key: INSTALL_DEFAULTS.get(key, "")
                    for key in ("server_name", "data_root", "backend_jar", "frontend_path")
                },
                "tasks": [{key: value for key, value in task.items() if key != "weight"} for task in TASKS],
            })
            return
        if path.startswith("/api/runs/"):
            run_id = path.rsplit("/", 1)[-1]
            with RUNS_LOCK:
                run = RUNS.get(run_id)
            self.send_json(public_run(run) if run else {"error": "run not found"}, 200 if run else 404)
            return
        self.serve_static(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/api/install":
            self.send_json({"error": "not found"}, 404)
            return
        if self.headers.get("X-Installer-Token") != REQUEST_TOKEN:
            self.send_json({"error": "invalid local request token"}, 403)
            return
        if not ALLOW_MODIFY or os.geteuid() != 0:
            self.send_json({"error": "真实安装器必须以 sudo/root 和 --execute 启动"}, 409)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            run = make_run(payload)
        except (ValueError, json.JSONDecodeError, InstallError) as exc:
            self.send_json({"error": str(exc)}, 400)
            return
        self.send_json(run, 202)

    def serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        candidate = (STATIC / relative).resolve()
        if STATIC not in candidate.parents and candidate != STATIC:
            self.send_error(403)
            return
        if not candidate.is_file():
            self.send_error(404)
            return
        content_type = "text/plain; charset=utf-8"
        if candidate.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        elif candidate.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif candidate.suffix == ".js":
            content_type = "text/javascript; charset=utf-8"
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    global ALLOW_MODIFY, PORT, HOST, CONFIG_PATH, INSTALL_DEFAULTS
    parser = argparse.ArgumentParser(description="CICT Ubuntu 24 direct installer")
    parser.add_argument("--execute", action="store_true", help="enable real system changes")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--config", default=str(CONFIG_PATH), help="JSON configuration path")
    args = parser.parse_args()
    ALLOW_MODIFY = args.execute
    PORT = args.port
    CONFIG_PATH = Path(args.config).expanduser().resolve()
    if CONFIG_PATH.is_file():
        try:
            INSTALL_DEFAULTS = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(INSTALL_DEFAULTS, dict):
                raise ValueError("配置根节点必须是 JSON 对象")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise SystemExit(f"配置文件读取失败: {CONFIG_PATH}: {exc}") from exc
        configured_bind = str(INSTALL_DEFAULTS.get("bind", "")).strip()
        if configured_bind:
            HOST, configured_port = configured_bind.rsplit(":", 1) if ":" in configured_bind else (configured_bind, str(PORT))
            PORT = int(configured_port)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"CICT Ubuntu Installer running at http://{HOST}:{PORT}")
    print("Mode: real installation enabled" if ALLOW_MODIFY else "Mode: locked; start with --execute")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
