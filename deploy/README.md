# CICT Docker Compose 部署目录（主路线）

本目录是 Docker 主路线的部署文件实物，与 `deployment-guide.md` 第 3、4 章一一对应：

```
deploy/
├── docker-compose.yml          # 编排主文件（服务、健康检查、网络、端口）
├── .env.example                # 环境变量模板（install.sh 生成随机密码）
├── cict/
│   ├── Dockerfile              # 后端镜像（eclipse-temurin 17，UID 10001）
│   └── application.properties  # Spring Boot 配置（地址使用 Compose 服务名）
├── nginx/
│   ├── nginx.conf
│   └── conf.d/cict.conf        # 前端站点 + /api/ 反向代理
├── mariadb/server.cnf          # MariaDB 11.4 调优配置
├── bundle/                     # 一键安装脚本（install.sh 在线/离线 + build-bundle.sh 制包）
└── gui/                        # 部署向导 Web UI（单文件标准库，install.sh 的图形外壳）
```

## 在线部署

目标机（Ubuntu 24.04 amd64，可联网）克隆仓库后一条命令：

```bash
cd deploy
sudo ./bundle/install.sh --online --app-jar <jar> --frontend <zip或目录> --tag <唯一版本号>
```

自动完成：Docker Engine 安装、日志轮转、目录与随机密码 `.env`、镜像拉取与后端构建、`compose up`、健康等待与验收报告。幂等，重跑即升级（换新 tag）。

## 离线部署（推荐走一键包）

在联网机器上执行：

```bash
cd bundle
./build-bundle.sh --app-jar <jar> --frontend <zip或目录> --tag <唯一版本号>
```

目标机解压安装包后 `sudo ./install.sh`，详见 [bundle/README.md](./bundle/README.md)。
