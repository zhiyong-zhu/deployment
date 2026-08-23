#!/usr/bin/env python3
"""CICT 部署向导（Web UI）。

单文件、仅依赖 Python 3 标准库（Ubuntu 24.04 自带 python3，离线可用）。
- 职责：在浏览器中编辑配置与密码 → 调用 install.sh 执行部署 → 展示验证结果
- 引擎：所有部署动作仍由 deploy/bundle/install.sh 完成，本程序不复制部署逻辑
- 安全：只绑定 127.0.0.1:8788，远程使用请走 SSH 隧道；执行安装需要以 sudo 启动本程序

用法:
  在线模式（仓库 deploy/gui/ 下）: sudo python3 deploy-gui.py
  离线模式（bundle 解压目录内）  : sudo python3 deploy-gui.py
  然后浏览器访问 http://127.0.0.1:8788
"""
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8788
SCRIPT_DIR = Path(__file__).resolve().parent

# ===== 模式与路径定位 =====
if (SCRIPT_DIR / "manifest.sha256").is_file():
    MODE = "offline"                       # bundle 根目录
    SRC = SCRIPT_DIR / "project"
    INSTALL_SH = SCRIPT_DIR / "install.sh"
    BUNDLE_TAG = (SCRIPT_DIR / "bundle-tag").read_text().strip() if (SCRIPT_DIR / "bundle-tag").is_file() else ""
elif ((SCRIPT_DIR.parent) / "docker-compose.yml").is_file():
    MODE = "online"                        # 仓库 deploy/gui/
    SRC = SCRIPT_DIR.parent
    INSTALL_SH = SCRIPT_DIR.parent / "bundle" / "install.sh"
    BUNDLE_TAG = ""
else:
    raise SystemExit("请在离线安装包目录或仓库 deploy/gui/ 下运行本程序")

DEFAULT_TARGET = "/data/app/cict-docker"

# 可编辑的配置文件（相对 SRC 路径 → 界面标签）
EDITABLE_FILES = {
    "docker-compose.yml": "docker-compose.yml（服务编排）",
    "nginx/nginx.conf": "nginx.conf（Nginx 主配置）",
    "nginx/conf.d/cict.conf": "cict.conf（站点与 API 代理）",
    "mariadb/server.cnf": "server.cnf（MariaDB 调优）",
    "cict/application.properties": "application.properties（Spring Boot）",
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
    "NGINX_PORT": "Nginx 对外端口",
    "ADMIN_BIND_ADDRESS": "管理端口绑定地址",
    "RABBITMQ_MGMT_PORT": "RabbitMQ 管理端口",
    "RUSTFS_PORT": "RustFS S3 端口",
    "RUSTFS_CONSOLE_PORT": "RustFS 控制台端口",
    "CICT_IMAGE_TAG": "后端镜像版本",
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

# ===== 安装子进程状态 =====
class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.lines = deque(maxlen=5000)
        self.running = False
        self.exit_code = None
        self.cmd = ""

    def start(self, cmd):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.exit_code = None
            self.cmd = " ".join(cmd)

        def run():
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(INSTALL_SH.parent), stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, bufsize=1)
                for line in proc.stdout:
                    with self.lock:
                        self.lines.append(line.rstrip("\n"))
                code = proc.wait()
                with self.lock:
                    self.exit_code = code
                    self.lines.append(f"[deploy-gui] install.sh 退出码: {code}")
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

    env = parse_env((Path(target_dir) / ".env").read_text()) if (Path(target_dir) / ".env").is_file() else {}
    nginx_port = env.get("NGINX_PORT", "80")
    front = http_status(f"http://127.0.0.1:{nginx_port}/")
    results.append({"name": f"前端页面 (:{nginx_port})", "ok": front == 200, "detail": f"HTTP {front}"})
    api = http_status(f"http://127.0.0.1:{nginx_port}/api/auth/login")
    results.append({"name": "后端 API 探活", "ok": api is not None and api < 500, "detail": f"HTTP {api}（非 5xx 即在响应）"})
    for port, label in ((env.get("RABBITMQ_MGMT_PORT", "15672"), "RabbitMQ 管理端口"),
                        (env.get("RUSTFS_PORT", "9000"), "RustFS S3 端口"),
                        (env.get("RUSTFS_CONSOLE_PORT", "9001"), "RustFS 控制台端口")):
        results.append({"name": f"{label} (:{port} 仅本机)", "ok": port_open(int(port)), "detail": "监听 127.0.0.1" if port_open(int(port)) else "未监听"})
    return results

