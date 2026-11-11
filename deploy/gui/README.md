# CICT 运维控制台（Web UI）

单文件、仅依赖 Python 3 标准库的部署与运维控制台。**职责边界**：UI 只负责配置编辑、动作触发与结果展示；全部部署动作由 `deploy/bundle/install.sh` 完成，本程序不复制部署逻辑，install.sh 的幂等/密码/校验行为对 UI 完全一致。

## 启动

**推荐方式：systemd 常驻服务**（install.sh 安装末尾自动完成，服务名 `cict-ops`，目录 `/data/app/cict-ops`）：

```bash
systemctl status cict-ops          # 查看状态
sudo systemctl start cict-ops      # 安装向导占用 8788 时，向导退出后手工拉起
```

临时运行（仓库或安装包内）：

```bash
sudo python3 deploy/gui/deploy-gui.py    # 在线模式（deploy/gui/ 下）
sudo python3 deploy-gui.py               # 离线模式（bundle 解压目录内）
```

浏览器访问 `http://127.0.0.1:8788/?token=<令牌>`（令牌在安装完成输出与 `gui/console.env`）。远程操作先建 SSH 隧道：

```bash
ssh -L 8788:127.0.0.1:8788 <用户>@<服务器IP>
```

## 界面（五个页签）

1. **① 概览**：部署目录/镜像版本/上次安装时间、访问地址总表（Web 入口、API 直连、数据库、RustFS S3/控制台、RabbitMQ 管理隧道提示）、服务与端口监听状态、一键健康验证
2. **② 配置**：
   - `.env` 键值编辑（含端口/绑定地址/S3 对外地址等，取值校验会把"缺 http:// 前缀""端口非法"这类问题在保存前拦下），保存只写文件不重建容器
   - 部署目录配置文件直编（docker-compose.yml / nginx / application.properties），保存前自动 `docker compose config` 与 `nginx -t` 预检
   - **应用变更** = `docker compose up -d`（重建配置有变化的容器；检测到 application.properties 更新会自动补 `restart cict` 让 JVM 重读配置）
   - 注意：**重启容器不会重读 .env**，改 `.env` 后必须"应用变更"
3. **③ 升级**：后端 JAR 上传独立升级（备份旧包→重建镜像→重建 cict 容器）、前端 zip 上传独立升级（bind mount 实时生效）、完整安装/重部署（等价命令行重跑 install.sh，JAR/前端可只填其一）、**控制台自更新**（GitHub 检查更新或上传新版 cict-installer.run）
4. **④ 服务**：六个组件各自 启动/停止/重启（互不影响其他组件）、端口监听检测、5 秒自动刷新
5. **⑤ 日志**：按服务查看 `docker logs --tail`（≤2000 行），支持每 2 秒跟随刷新

所有长任务（安装/应用/升级/服务操作）共用底部"任务输出"抽屉实时滚动，显示退出码。

## 自更新

控制台文件位于 `/data/app/cict-ops`（布局同 deploy/）。更新只覆盖控制台自身文件（gui/bundle/compose 模板），不动部署目录、`.env` 密码与访问令牌；更新后 systemd 服务自动重启。版本号来自发布 tag（make-portable.sh 打包时写入 `gui/VERSION`）。内网环境用"上传 .run 更新"。

## 安全边界

- 只绑定 `127.0.0.1:8788`，不对外监听；远程一律走 SSH 隧道
- **访问令牌**：`gui/console.env` 的 `SERVER_TOKEN`（安装时随机生成），所有 `/api/*` 校验 `X-Token` 头或 `?token=`；未配置时退化为无鉴权（仅限一次性安装向导场景，常驻服务必须配置）
- 以 root 运行（控制 docker 与写部署目录所需）；容器日志可能包含业务数据，不要把隧道转发给不可信网络
- 密码只写入 `.env`（600），不进入安装报告与日志
