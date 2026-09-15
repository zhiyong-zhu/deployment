# CICT 一键安装脚本（在线 / 离线）

主路线（Docker Compose）的一键交付工具。`install.sh` 是同一个脚本、两种模式：

| 模式 | 命令 | 执行位置 | 行为 |
|------|------|---------|------|
| 在线 | `sudo ./install.sh --online --app-jar <jar> --frontend <zip或目录> --tag <版本>` | 仓库 `deploy/bundle/` 下 | 从 Docker 官方 apt 源装 Docker Engine，在线拉取官方镜像并构建后端镜像 |
| 离线 | `sudo ./install.sh` | `build-bundle.sh` 产物的解压目录内 | 校验 manifest → 离线装 Docker Engine（本地 deb 仓库）→ 加载镜像 tar |

两种模式共用的后续步骤：同步项目文件到部署目录 → 生成随机密码 `.env`（600 权限）→ `compose up -d` → 健康等待 → 创建 RabbitMQ 管理账号 → 输出验收报告。脚本幂等：保留 `.env` 与数据卷，重跑即升级。

## 单文件便携安装器（在线部署）

`make-portable.sh` 把 deploy/ 全部文件内嵌为一个自解压可执行 `dist/cict-installer.run`（~50KB），部署现场只需携带 **cict-installer.run + JAR + 前端 zip** 三个文件：

```bash
./make-portable.sh                                   # 联网机器生成
sudo ./cict-installer.run                            # 目标机：不带参数直接启动 Web 部署向导
sudo ./cict-installer.run --online --app-jar <jar> --frontend <zip> --tag <版本>   # 或命令行直装
```

参数与 `install.sh` 完全一致。离线部署不适用（GB 级镜像不内嵌），仍使用 `build-bundle.sh` 的完整离线包。

## 图形界面（可选）

`deploy-gui.py`（单文件、仅 Python 3 标准库）提供 Web 向导：界面编辑参数/密码 → 一键调用 `install.sh` → 实时日志 → 逐项验证。在线模式在仓库 `deploy/gui/` 下启动，离线模式在 bundle 解压目录内启动（制包时自动打入）：

```bash
sudo python3 deploy-gui.py          # 浏览器（经 SSH 隧道）访问 http://127.0.0.1:8788
```

详见 `deploy/gui/README.md`。向导只是安装脚本的执行外壳，部署逻辑仍以 `install.sh` 为单一事实源。

制包脚本：

| 脚本 | 执行位置 | 作用 |
|------|---------|------|
| `build-bundle.sh` | 联网的 Ubuntu（与目标机同架构、同系统版本） | 拉取/构建全部镜像并导出 tar、下载 Docker Engine 离线 deb 包、组装项目文件，生成带 sha256 清单的完整离线安装包 |

## 制包（联网机器）

```bash
cd deploy/bundle
./build-bundle.sh \
  --app-jar /path/to/cict-dist-2.1.2.jar \
  --frontend /path/to/dist.zip \
  --tag 2.1.2
```

产物在 `deploy/bundle/dist/`：`cict-offline-bundle-<tag>.tar.gz` 与同名 `.sha256`。

要求：

- 已按部署指南 3.5 安装 Docker Engine 与 compose 插件（制包机同时充当镜像拉取与后端镜像构建机）
- `unzip`、`dpkg-dev`（打包 Engine 离线包时需要）
- `--tag` 每次发布必须唯一，禁止复用旧 tag 或 latest
- 目标机已装 Docker 时可加 `--skip-engine` 减小体积

密码不进安装包：`.env` 中的密码占位符由目标机上的 `install.sh` 用 `openssl rand` 现场生成，制包与传输环节全程不接触明文密码。

## 安装（目标机）

在线环境（见上表）。离线环境：

```bash
sha256sum -c cict-offline-bundle-<tag>.tar.gz.sha256   # 先核对整包
tar -xzf cict-offline-bundle-<tag>.tar.gz
cd cict-offline-bundle
sudo ./install.sh
```

- 默认部署到 `/data/app/cict-docker`，可用 `--target-dir` 覆盖；安装成功后部署路径、模式与镜像版本记录到 `/etc/cict-docker/deploy.env`，之后升级不带 `--target-dir` 时自动沿用上次路径
- 幂等：重复执行保留已有 `.env` 与 `data/` 数据，只同步项目文件并重新拉起服务，因此也用于**离线升级**（换新包重跑即可）；`.env.example` 新增的变量（如直连端口 `CICT_PORT`/`MARIADB_PORT`）会自动补齐到已有 `.env`
- 安装日志与验收输出写入 `<部署目录>/install-report.txt`（不含密码）

## 离线升级后端

按上文用新 JAR、新 `--tag` 重新制包并重跑 `install.sh` 即可；等价于部署指南 4.5 的手工流程。回滚 = 用旧 tag 的包重跑。

## 设计说明

- 镜像版本、服务编排以 `deploy/docker-compose.yml` 为单一事实源，制包脚本不另行维护镜像清单之外的版本逻辑
- `manifest.sha256` 覆盖 bundle 内全部文件；`images.sha256`、`docker-packages.sha256` 在各自子目录内二次校验
- Docker Engine 离线安装使用本地 apt 仓库（`file:` 源 + `--no-download`），安装完成后即删除该源，避免污染目标机 apt