# ===== HTTP 服务 =====
HTML = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>CICT 部署向导</title>
<style>
 body{font-family:system-ui,-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;margin:0;background:#f4f6f8;color:#1f2937}
 header{background:#111827;color:#fff;padding:14px 24px;display:flex;gap:16px;align-items:center}
 header h1{font-size:17px;margin:0;font-weight:600}
 .badge{font-size:12px;padding:2px 10px;border-radius:10px;background:#374151}
 .badge.ok{background:#059669}.badge.warn{background:#b45309}
 nav{display:flex;gap:4px;padding:0 24px;background:#fff;border-bottom:1px solid #e5e7eb}
 nav button{border:none;background:none;padding:12px 18px;font-size:14px;cursor:pointer;color:#6b7280;border-bottom:2px solid transparent}
 nav button.active{color:#111827;border-bottom-color:#2563eb;font-weight:600}
 main{padding:24px;max-width:1000px;margin:0 auto}
 .card{background:#fff;border:1px solid #e5e7eb;border-radius:8px;padding:20px;margin-bottom:16px}
 .card h2{font-size:15px;margin:0 0 14px;color:#111827}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
 .field label{display:block;font-size:12px;color:#6b7280;margin-bottom:4px}
 .field input{width:100%;box-sizing:border-box;padding:7px 9px;border:1px solid #d1d5db;border-radius:6px;font-size:13px;font-family:inherit}
 .field .row{display:flex;gap:6px}
 .mini{font-size:12px;padding:6px 10px;border:1px solid #d1d5db;background:#f9fafb;border-radius:6px;cursor:pointer;white-space:nowrap}
 .mini:hover{background:#eff6ff;border-color:#93c5fd}
 .btn{padding:9px 20px;border:none;border-radius:6px;background:#2563eb;color:#fff;font-size:14px;cursor:pointer}
 .btn:disabled{background:#9ca3af;cursor:not-allowed}
 .btn.green{background:#059669}
 pre#log{background:#0b1021;color:#d1fae5;font-size:12px;line-height:1.55;padding:14px;border-radius:8px;
   height:420px;overflow-y:auto;white-space:pre-wrap;word-break:break-all}
 table{width:100%;border-collapse:collapse;font-size:13px}
 td,th{padding:8px 10px;border-bottom:1px solid #e5e7eb;text-align:left}
 .st{font-weight:700}.st.ok{color:#059669}.st.bad{color:#dc2626}
 details{margin-top:14px}summary{cursor:pointer;font-size:13px;color:#374151}
 textarea{width:100%;box-sizing:border-box;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;
   min-height:180px;padding:8px;border:1px solid #d1d5db;border-radius:6px}
 .hint{font-size:12px;color:#6b7280;margin-top:10px;line-height:1.7}
 .warnbox{background:#fffbeb;border:1px solid #fcd34d;color:#92400e;font-size:12px;padding:8px 12px;border-radius:6px;margin-top:10px;display:none}
 #toast{position:fixed;bottom:24px;right:24px;background:#111827;color:#fff;padding:10px 18px;border-radius:8px;font-size:13px;display:none}
</style></head><body>
<header><h1>CICT 部署向导</h1>
 <span class="badge" id="mode"></span><span class="badge" id="root"></span>
 <span class="badge" id="tag" style="margin-left:auto"></span></header>
<nav><button class="active" data-tab="cfg">① 配置</button>
 <button data-tab="run">② 一键安装</button><button data-tab="verify">③ 验证</button></nav>
<main>
<section id="tab-cfg">
 <div class="card"><h2>部署参数</h2><div class="grid" id="params"></div>
  <div id="online-box" style="display:none;margin-top:12px" class="grid"></div></div>
 <div class="card"><h2>密码（留空的项安装时自动生成随机值）</h2>
  <div class="grid" id="secrets"></div>
  <button class="mini" onclick="fillRandomAll()">全部生成随机值</button>
  <button class="mini" onclick="toggleShow()">显示/隐藏密码</button>
  <div class="warnbox" id="dollar-warn">密码包含 $：Compose 的 .env 中需写成 $$，建议改用随机十六进制值</div></div>
 <div class="card"><details><summary>高级配置（直接编辑部署文件，修改后需重新执行安装生效）</summary>
  <div id="adv"></div></details></div>
 <button class="btn" onclick="saveConfig()">保存配置</button>
 <div class="hint">保存会把上述值写入 <code>&lt;部署目录&gt;/.env</code>（权限 600）并保存高级配置文件；尚未保存的密码留空项，由 install.sh 随机生成。</div>
</section>
<section id="tab-run" style="display:none">
 <div class="card"><h2>执行安装</h2>
  <label style="font-size:13px"><input type="checkbox" id="skip-engine"> 已安装 Docker，跳过 Engine 安装（--skip-engine）</label>
  <div class="grid" style="margin-top:12px">
   <div class="field"><label>Docker apt 源基地址（留空自动探测；国内网络下载失败时填，如 mirrors.aliyun.com/docker-ce）</label>
    <input id="k:docker_apt_mirror" type="text" placeholder="https://mirrors.aliyun.com/docker-ce"></div>
   <div class="field"><label>镜像加速器（拉取 Docker Hub 镜像超时/被拒时填，可留空）</label>
    <input id="k:registry_mirror" type="text" placeholder="https://<你的镜像加速地址>"></div>
  </div><br>
  <button class="btn green" id="runbtn" onclick="runInstall()">开始安装</button>
  <span class="hint" id="runstate" style="display:inline;margin-left:12px"></span></div>
 <div class="card"><h2>安装日志（实时）</h2><pre id="log">等待启动安装...</pre></div>
</section>
<section id="tab-verify" style="display:none">
 <div class="card"><h2>验证结果</h2>
  <button class="btn" onclick="doVerify()">立即验证</button>
  <table id="vtab" style="margin-top:14px"><thead><tr><th>检查项</th><th style="width:80px">状态</th><th>详情</th></tr></thead><tbody></tbody></table>
  <div class="hint">全部通过后，请手工完成最后一步：SSH 隧道 <code>ssh -L 9001:127.0.0.1:9001 &lt;用户&gt;@&lt;服务器IP&gt;</code>，
  访问 <code>http://127.0.0.1:9001</code> 用 .env 中的 RustFS 密钥登录并创建 <b>pike</b> 存储桶。</div></div>
</section>
</main><div id="toast"></div>
<script>
let CFG=null, logFrom=0, timer=null;
const $=id=>document.getElementById(id);
const RANDOM_BYTES={MARIADB_ROOT_PASSWORD:24,MARIADB_PASSWORD:24,REDIS_PASSWORD:24,RABBITMQ_DEFAULT_PASS:24,RABBITMQ_ADMIN_PASSWORD:24,RUSTFS_ACCESS_KEY:16,RUSTFS_SECRET_KEY:32};
function rnd(n){const a=new Uint8Array(n);crypto.getRandomValues(a);return[...a].map(b=>b.toString(16).padStart(2,'0')).join('')}
function toast(m,ok=true){const t=$('toast');t.textContent=m;t.style.background=ok?'#059669':'#dc2626';t.style.display='block';setTimeout(()=>t.style.display='none',2600)}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('nav button').forEach(x=>x.classList.remove('active'));b.classList.add('active');
 ['cfg','run','verify'].forEach(t=>$('tab-'+t).style.display=t===b.dataset.tab?'':'none');});
async function load(){
 CFG=await (await fetch('/api/config')).json();
 $('mode').textContent=CFG.mode==='online'?'在线模式':'离线模式';
 $('mode').className='badge '+(CFG.mode==='online'?'ok':'warn');
 $('root').textContent=CFG.is_root?'root 运行':'非 root（无法执行安装）';
 $('root').className='badge '+(CFG.is_root?'ok':'warn');
 if(CFG.bundle_tag)$('tag').textContent='bundle 版本: '+CFG.bundle_tag;
 const p=$('params'),s=$('secrets');p.innerHTML='';s.innerHTML='';
 for(const[k,label,val]of CFG.params){if(k==='CICT_IMAGE_TAG'&&CFG.mode==='offline')continue;
  p.insertAdjacentHTML('beforeend',field(k,label,val,false));}
 for(const[k,label,val]of CFG.secrets){s.insertAdjacentHTML('beforeend',field(k,label,val,true));}
 if(CFG.mode==='online'){const o=$('online-box');o.style.display='grid';
  o.innerHTML=field('app_jar','后端 JAR 路径（服务器上的路径）',CFG.app_jar||'',false)
   +field('frontend','前端产物路径（zip 或目录）',CFG.frontend||'',false)
   +field('tag','本次发布的唯一版本号',CFG.tag||'',false);}
 const adv=$('adv');adv.innerHTML='';
 for(const f of CFG.files){adv.insertAdjacentHTML('beforeend',
  `<p style="font-size:12px;color:#6b7280;margin:14px 0 4px">${f.label}</p><textarea id="f:${f.path}">${f.content.replace(/</g,'&lt;')}</textarea>`);}
 bindInputs();refreshLog();}
function field(k,label,val,secret){return `<div class="field"><label>${label}</label><div class="row">
 <input id="k:${k}" type="${secret?'password':'text'}" value="${String(val).replace(/"/g,'&quot;')}" autocomplete="off">
 ${secret?`<button class="mini" onclick="fillRandom('${k}')">随机</button>`:''}</div></div>`}
function fillRandom(k){$('k:'+k).value=rnd(RANDOM_BYTES[k]||24);bindInputs()}
function fillRandomAll(){for(const k in RANDOM_BYTES)fillRandom(k)}
function toggleShow(){document.querySelectorAll('#secrets input').forEach(i=>i.type=i.type==='password'?'text':'password')}
function bindInputs(){const has$=Object.values(collect()).some(v=>v.includes('$'));$('dollar-warn').style.display=has$?'block':'none'}
function collect(){const o={};document.querySelectorAll('input[id^="k:"]').forEach(i=>o[i.id.slice(2)]=i.value.trim());return o}
async function saveConfig(){
 const body={target_dir:$('k:target_dir')?.value||'',values:collect(),files:{}};
 document.querySelectorAll('textarea[id^="f:"]').forEach(t=>body.files[t.id.slice(2)]=t.value);
 const r=await(await fetch('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
 toast(r.ok?'配置已保存'+(r.warnings?'，但有警告：'+r.warnings:''):'保存失败: '+r.error,r.ok)}
async function runInstall(){
 const v=collect();
 const body={skip_engine:$('skip-engine').checked,target_dir:v.target_dir,
   app_jar:v.app_jar||'',frontend:v.frontend||'',tag:v.tag||'',
   docker_apt_mirror:v.docker_apt_mirror||'',registry_mirror:v.registry_mirror||''};
 const r=await(await fetch('/api/install',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})).json();
 if(!r.ok)return toast('无法启动: '+r.error,false);
 toast('安装已启动');logFrom=0;$('log').textContent='';startPoll()}
function startPoll(){if(timer)clearInterval(timer);timer=setInterval(refreshLog,1200)}
async function refreshLog(){
 const s=await(await fetch('/api/status?since='+logFrom)).json();
 if(s.lines.length){const log=$('log');s.lines.forEach(l=>log.textContent+=l+'\n');log.scrollTop=log.scrollHeight;logFrom+=s.lines.length}
 $('runstate').textContent=s.running?'运行中...':(s.exit_code===null?'未运行':(s.exit_code===0?'已完成（退出码 0）':'失败（退出码 '+s.exit_code+'）'));
 if(!s.running&&timer){clearInterval(timer);timer=null}}
async function doVerify(){
 const r=await(await fetch('/api/verify',{method:'POST'})).json();
 const tb=$('vtab').querySelector('tbody');tb.innerHTML='';
 for(const c of r.results)tb.insertAdjacentHTML('beforeend',
  `<tr><td>${c.name}</td><td class="st ${c.ok?'ok':'bad'}">${c.ok?'✓ 通过':'✗ 失败'}</td><td>${c.detail}</td></tr>`)}
load();startPoll();
</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默访问日志
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/config":
            self._json(self.get_config())
        elif self.path.startswith("/api/status"):
            since = int(self.path.split("since=")[-1]) if "since=" in self.path else 0
            with JOB.lock:
                lines = list(JOB.lines)[since:]
                self._json({"running": JOB.running, "exit_code": JOB.exit_code, "lines": lines})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/api/config":
            self._json(self.save_config(payload))
        elif self.path == "/api/install":
            self._json(self.start_install(payload))
        elif self.path == "/api/verify":
            target = payload.get("target_dir") or DEFAULT_TARGET
            self._json({"results": run_verify(target)})
        else:
            self._json({"error": "not found"}, 404)

    # ---- 业务 ----
    def get_config(self):
        template = (SRC / ".env.example").read_text()
        env = parse_env(template)
        target_env_path = Path(DEFAULT_TARGET) / ".env"
        if target_env_path.is_file():
            env.update({k: v for k, v in parse_env(target_env_path.read_text()).items()})
        params, secrets = [], []
        for k, v in env.items():
            label = FIELD_LABELS.get(k, k)
            (secrets if k in SECRET_KEYS else params).append([k, label, v])
        params.insert(0, ["target_dir", "部署目录", DEFAULT_TARGET])
        return {
            "mode": MODE, "is_root": os.geteuid() == 0 if hasattr(os, "geteuid") else True,
            "bundle_tag": BUNDLE_TAG, "params": params, "secrets": secrets,
            "files": [{"path": p, "label": label, "content": (SRC / p).read_text()}
                      for p, label in EDITABLE_FILES.items()],
        }

    def save_config(self, payload):
        warnings = []
        values = payload.get("values", {})
        target = payload.get("target_dir") or DEFAULT_TARGET
        if not target.startswith("/"):
            return {"ok": False, "error": "部署目录必须是绝对路径"}
        for k, v in values.items():
            if k in SECRET_KEYS and "$" in v:
                warnings.append(f"{k} 含 $")
        tag = values.get("tag") or BUNDLE_TAG or parse_env((SRC / ".env.example").read_text()).get("CICT_IMAGE_TAG", "")
        if MODE == "online" and not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", tag or ""):
            return {"ok": False, "error": "在线模式需要有效的唯一版本号"}
        try:
            template = (SRC / ".env.example").read_text()
            content = render_env(template, values, tag)
            target_dir = Path(target)
            target_dir.mkdir(parents=True, exist_ok=True)
            env_path = target_dir / ".env"
            env_path.write_text(content)
            os.chmod(env_path, 0o600)
            saved_files = []
            for rel, content in payload.get("files", {}).items():
                if rel not in EDITABLE_FILES:
                    return {"ok": False, "error": f"不允许写入: {rel}"}
                (SRC / rel).write_text(content)
                saved_files.append(rel)
            return {"ok": True, "warnings": "；".join(warnings), "saved_files": saved_files}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def start_install(self, payload):
        if os.geteuid() != 0:
            return {"ok": False, "error": "本程序未以 sudo 运行，install.sh 需要 root"}
        if JOB.running:
            return {"ok": False, "error": "已有安装任务在运行"}
        cmd = [str(INSTALL_SH), "--target-dir", payload.get("target_dir") or DEFAULT_TARGET]
        if payload.get("skip_engine"):
            cmd.append("--skip-engine")
        values = {}
        target_env = Path(cmd[cmd.index("--target-dir") + 1]) / ".env"
        if target_env.is_file():
            values = parse_env(target_env.read_text())
        if MODE == "online":
            app_jar = payload.get("app_jar") or ""
            frontend = payload.get("frontend") or ""
            tag = payload.get("tag") or values.get("CICT_IMAGE_TAG", "")
            if not (app_jar and frontend and tag):
                return {"ok": False, "error": "在线模式需要提供 JAR 路径、前端路径和版本号（请先在①配置页填写并保存）"}
            cmd += ["--online", "--app-jar", app_jar, "--frontend", frontend, "--tag", tag]
        if not INSTALL_SH.is_file():
            return {"ok": False, "error": f"找不到 {INSTALL_SH}"}
        if payload.get("docker_apt_mirror"):
            cmd += ["--docker-apt-mirror", payload["docker_apt_mirror"]]
        if payload.get("registry_mirror"):
            cmd += ["--registry-mirror", payload["registry_mirror"]]
        return {"ok": JOB.start(cmd), "cmd": " ".join(cmd)}


def main():
    print(f"CICT 部署向导 | 模式: {MODE} | http://127.0.0.1:{PORT}")
    print("远程使用请先建立 SSH 隧道: ssh -L 8788:127.0.0.1:8788 <用户>@<服务器IP>")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
