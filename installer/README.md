# CICT Ubuntu Installer 1.0

这是 CICT Ubuntu 24 直接安装工具。交付物是单个 Linux amd64 二进制和一个 JSON 配置文件；目标机不需要安装 Python、Node.js、Rust 或其他应用运行时。它会真实执行软件源配置、apt 安装、源码编译、配置文件写入、systemd 服务启动、前后端部署和验收。

工具只绑定 `127.0.0.1`，不会作为常驻 root Web 服务运行。执行安装时由操作者在 Ubuntu 虚拟机中显式启动：

```bash
sudo cict-installer --execute
```

浏览器访问 `http://127.0.0.1:8787`。如果通过 SSH 远程测试，使用：

```bash
ssh -L 8787:127.0.0.1:8787 <用户>@<Ubuntu-IP>
```

## 安装范围

- Ubuntu 24.04 amd64
- MariaDB 11.4，数据目录 `/data/db/mysql`
- Redis 7.4.10，源码编译到 `/data/app/redis`
- RabbitMQ 4.3 + Erlang 27
- OpenJDK 17
- Nginx 1.28.3，源码编译到 `/data/app/nginx`
- RustFS 1.0.0-rc.2
- CICT JAR 和前端静态文件

安装器要求在线访问软件源和 GitHub Release。离线安装包尚未接入此版本。

## 交付包安装

```bash
tar -xzf cict-ubuntu-installer-1.0.0.tar.gz
cd cict-ubuntu-installer-1.0.0
sudo ./install.sh
sudo cict-installer --execute
```

安装器会校验 Ubuntu 24.04、amd64、root 权限、磁盘空间、网络和发布文件路径。服务配置任务在包安装完成后并行执行；后端部署等待所有依赖服务完成。

配置文件为 `/etc/cict-installer/config.json`，安装脚本首次运行时从 `config.example.json` 创建，权限为 `600`。可以只编辑这个文件后启动安装器，页面输入只是覆盖配置文件中的非空字段。

## 发布文件

在 `/etc/cict-installer/config.json` 或页面填写虚拟机本地路径：

- 后端：`.jar` 文件，例如 `/tmp/cict-app.jar`
- 前端：包含 `index.html` 的目录或 zip，例如 `/tmp/cict-dist.zip`

密码由页面生成或手工输入。密码只写入 `/etc/cict/cict.conf`、`/etc/default/rustfs` 等受限文件，不写入安装报告和普通日志。

## 重新执行和卸载

重新执行会复用已下载文件、保留 MariaDB/RustFS 数据，并重新应用期望配置。RabbitMQ 和 MariaDB 业务密码会更新为本次输入值。

卸载脚本只删除安装器本身，不删除业务服务、数据目录、`/etc/cict` 或 `/data`：

```bash
sudo ./uninstall.sh
```

## 当前边界

- 只支持 Ubuntu 24.04 amd64 在线安装。
- 不自动启用 UFW，避免锁死非标准 SSH 端口；安装完成后按网络策略手工配置防火墙。
- 不自动配置 HTTPS 证书。
- MariaDB 和 RustFS 的数据不会在失败时自动回滚；失败时保留安装报告，便于修复后重试。

## 构建二进制

在有 Docker 的构建机上执行：

```bash
./build-binary.sh
./package.sh
```

脚本使用 Linux amd64 Python 构建环境生成单文件 ELF；PyInstaller 只参与构建，Ubuntu 目标机不需要 Python。

也可以直接在 Ubuntu amd64 构建机执行 `./build-binary-native.sh`。目标机运行包不需要安装 PyInstaller。
