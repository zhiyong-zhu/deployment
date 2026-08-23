# CICT Deployment

- [部署文档](./deployment-guide.md)
- [Docker 主路线：部署文件与一键安装](./deploy/)（推荐，含 Web UI 部署向导）
- [Ubuntu 直装安装器](./installer/README.md)（备选路线，仅在线）

## 推荐部署形态

Windows 宿主机（Hyper-V / VMware 虚拟机，见部署文档第 2 章）或实体 Ubuntu 24.04 服务器 + Docker Compose，见部署文档第 1 章选型说明。

## 一键安装（Docker 主路线）

在线部署（目标机直接联网，一条命令完成 Docker 安装到验收）：

```bash
cd deploy
sudo ./bundle/install.sh --online --app-jar <jar> --frontend <zip或目录> --tag <唯一版本号>
```

也可先生成单文件便携安装器（部署现场只需 `cict-installer.run` + JAR + 前端 zip 三个文件）：

```bash
cd deploy/bundle && ./make-portable.sh
sudo ./cict-installer.run --online --app-jar <jar> --frontend <zip> --tag <唯一版本号>   # 目标机
sudo ./cict-installer.run --gui                                                          # 或 Web 向导
```

离线部署（联网机器制包，与目标机同架构同系统版本，已装 Docker、unzip、dpkg-dev）：

```bash
cd deploy/bundle
./build-bundle.sh --app-jar <jar> --frontend <zip或目录> --tag <唯一版本号>
```

目标机安装（幂等，重复执行即离线升级）：

```bash
sha256sum -c cict-offline-bundle-<tag>.tar.gz.sha256
tar -xzf cict-offline-bundle-<tag>.tar.gz
cd cict-offline-bundle && sudo ./install.sh
```

详见 [deploy/bundle/README.md](./deploy/bundle/README.md)。密码不进安装包，由目标机安装时随机生成。

## Ubuntu 直装安装器（备选路线）

```bash
cd installer
./build-binary-native.sh
./package.sh
```

生成的单文件二进制和 tar 包位于 `installer/dist/`。目标 Ubuntu 不需要安装 Python；构建机需要 Python、pip 和 PyInstaller，或者使用 `./build-binary.sh` 通过 Docker 构建。
