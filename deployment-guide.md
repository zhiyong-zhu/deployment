# 部署指南

> 本文档定义 CICT 生产环境的标准部署形态：**Ubuntu 24.04 LTS（虚拟机或物理机）+ Docker Compose**，主路线覆盖在线与离线场景；systemd 直装作为备选路线保留。Windows 宿主机上的虚拟机准备见第 2 章。
>
> 后端通过 Docker 容器（主路线）或 systemd（备选路线）运行 Spring Boot JAR 包，无需 Tomcat。
>
> 修订基线：2026-08-22。执行生产部署前仍需在与目标环境相同的系统版本、CPU 架构和网络条件下完成一次全流程验收。

---

## 目录

- [第 1 章 部署方案与选型](#第-1-章-部署方案与选型)
- [第 2 章 Windows 宿主机准备](#第-2-章-windows-宿主机准备)
- [第 3 章 主路线：Docker Compose 在线部署](#第-3-章-主路线-docker-compose-在线部署)
- [第 4 章 主路线：离线打包与离线安装](#第-4-章-主路线离线打包与离线安装)
- [第 5 章 备选路线：Ubuntu 直装在线安装](#第-5-章-备选路线ubuntu-直装在线安装)
- [第 6 章 备选路线：Ubuntu 直装离线安装](#第-6-章-备选路线ubuntu-直装离线安装)
- [附录 A：更新流程与运维速查](#附录-a更新流程与运维速查)
- [附录 B：CentOS 与 Ubuntu 差异对照](#附录-bcentos-与-ubuntu-差异对照)
- [附录 C：官方参考资料](#附录-c官方参考资料)

---

## 组件版本总览

| 组件 | 版本 | 直接安装方式 | Docker 镜像 |
|------|------|------------|------------|
| MariaDB | 11.4.x | apt 源 / deb 包 | `mariadb:11.4` |
| Redis | 7.4.x | 源码编译 | `redis:7.4.10` |
| RabbitMQ | 4.3.x | Team RabbitMQ apt 源 / deb 包 | `rabbitmq:4.3.4-management` |
| Erlang | 27.x | Team RabbitMQ apt 源 / deb 包 | （包含在 RabbitMQ 镜像中） |
| JDK | 17.x | apt / deb 包 | `eclipse-temurin:17-jre-jammy` |
| Nginx | 1.28.x | 源码编译 | `nginx:1.28.3` |
| RustFS | v1.0.0-rc.2 | MUSL 二进制包 | `rustfs/rustfs:1.0.0-rc.2` |
| 后端服务 | CICT JAR | systemd 直接运行 | Docker 容器运行 |

> **建议起步规格**：4 vCPU、16GB RAM、100GB 可用 SSD；生产容量应根据并发量、对象数据量和保留周期重新评估。示例中的 JVM 4GB 与 MariaDB buffer pool 4GB 已占用约 8GB，不能直接套用到低内存主机。

> 本文版本用于**全新部署**。现有 RabbitMQ 3.8 数据目录不能直接挂载给 4.3，必须按官方支持路径逐级升级，或导出定义并迁移消息；MariaDB、Redis 和 RustFS 升级也必须先完成兼容性评估与可恢复备份。

---

## 第 1 章 部署方案与选型

### 1.1 推荐架构

生产环境的标准形态：

**Windows 宿主机 → Hyper-V / VMware 虚拟机（Ubuntu Server 24.04 LTS）→ Docker Compose 编排全部服务**

Docker 主机不限于虚拟机：**实体 Ubuntu 服务器、云主机与虚拟机完全等同**，按第 3、4 章部署即可。Windows 宿主机上的虚拟机准备见第 2 章。

### 1.2 部署路线矩阵

| 路线 | 章节 | 适用场景 |
|------|------|---------|
| 主路线：Docker Compose | 第 3 章（在线）/ 第 4 章（离线） | 默认选择：组件级升级回滚、镜像化离线分发、测试与生产环境一致 |
| 备选路线：systemd 直装 | 第 5 章（在线）/ 第 6 章（离线） | 现场不允许使用容器，或运维团队只具备 systemd 技能 |

### 1.3 为什么以 Docker Compose 为主路线

- **离线分发**：整套服务以镜像 tar 交付，版本即镜像 tag，可校验、可整体回退。直装路线的离线等价物是逐组件维护 deb/源码下载清单，环节多、在目标机环境差异上容易失败。
- **组件级生命周期**：升级 RabbitMQ 不影响 MariaDB，出问题按 tag 回滚到上一版本。
- **环境一致性**：验收通过的镜像与生产运行的是同一不可变工件，直接降低"目标机环境差异"类故障的排查成本。

直装路线的价值是少一层抽象、排障路径最短；其代价与边界见第 5 章开头。两条路线的运维命令对照见附录 A.6。

### 1.4 宿主操作系统：Ubuntu 24.04 LTS（当前基线）

选型依据：

- Redis 官方 apt 源只发布 Ubuntu/Debian 包，RHEL 系发行版只能源码编译 Redis
- MariaDB、RabbitMQ/Erlang、Docker 官方仓库均把 Ubuntu LTS 作为第一优先支持对象
- 24.04 标准支持至 2029 年 4 月，可延长至 2034 年

**Ubuntu 26.04 LTS 评估（2026-08）**：26.04 已于 2026 年 4 月发布，Docker 官方 apt 源已提供支持。

- 主路线（Docker）：宿主机只依赖内核与 Docker Engine，26.04 在完成一次全流程验收后即可使用；迁移方式为重建主机 + 恢复数据卷，无紧迫性。
- 备选路线（直装）：依赖 MariaDB、RabbitMQ/Erlang 等第三方 apt 源对 26.04 的适配进度，需逐源确认有对应版本包后再切换。
- 结论：**当前基线维持 24.04。26.04 不是不合适，而是按路线分批切换**：主路线先验收先切，直装路线等第三方源齐备。CentOS Linux 已停止维护，不在选型范围内（历史差异见附录 B）。

### 1.5 不采用的 Windows 部署方式

- **Windows 原生安装 + Docker 补 Redis**：Redis 没有官方 Windows 版本（微软移植停留在 3.x，社区 fork 停留在 5.0.14）；Nginx 官方 Windows 版只有一个 worker 进程，上游定位为评估用途；Docker Desktop 不支持 Windows Server，混合方案无法覆盖服务器场景。
- **Windows + Docker Desktop**：面向开发环境，存在商用授权、WSL2 数据落盘（VHDX 绑定用户会话）、开机自启等运维问题，不作为生产路径（早期版本文档中的 Windows Docker Desktop 章节已移除）。开发/演示一律使用第 2 章的虚拟机方案。

---

## 第 2 章 Windows 宿主机准备

> 目标：在 Windows 宿主机上得到一台行为等同物理机的 Ubuntu Server 24.04 amd64 主机。Docker 主机若为实体 Ubuntu 服务器或云主机，直接进入第 3、4 章，本章可跳过。

### 2.1 虚拟机选型

| 方案 | 适用条件 | 说明 |
|------|---------|------|
| Hyper-V | Windows 11/Server 专业版、企业版、教育版 | 系统自带、免费；Type-1 架构，性能接近原生 |
| VMware Workstation Pro | Windows 家庭版，或已有 VMware 体系 | 已免费商用；若宿主机同时启用 Hyper-V/WSL2，VMware 以兼容模式运行、性能打折，两者二选一 |

虚拟化性能损耗参考：CPU 与内存接近原生（约 1%~5%），磁盘 I/O 约 5%~15%，对本套服务不构成决策因素。真正的成本是**内存划分**：分配给虚拟机的 16GB 即被钉死，宿主机自身还需 8GB 以上，因此宿主机建议 32GB 内存起步。

### 2.2 虚拟机资源配置

| 项目 | 建议值 | 说明 |
|------|--------|------|
| vCPU / 内存 | 4 vCPU / 16GB | JVM 4G 堆 + MariaDB buffer pool 4G 已占约 8G |
| 磁盘 | 100GB+ SSD，**固定大小**虚拟磁盘 | 动态扩展磁盘性能与容量可预测性差；MariaDB、Redis、RustFS 数据均在 `/data` |
| 动态内存 | 关闭 | 数据库服务需要稳定内存 |
| 网络 | Hyper-V 外部虚拟交换机 / VMware 桥接模式 | 虚拟机获得局域网 IP，配置静态 IP 或 DHCP 保留地址 |
| 系统 | Ubuntu Server 24.04 LTS amd64 | 不要桌面版，节省资源 |

系统装好后打一个快照/检查点（Hyper-V 使用生产检查点）再进入第 3 章部署，部署失败可整体回滚；**验收通过后删除临时检查点**，避免长期携带性能损耗。

### 2.3 实体 Ubuntu 服务器 / 云主机预检清单

不经过虚拟化时，按以下清单确认后直接进入第 3 章（在线）或第 4 章（离线）：

1. `cat /etc/os-release` 确认 Ubuntu 24.04 amd64
2. `/data` 独立分区或磁盘，可用空间 ≥100GB（虚拟机场景同样建议单独规划）
3. 静态 IP 或 DHCP 保留地址
4. SSH 可达

### 2.4 防火墙与安全基线

- 主机上线前手工配置防火墙：默认只放行 SSH 与 NGINX 对外端口（默认 80）；RabbitMQ 管理端口 15672、RustFS 9000/9001 保持绑定 `127.0.0.1`，远程访问一律走 SSH 隧道
- 生产环境按网络策略另行评估 HTTPS 证书（本文档以 80 端口作为首次部署验证基线）

---

## 第 3 章 主路线：Docker Compose 在线部署

> 通过 Docker Compose 编排全部服务，Docker 主机可以是虚拟机或实体 Ubuntu 服务器。Docker 环境中服务间通过 Docker 内部 DNS 通信，数据库地址使用服务名而非 `127.0.0.1`。
>
> 本仓库 `deploy/` 目录已提供本章全部项目文件实物（compose、配置、Dockerfile），可直接复制到目标机；本章命令用于说明文件内容与生成随机密码的方式。

### 3.0 一键安装（推荐入口）

> 本章其余小节保留手工步骤，作为原理说明与排障参考；日常部署直接使用本节，无需手工执行 3.5~3.10。

在目标机（Ubuntu 24.04 amd64，可访问互联网）上获得本仓库后执行：

```bash
cd deploy
sudo ./bundle/install.sh --online \
  --app-jar /path/to/cict-dist-2.1.2.jar \
  --frontend /path/to/dist.zip \
  --tag 2.1.2
```

脚本与第 4 章离线安装是**同一个 `install.sh`**：`--online` 模式自动从 Docker 官方 apt 源安装 Docker Engine（3.5）、配置日志轮转（3.6）、拉取官方镜像并构建后端镜像（3.8），其余步骤（生成随机密码 `.env`、`compose up`、健康等待、RabbitMQ 管理账号、验收报告）与离线安装完全一致。

- 幂等：重复执行保留 `.env` 密码与 `data/` 数据，因此**日常升级 = 重跑本命令并换新 tag**（版本号必须唯一），详见附录 A.3
- 已装 Docker 的机器加 `--skip-engine`
- 仅剩的手工步骤：3.10 创建 RustFS 存储桶 `pike`（脚本结束的报告中已给出操作提示）

**图形界面（可选）**：不习惯命令行的操作者可使用部署向导：

```bash
sudo python3 deploy/gui/deploy-gui.py     # 浏览器经 SSH 隧道访问 http://127.0.0.1:8788
```

在界面中编辑参数与密码、一键执行安装并查看逐项验证结果。向导只是 `install.sh` 的配置编辑与执行外壳（单文件、仅标准库、零依赖），行为与本节命令完全一致，详见 `deploy/gui/README.md`。

### 3.1 架构总览

```
                    ┌─────────────────────────────────────────┐
                    │           Docker Host (Ubuntu 24)        │
                    │                                         │
   用户 ──:80──────► │  nginx ◄── proxy_pass ──► cict (:9090)  │
                    │    │                      │             │
                    │    │ static files          │             │
                    │    │                      │             │
   :15672 ─────────►  rabbitmq ◄────────────────┘             │
   :9000  ─────────►  rustfs                                   │
                    │    │                      │             │
                    │  mariadb ◄────────────────┘             │
                    │  redis   ◄────────────────┘             │
                    └─────────────────────────────────────────┘
```

- **Nginx** 对外暴露 80 端口，前端静态文件由 Nginx 直接提供，`/api/` 路径反向代理到后端
- **后端 CICT** 容器内部监听 9090 端口，仅在 Docker 内部网络中与 Nginx 通信
- **RabbitMQ 管理界面** 暴露 15672 端口，**RustFS S3 API** 暴露 9000 端口，**RustFS 控制台** 暴露 9001 端口
- MariaDB、Redis 和 RabbitMQ 的业务端口不发布到宿主机，只在 `cict-net` 内部网络使用

### 3.2 项目目录结构

```bash
sudo apt install -y unzip rsync
sudo mkdir -p /data/app/cict-docker
cd /data/app/cict-docker
```

```
/data/app/cict-docker/
├── docker-compose.yml          # 编排主文件
├── .env                        # 环境变量（密码、端口等）
├── cict/
│   ├── Dockerfile              # 后端镜像构建文件
│   ├── app.jar                 # 后端 JAR（固定文件名，更新时直接覆盖）
│   └── application.properties  # Spring Boot 配置
├── nginx/
│   ├── nginx.conf              # Nginx 主配置
│   ├── conf.d/
│   │   └── cict.conf           # 前端站点与后端代理配置
│   └── html/                   # 前端静态文件目录
├── mariadb/
│   └── server.cnf              # MariaDB 11.4 自定义配置
├── data/                       # 数据持久化目录
│   ├── mariadb/
│   ├── redis/
│   ├── rabbitmq/
│   └── rustfs/
└── logs/                       # 日志目录
    ├── cict/
    ├── nginx/
    └── rustfs/
```

```bash
sudo mkdir -p /data/app/cict-docker/{cict,nginx/{conf.d,html},mariadb,data/{mariadb,redis,rabbitmq,rustfs},logs/{cict,nginx,rustfs}}
sudo chown -R "$(id -un):$(id -gn)" /data/app/cict-docker
# Dockerfile 中的后端用户固定为 UID/GID 10001
sudo chown -R 10001:10001 /data/app/cict-docker/logs/cict
```

### 3.3 配置文件

> **重要**：第 3、4 章共用以下配置文件，Docker 部署的配置与直接安装不同——服务间通过 Docker 内部 DNS 通信，地址使用服务名。

#### 3.3.1 环境变量 `.env`

```bash
sudo apt install -y openssl
cd /data/app/cict-docker
umask 077

MARIADB_ROOT_PASSWORD="$(openssl rand -hex 24)"
MARIADB_PASSWORD="$(openssl rand -hex 24)"
REDIS_PASSWORD="$(openssl rand -hex 24)"
RABBITMQ_PASSWORD="$(openssl rand -hex 24)"
RABBITMQ_ADMIN_PASSWORD="$(openssl rand -hex 24)"
RUSTFS_ACCESS_KEY="$(openssl rand -hex 16)"
RUSTFS_SECRET_KEY="$(openssl rand -hex 32)"

tee .env >/dev/null <<EOF
# ===== MariaDB =====
MARIADB_ROOT_PASSWORD=${MARIADB_ROOT_PASSWORD}
MARIADB_DATABASE=cict
MARIADB_USER=cict
MARIADB_PASSWORD=${MARIADB_PASSWORD}

# ===== Redis =====
REDIS_PASSWORD=${REDIS_PASSWORD}

# ===== RabbitMQ =====
RABBITMQ_DEFAULT_USER=cict
RABBITMQ_DEFAULT_PASS=${RABBITMQ_PASSWORD}
RABBITMQ_ADMIN_PASSWORD=${RABBITMQ_ADMIN_PASSWORD}

# ===== RustFS =====
RUSTFS_ACCESS_KEY=${RUSTFS_ACCESS_KEY}
RUSTFS_SECRET_KEY=${RUSTFS_SECRET_KEY}

# ===== CICT 后端 =====
JAVA_TOOL_OPTIONS=-Xms1g -Xmx4g
CICT_IMAGE_TAG=2.1.2-SNAPSHOT

# ===== 对外端口 =====
NGINX_PORT=80
ADMIN_BIND_ADDRESS=127.0.0.1
RABBITMQ_MGMT_PORT=15672
RUSTFS_PORT=9000
RUSTFS_CONSOLE_PORT=9001
EOF

chmod 600 .env
unset MARIADB_ROOT_PASSWORD MARIADB_PASSWORD REDIS_PASSWORD RABBITMQ_PASSWORD RABBITMQ_ADMIN_PASSWORD RUSTFS_ACCESS_KEY RUSTFS_SECRET_KEY
```

> 上述命令会在本机生成随机密码，不会把公开示例密码带入部署。请将 `.env` 安全备份到密码管理系统；文件不得提交到 Git。管理端口默认只绑定 `127.0.0.1`，远程管理请使用 SSH 隧道或改为明确的管理网 IP，不要直接绑定公网地址。

实际密码最终保存在 `/data/app/cict-docker/.env`。执行完上述脚本后不需要再手工填写；如果公司已有指定密码，可执行 `nano /data/app/cict-docker/.env`，直接修改等号右侧的值，例如：

```dotenv
MARIADB_ROOT_PASSWORD=实际的数据库root密码
MARIADB_PASSWORD=实际的cict数据库用户密码
REDIS_PASSWORD=实际的Redis密码
RABBITMQ_DEFAULT_PASS=实际的RabbitMQ应用账号密码
RABBITMQ_ADMIN_PASSWORD=实际的RabbitMQ管理账号密码
RUSTFS_ACCESS_KEY=实际的RustFS访问Key
RUSTFS_SECRET_KEY=实际的RustFS密钥
JAVA_TOOL_OPTIONS=-Xms1g -Xmx4g
```

> 不要保留“实际的……”这些说明文字。推荐使用脚本生成的十六进制随机值；若手工密码包含 `$`，在 Compose `.env` 中需写成 `$$`，否则可能被当成变量插值。

主要变量的用途如下：

| `.env` 变量 | 使用方 | 作用 |
|-------------|--------|------|
| `MARIADB_ROOT_PASSWORD` | MariaDB 容器 | 首次初始化数据库 root 账号 |
| `MARIADB_USER` / `MARIADB_PASSWORD` | MariaDB、CICT | 创建并连接 `cict` 业务账号 |
| `REDIS_PASSWORD` | Redis、CICT | Redis 认证 |
| `RABBITMQ_DEFAULT_USER` / `RABBITMQ_DEFAULT_PASS` | RabbitMQ、CICT | 消息队列应用账号 |
| `RABBITMQ_ADMIN_PASSWORD` | RabbitMQ 初始化命令 | 创建 `cict_admin` 管理账号 |
| `RUSTFS_ACCESS_KEY` / `RUSTFS_SECRET_KEY` | RustFS、CICT | S3 API 认证 |
| `JAVA_TOOL_OPTIONS` | CICT 容器中的 JVM | JVM 堆内存等启动参数 |
| `CICT_IMAGE_TAG` | Docker Compose | 后端镜像版本标签 |

#### 3.3.2 后端 Spring Boot 配置 `cict/application.properties`

```bash
sudo tee /data/app/cict-docker/cict/application.properties <<'EOF'
# ===== API 白名单 =====
pike.api.white-list[0]=/api/generator/**
pike.api.white-list[1]=/ssoLogin/sso/social-callback/**
pike.api.white-list[2]=/ssoLogin/auth-binding/**
pike.api.white-list[3]=/py
pike.api.white-list[4]=/api/v1/**
pike.api.white-list[5]=/webhook/trigger/**
pike.api.white-list[6]=/brand-config/public
pike.api.white-list[7]=/brand-config/asset/**
pike.api.white-list[8]=/auth/login

# ===== AutoCrud 扫描包配置 =====
autocrud.base-packages=com.polelink

# ===== 数据库配置（Docker 服务名 mariadb） =====
spring.datasource.url=jdbc:mariadb://mariadb:3306/cict
spring.datasource.username=${MARIADB_USER}
spring.datasource.password=${MARIADB_PASSWORD}

# ===== Quartz 配置 =====
spring.quartz.properties.org.quartz.jobStore.tablePrefix=qrtz_

# ===== Redis 配置（Spring Boot 2.x） =====
spring.redis.host=redis
spring.redis.port=6379
spring.redis.password=${REDIS_PASSWORD}

# ===== RabbitMQ 配置 =====
spring.rabbitmq.host=rabbitmq
spring.rabbitmq.port=5672
spring.rabbitmq.username=${RABBITMQ_DEFAULT_USER}
spring.rabbitmq.password=${RABBITMQ_DEFAULT_PASS}

# ===== RustFS / S3 配置（Docker 服务名 rustfs） =====
pike.file.s3.endpoint=http://rustfs:9000
pike.file.s3.bucket=pike
pike.file.s3.accessKey=${RUSTFS_ACCESS_KEY}
pike.file.s3.secretKey=${RUSTFS_SECRET_KEY}

# ===== 日志配置 =====
logging.file.path=/data/logs
logging.file.name=/data/logs/cict.log

# ===== 服务端口 =====
server.port=9090
EOF
```

> Spring Boot 会从进程环境解析 `${MARIADB_PASSWORD}` 等占位符。与直接部署不同，Docker 环境中的数据库、Redis、RabbitMQ 和 RustFS 地址必须使用 Compose 服务名。若项目使用 Spring Boot 3.x，请将 `spring.redis.*` 改为 `spring.data.redis.*`，并以项目实际配置元数据为准。

密码传递链路是：`.env` → `docker-compose.yml` 的 `environment` → CICT 容器环境变量 → Spring Boot 解析 `application.properties` 中的 `${...}`。Compose 读取 `.env` 并不等于自动把全部变量送进容器，所以 3.4 的 `cict.environment` 必须显式列出应用需要的每个变量。

#### 3.3.3 后端 Dockerfile `cict/Dockerfile`

```dockerfile
FROM eclipse-temurin:17-jre-jammy

LABEL maintainer="CICT Team"

WORKDIR /app

RUN groupadd --system --gid 10001 cict \
    && useradd --system --uid 10001 --gid 10001 --home-dir /app cict \
    && mkdir -p /data/logs \
    && chown -R cict:cict /app /data/logs

COPY --chown=cict:cict app.jar /app/app.jar

COPY application.properties /etc/cict/application.properties

EXPOSE 9090

USER cict

ENTRYPOINT ["java", "-jar", "/app/app.jar", "--spring.config.additional-location=/etc/cict/application.properties"]
```

> Docker 版不再在 `ENTRYPOINT` 中展开 `$JAVA_OPTS`。Compose 将 `.env` 中的 `JAVA_TOOL_OPTIONS` 注入容器，JVM 启动时会自动读取它，因此 Dockerfile 中无需再次引用该变量。

#### 3.3.4 MariaDB 配置文件 `mariadb/server.cnf`

Docker 使用镜像默认数据目录 `/var/lib/mysql`，只复用与数据路径无关的参数：

```bash
sudo tee /data/app/cict-docker/mariadb/server.cnf <<'EOF'
[mysqld]

# ===== 性能与安全 =====
lower_case_table_names=1
performance_schema=1
skip-name-resolve=ON
log_bin_trust_function_creators = 1
max_sp_recursion_depth = 10

# ===== 字符集 =====
character-set-server = utf8mb4
collation-server = utf8mb4_general_ci
init_connect='SET NAMES utf8mb4 COLLATE utf8mb4_general_ci'
skip-character-set-client-handshake

# ===== 事务隔离 =====
transaction_isolation = READ-COMMITTED

# ===== 连接与线程 =====
back_log = 1000
max_connections = 500
thread_cache_size = 100
thread_stack = 256K

# ===== 表缓存 =====
table_open_cache = 8192
table_definition_cache = 8192

# ===== 数据包与缓冲 =====
max_allowed_packet = 64M
binlog_cache_size = 1M
max_heap_table_size = 128M
tmp_table_size = 64M

# ===== 读写缓冲（per-connection） =====
read_buffer_size = 1M
read_rnd_buffer_size = 1M
join_buffer_size = 1M

# ===== InnoDB 优化 =====
innodb_buffer_pool_size = 4G
innodb_log_file_size = 512M
innodb_flush_log_at_trx_commit = 1
innodb_io_capacity = 2000

# ===== 二进制日志 =====
log-bin=mysql-bin
binlog_format=row
binlog_expire_logs_seconds = 259200

# ===== 慢查询日志（排障时开启，并同步配置日志轮转） =====
slow_query_log = OFF
long_query_time = 2

# ===== 全文搜索 =====
innodb_ft_min_token_size = 4
ft_min_word_len = 4
EOF
```

> `lower_case_table_names=1` 必须在 MariaDB 数据卷首次初始化时生效。已使用默认值 `0` 初始化的 `./data/mariadb` 不能直接切换，应先逻辑备份，再用新数据卷初始化并恢复。

> 配置项修订说明见 [5.2.4 配置 MariaDB](#524-配置-mariadb) 中的配置修订表。

#### 3.3.5 Nginx 配置

`nginx/nginx.conf`：

```nginx
user  nginx;
worker_processes  auto;
error_log  /var/log/nginx/error.log warn;
pid        /var/run/nginx.pid;

events {
    worker_connections 10240;
}

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    gzip on;
    gzip_comp_level 6;
    gzip_min_length 1k;
    gzip_proxied any;
    gzip_vary on;
    gzip_types text/plain text/css application/json application/javascript
               application/xml application/xml+rss image/svg+xml;

    include /etc/nginx/conf.d/*.conf;
}
```

`nginx/conf.d/cict.conf`：

```nginx
server {
    listen 80 default_server;
    server_name _;
    root   /usr/share/nginx/html;
    index  index.html;

    # 尾斜杠会将对外的 /api/ 前缀替换为后端的 /
    location /api/ {
        proxy_pass http://cict:9090/;
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

    location ~* \.(ico|jpg|jpeg|png|gif|svg|css|js|woff|woff2|ttf|eot)$ {
        expires 30d;
        access_log off;
    }
}
```

> 前端和 API 必须位于同一个默认虚拟主机中。`proxy_pass` 中的 `cict` 是 Docker Compose 服务名；结尾斜杠会把外部 `/api/auth/login` 映射为后端 `/auth/login`，避免将多余的 `/api` 前缀传给 Spring MVC。

生产环境还应增加 HTTPS 配置和证书挂载；本节的 80 端口仅作为首次部署验证基线。

如果从旧版文档升级，删除遗留的 `nginx/conf.d/frontend.conf` 和 `backend.conf`，目录中只保留新的 `cict.conf`。

### 3.4 docker-compose.yml

```bash
sudo tee /data/app/cict-docker/docker-compose.yml <<'EOF'
services:
  # ============ MariaDB 11.4 ============
  mariadb:
    image: mariadb:11.4
    container_name: cict-mariadb
    restart: unless-stopped
    environment:
      MARIADB_ROOT_PASSWORD: ${MARIADB_ROOT_PASSWORD}
      MARIADB_DATABASE: ${MARIADB_DATABASE}
      MARIADB_USER: ${MARIADB_USER}
      MARIADB_PASSWORD: ${MARIADB_PASSWORD}
    volumes:
      - ./mariadb/server.cnf:/etc/mysql/mariadb.conf.d/server.cnf:ro
      - ./data/mariadb:/var/lib/mysql
    healthcheck:
      test: ["CMD", "healthcheck.sh", "--connect", "--innodb_initialized"]
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 30s
    networks:
      - cict-net

  # ============ Redis 7.4 ============
  redis:
    image: redis:7.4.10
    container_name: cict-redis
    restart: unless-stopped
    environment:
      REDIS_PASSWORD: ${REDIS_PASSWORD}
    # 仅在 cict-net 内监听，不向宿主机发布 6379，并通过 requirepass 认证。
    # 若后续增加 ports 映射，必须同时配置宿主机防火墙和访问白名单。
    command: ["sh", "-c", "exec redis-server --bind 0.0.0.0 --protected-mode no --appendonly yes --requirepass \"$$REDIS_PASSWORD\""]
    volumes:
      - ./data/redis:/data
    healthcheck:
      test: ["CMD-SHELL", "REDISCLI_AUTH=\"$$REDIS_PASSWORD\" redis-cli ping | grep -q PONG"]
      interval: 10s
      timeout: 5s
      retries: 5
    networks:
      - cict-net

  # ============ RabbitMQ 4.3 ============
  rabbitmq:
    image: rabbitmq:4.3.4-management
    container_name: cict-rabbitmq
    restart: unless-stopped
    environment:
      RABBITMQ_DEFAULT_USER: ${RABBITMQ_DEFAULT_USER}
      RABBITMQ_DEFAULT_PASS: ${RABBITMQ_DEFAULT_PASS}
      RABBITMQ_ADMIN_PASSWORD: ${RABBITMQ_ADMIN_PASSWORD}
    volumes:
      - ./data/rabbitmq:/var/lib/rabbitmq
    ports:
      - "${ADMIN_BIND_ADDRESS}:${RABBITMQ_MGMT_PORT}:15672"
    healthcheck:
      test: ["CMD", "rabbitmq-diagnostics", "ping"]
      interval: 15s
      timeout: 10s
      retries: 10
      start_period: 30s
    networks:
      - cict-net

  # ============ RustFS ============
  rustfs:
    image: rustfs/rustfs:1.0.0-rc.2
    container_name: cict-rustfs
    restart: unless-stopped
    environment:
      RUSTFS_ACCESS_KEY: ${RUSTFS_ACCESS_KEY}
      RUSTFS_SECRET_KEY: ${RUSTFS_SECRET_KEY}
      RUSTFS_ADDRESS: ":9000"
      RUSTFS_CONSOLE_ADDRESS: ":9001"
      RUSTFS_CONSOLE_ENABLE: "true"
      RUSTFS_CORS_ALLOWED_ORIGINS: "*"
      RUSTFS_CONSOLE_CORS_ALLOWED_ORIGINS: "*"
      RUSTFS_OBS_LOGGER_LEVEL: error
      RUSTFS_OBS_LOG_DIRECTORY: /var/log/rustfs/
    volumes:
      - ./data/rustfs:/data
      - ./logs/rustfs:/var/log/rustfs
    ports:
      - "${ADMIN_BIND_ADDRESS}:${RUSTFS_PORT}:9000"
      - "${ADMIN_BIND_ADDRESS}:${RUSTFS_CONSOLE_PORT}:9001"
    networks:
      - cict-net

  # ============ CICT 后端 ============
  cict:
    build:
      context: ./cict
      dockerfile: Dockerfile
    image: cict/backend:${CICT_IMAGE_TAG}
    container_name: cict-app
    restart: unless-stopped
    environment:
      # 左侧是容器内变量名，右侧来自同目录的 .env
      JAVA_TOOL_OPTIONS: ${JAVA_TOOL_OPTIONS}
      MARIADB_USER: ${MARIADB_USER}
      MARIADB_PASSWORD: ${MARIADB_PASSWORD}
      REDIS_PASSWORD: ${REDIS_PASSWORD}
      RABBITMQ_DEFAULT_USER: ${RABBITMQ_DEFAULT_USER}
      RABBITMQ_DEFAULT_PASS: ${RABBITMQ_DEFAULT_PASS}
      RUSTFS_ACCESS_KEY: ${RUSTFS_ACCESS_KEY}
      RUSTFS_SECRET_KEY: ${RUSTFS_SECRET_KEY}
    volumes:
      - ./logs/cict:/data/logs
    depends_on:
      mariadb:
        condition: service_healthy
      redis:
        condition: service_healthy
      rabbitmq:
        condition: service_healthy
      rustfs:
        condition: service_started
    networks:
      - cict-net

  # ============ Nginx ============
  nginx:
    image: nginx:1.28.3
    container_name: cict-nginx
    restart: unless-stopped
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./nginx/conf.d:/etc/nginx/conf.d:ro
      - ./nginx/html:/usr/share/nginx/html:ro
      - ./logs/nginx:/var/log/nginx
    ports:
      - "${NGINX_PORT}:80"
    depends_on:
      - cict
    networks:
      - cict-net

networks:
  cict-net:
    driver: bridge
EOF
```

**关键设计说明**：

| 设计点 | 说明 |
|--------|------|
| `restart: unless-stopped` | 容器退出或 Docker 重启后会自动拉起，除非管理员主动停止；语义比 systemd 的 `Restart=on-failure` 更宽 |
| `depends_on` | 后端等待 MariaDB、Redis、RabbitMQ 健康检查通过；RustFS 镜像未假定内置 `curl`/`wget`，只等待容器启动，应用仍需具备连接重试能力 |
| 不发布数据服务端口 | MariaDB 3306、Redis 6379 和 RabbitMQ 5672 只在 `cict-net` 中开放，宿主机运维通过 `docker compose exec` 进入容器 |
| `healthcheck` | MariaDB 使用镜像自带 `healthcheck.sh`，避免在检查命令中暴露密码；Redis、RabbitMQ 使用各自原生命令 |
| `networks: cict-net` | 自定义桥接网络，服务间通过服务名互相访问 |
| `volumes` | 数据持久化到宿主机 `./data/` 目录，容器删除后数据不丢失 |

### 3.5 安装 Docker Engine

```bash
# 添加 Docker 官方 GPG 密钥
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# 添加 Docker apt 源
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 后续命令按非 root 用户执行；docker 组等同 root 权限，只应授予受信任的运维用户
sudo usermod -aG docker "$USER"

# 退出 SSH 会话并重新登录，使组权限生效，然后再执行以下验证

# 验证
docker --version
docker compose version
docker run --rm hello-world
```

### 3.6 配置 Docker 日志轮转

```bash
sudo tee /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "5"
  }
}
EOF

sudo systemctl daemon-reload
sudo systemctl restart docker
```

> 中国大陆环境如需镜像加速，只添加云厂商为当前账号或网络明确提供的最新地址，并先用 `docker pull hello-world` 验证。公共镜像地址经常失效，不应把未经验证的地址固化到生产文档。第 4 章的联网制包机器同样适用。

### 3.7 构建前检查

```bash
cd /data/app/cict-docker
test -f cict/app.jar
docker compose config --quiet

# 使用实际挂载的配置验证 Nginx 语法
docker compose run --rm --no-deps nginx nginx -t
```

### 3.8 构建并启动全部服务

```bash
cd /data/app/cict-docker

# 构建后端镜像（RustFS 使用官方镜像，无需构建）
docker compose build cict

# 拉取官方镜像 + 启动全部服务
docker compose up -d
```

### 3.9 验证服务状态

```bash
# 查看所有容器状态
docker compose ps

# MariaDB：密码从容器环境读取，不依赖宿主机 shell 是否 source .env
docker compose exec -T mariadb sh -c \
  'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" -e "SELECT VERSION();"'

# Redis
docker compose exec -T redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli ping'

# RabbitMQ：默认用户仅供应用使用，另建管理账号（重复执行安全）
docker compose exec -T rabbitmq sh -c '
  rabbitmqctl set_user_tags "$RABBITMQ_DEFAULT_USER" &&
  if ! rabbitmqctl list_users --silent | awk "{print \$1}" | grep -qx cict_admin; then
    rabbitmqctl add_user cict_admin "$RABBITMQ_ADMIN_PASSWORD"
  fi &&
  rabbitmqctl set_user_tags cict_admin administrator &&
  rabbitmqctl set_permissions -p / cict_admin ".*" ".*" ".*"
'

# 管理端口默认只绑定 127.0.0.1，可先建立 SSH 隧道：
# ssh -L 15672:127.0.0.1:15672 -L 9000:127.0.0.1:9000 -L 9001:127.0.0.1:9001 <用户>@<服务器IP>
# RabbitMQ 管理界面：http://127.0.0.1:15672（使用 cict_admin）
# RustFS S3 API：http://127.0.0.1:9000
# RustFS 控制台：http://127.0.0.1:9001
# Nginx：浏览器访问 http://<服务器IP>
```

### 3.10 在 RustFS 中创建存储桶

后端配置引用了 `pike` 存储桶：

1. 建立上述 SSH 隧道后访问 `http://127.0.0.1:9001`
2. 使用 `.env` 中配置的 `RUSTFS_ACCESS_KEY` / `RUSTFS_SECRET_KEY` 登录
3. 创建名为 `pike` 的存储桶

### 3.11 部署前后端代码（首次）

> 首次放置前后端产物与日常更新是**同一套操作**，完整的分步手册（含验证与回滚）统一收敛在**附录 A.3**，此处不重复展开：
>
> - 首次部署前端：按 **A.3.1** 执行一次（前端是挂载目录，无需构建镜像）
> - 首次部署后端：在线环境按 **A.3.2** 执行一次；离线环境按 **A.3.3** 或 **4.0** 一键安装包执行


---

## 第 4 章 主路线：离线打包与离线安装

> 在联网机器上拉取全部镜像并导出为 tar 文件，同时下载 Docker Engine 离线 deb 包，打包拷贝至离线服务器后加载使用。配置文件与第 3 章完全相同。

### 4.0 一键安装包（推荐入口）

以上流程已封装为仓库 `deploy/bundle/` 中的两个脚本，**日常发布直接使用**；4.1~4.6 的手工步骤保留作为透明说明与排障参考。

**联网机器制包**（要求与目标机同架构、同系统版本，已按 3.5 安装 Docker，另需 `unzip`、`dpkg-dev`）：

```bash
cd deploy/bundle
./build-bundle.sh \
  --app-jar /path/to/cict-dist-2.1.2.jar \
  --frontend /path/to/dist.zip \
  --tag 2.1.2
```

产物为 `dist/cict-offline-bundle-<tag>.tar.gz` 与同名 `.sha256`。`--tag` 每次发布必须唯一；目标机已装 Docker 时可加 `--skip-engine` 减小体积。密码不进安装包：`.env` 占位符由目标机安装时用 `openssl rand` 现场生成。

**离线目标机安装**：

```bash
sha256sum -c cict-offline-bundle-<tag>.tar.gz.sha256
tar -xzf cict-offline-bundle-<tag>.tar.gz
cd cict-offline-bundle
sudo ./install.sh
```

`install.sh` 幂等：重复执行保留已有 `.env` 密码与 `data/` 数据，仅同步项目文件并重新拉起服务，因此**离线升级同样是用新包重跑本命令**（等价于 A.3.3 的手工流程），回滚即用旧 tag 的包重跑。安装报告写入 `<部署目录>/install-report.txt`（不含密码）。

**图形界面（可选）**：安装包内已附带部署向导 `deploy-gui.py`（`build-bundle.sh` 自动打包）。目标机执行 `sudo python3 deploy-gui.py`，通过 SSH 隧道在浏览器中完成配置编辑、一键安装与逐项验证，见 `deploy/gui/README.md`。

### 4.1 联网机器准备

在一台与离线服务器**同架构、同系统版本**的联网 Ubuntu 24.04 机器上操作。

#### 4.1.1 安装 Docker

同 [3.5 安装 Docker Engine](#35-安装-docker-engine) 步骤。

#### 4.1.2 准备项目文件

仓库 `deploy/` 目录即完整项目文件，克隆到联网机器即可；后端 JAR 与前端产物在制包时通过 `--app-jar`、`--frontend` 参数注入。

#### 4.1.3 下载 Docker Engine 离线安装包

```bash
mkdir -p ~/docker-offline
cd ~/docker-offline
sudo apt install -y dpkg-dev

apt-get download $(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances \
  docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin | grep "^\w" | sort -u)

dpkg-scanpackages . /dev/null | gzip -9c > Packages.gz
sha256sum *.deb Packages.gz > docker-packages.sha256
ls -lh *.deb
```

### 4.2 拉取并导出全部镜像

```bash
mkdir -p ~/docker-images
cd ~/docker-images

# 拉取官方镜像
docker pull mariadb:11.4
docker pull redis:7.4.10
docker pull rabbitmq:4.3.4-management
docker pull nginx:1.28.3
docker pull eclipse-temurin:17-jre-jammy
docker pull rustfs/rustfs:1.0.0-rc.2

# 构建后端镜像
cd /data/app/cict-docker
docker compose build cict

# 导出全部镜像为 tar 文件
docker save -o ~/docker-images/mariadb.tar mariadb:11.4
docker save -o ~/docker-images/redis.tar redis:7.4.10
docker save -o ~/docker-images/rabbitmq.tar rabbitmq:4.3.4-management
docker save -o ~/docker-images/nginx.tar nginx:1.28.3
docker save -o ~/docker-images/eclipse-temurin.tar eclipse-temurin:17-jre-jammy
docker save -o ~/docker-images/rustfs.tar rustfs/rustfs:1.0.0-rc.2
CICT_IMAGE="$(docker compose config --images | awk '/^cict\/backend:/{print; exit}')"
docker save -o ~/docker-images/cict-backend.tar "$CICT_IMAGE"

# 生成校验文件
cd ~/docker-images
sha256sum *.tar > images.sha256
du -sh ~/docker-images/
```

### 4.3 打包并传输

```bash
cd ~
ONLINE_WORK_DIR="$(pwd)"
tar -czf cict-docker-offline-bundle.tar.gz \
  -C /data/app cict-docker/ \
  -C "$ONLINE_WORK_DIR" docker-images/ docker-offline/

scp cict-docker-offline-bundle.tar.gz root@<离线服务器IP>:/data/app/
```

### 4.4 离线服务器安装

> 以下命令在**离线服务器**上执行。

#### 4.4.1 解压

```bash
cd /data/app
tar -zxvf cict-docker-offline-bundle.tar.gz
```

#### 4.4.2 离线安装 Docker Engine

```bash
cd /data/app/docker-offline
sha256sum -c docker-packages.sha256
echo 'deb [trusted=yes] file:/data/app/docker-offline ./' \
  | sudo tee /etc/apt/sources.list.d/docker-offline.list
sudo apt update
sudo apt-get install -y --no-download docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin

sudo systemctl enable docker
sudo systemctl start docker
sudo usermod -aG docker "$USER"

# 退出 SSH 会话并重新登录，使 docker 组权限生效
docker --version
docker compose version
```

#### 4.4.3 加载离线镜像

```bash
cd /data/app/docker-images
sha256sum -c images.sha256

for image_tar in *.tar; do
  echo "Loading $image_tar..."
  docker load -i "$image_tar"
done

docker images
```

预期输出：

```
REPOSITORY                TAG                  SIZE
mariadb                   11.4                 （以实际为准）
redis                     7.4.10               （以实际为准）
rabbitmq                  4.3.4-management     （以实际为准）
nginx                     1.28.3               （以实际为准）
eclipse-temurin           17-jre-jammy         （以实际为准）
rustfs/rustfs             1.0.0-rc.2           （以实际为准）
cict/backend              2.1.2-SNAPSHOT       （以实际为准）
```

#### 4.4.4 启动全部服务

镜像已加载到本地，`docker compose up` 直接使用本地镜像：

```bash
cd /data/app/cict-docker
docker compose up -d
```

> 离线环境不需要执行 `docker compose build`，镜像已在 4.2 步骤中构建并导入。

#### 4.4.5 验证

同 [3.9 验证服务状态](#39-验证服务状态)。

### 4.5 离线更新后端

> 本节操作已并入**附录 A.3.3**（联网机器与离线服务器的完整分步、校验与回滚）。日常推荐直接使用 4.0 一键安装包：联网机器用新 JAR + 新 tag 重新制包，目标机重跑 `sudo ./install.sh` 即完成升级，回滚 = 用旧 tag 的包重跑。

### 4.6 离线镜像管理参考

#### 镜像导出/导入命令速查

| 操作 | 命令 |
|------|------|
| 导出单个镜像 | `docker save -o <name>.tar <image>:<tag>` |
| 导出多个镜像 | `docker save -o all.tar image1:tag image2:tag` |
| 导入镜像 | `docker load -i <name>.tar` |
| 查看镜像列表 | `docker images` |
| 删除镜像 | `docker rmi <image>:<tag>` |

#### 批量导出/导入脚本（已由 4.0 一键安装包吸收，保留作参考）

```bash
#!/bin/bash
# export-all-images.sh
IMAGES=(
  "mariadb:11.4" "redis:7.4.10" "rabbitmq:4.3.4-management"
  "nginx:1.28.3" "eclipse-temurin:17-jre-jammy" "rustfs/rustfs:1.0.0-rc.2"
  "cict/backend:2.1.2-SNAPSHOT"
)
OUTPUT_DIR="./docker-images"
mkdir -p "$OUTPUT_DIR"
for image in "${IMAGES[@]}"; do
  filename=$(echo "$image" | tr '/:' '__').tar
  echo "Exporting $image -> $OUTPUT_DIR/$filename"
  docker save -o "$OUTPUT_DIR/$filename" "$image"
done
cd "$OUTPUT_DIR"
sha256sum *.tar > images.sha256
echo "Done. $(ls *.tar | wc -l) images exported."
```

```bash
#!/bin/bash
# import-all-images.sh
IMAGE_DIR="./docker-images"
cd "$IMAGE_DIR"
sha256sum -c images.sha256 || exit 1
for tar_file in *.tar; do
  echo "Loading $tar_file..."
  docker load -i "$tar_file"
done
echo "Done. Loaded images:"
docker images
```

---

## 第 5 章 备选路线：Ubuntu 直装在线安装

> 在联网的 Ubuntu 24.04 LTS 环境下，通过 apt 源和源码编译直接安装各组件，使用 systemd 管理服务。

### 5.1 环境准备

```bash
# 更新系统包索引
sudo apt update && sudo apt upgrade -y

# 安装基础工具
sudo apt install -y wget curl unzip tar build-essential net-tools ca-certificates gnupg rsync

# 统一安装目录
sudo mkdir -p /data/app
sudo chown -R $(whoami):$(whoami) /data/app
```

生产服务器建议启用主机防火墙。确认 SSH 实际端口后再启用，默认只公开 Web 入口：

```bash
sudo apt install -y ufw
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
sudo ufw status verbose
```

> MariaDB、Redis、RabbitMQ、RustFS 和后端端口默认只监听回环地址。确需跨主机开放时，应使用明确的管理网 IP、来源白名单和 TLS，不要直接向公网开放。

### 5.2 安装 MariaDB 11.4

#### 5.2.1 添加 MariaDB 官方源

```bash
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://mariadb.org/mariadb_release_signing_key.asc \
  | sudo gpg --dearmor -o /etc/apt/keyrings/mariadb-keyring.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/mariadb-keyring.gpg] https://mirrors.aliyun.com/mariadb/repo/11.4/ubuntu noble main" \
  | sudo tee /etc/apt/sources.list.d/mariadb.list
```

> 如果阿里云镜像不可用，可替换为官方源 `https://deb.mariadb.org/11.4/ubuntu noble main`。

#### 5.2.2 安装

```bash
sudo apt update
sudo apt install -y mariadb-server mariadb-client
```

#### 5.2.3 初始化数据目录

CICT 需要兼容大小写不一致的历史 SQL，因此使用 `lower_case_table_names=1`。该参数必须在系统表首次初始化时确定，不能复制已按默认值 `0` 初始化的 `/var/lib/mysql`。

```bash
sudo systemctl stop mariadb
if [ -d /data/db/mysql/mysql ]; then
  echo "/data/db/mysql 已初始化；请先备份并移走旧目录" >&2
  exit 1
fi
sudo install -d -o mysql -g mysql -m 0750 /data/db/mysql
```

如果系统存在 MariaDB AppArmor 配置，再增加数据目录权限；不同来源的软件包可能使用 `usr.sbin.mariadbd` 或 `usr.sbin.mysqld`，应以本机实际文件名为准：

```bash
MARIADB_AA_PROFILE=/etc/apparmor.d/usr.sbin.mariadbd
if [ -f "$MARIADB_AA_PROFILE" ]; then
  sudo install -d /etc/apparmor.d/local
  sudo tee /etc/apparmor.d/local/usr.sbin.mariadbd <<'EOF'
/data/db/mysql/ r,
/data/db/mysql/** rwk,
EOF
  sudo apparmor_parser -r "$MARIADB_AA_PROFILE"
fi
```

在空目录上初始化系统表：

```bash
sudo mariadb-install-db \
  --user=mysql \
  --datadir=/data/db/mysql \
  --auth-root-authentication-method=socket \
  --skip-test-db \
  --lower-case-table-names=1
```

> 不得在已有业务数据的目录上执行 `mariadb-install-db`。已有 `lower_case_table_names=0` 的实例需要逻辑备份（dump）后在新目录恢复，不能原地切换。

#### 5.2.4 配置 MariaDB

创建配置文件 `/etc/mysql/mariadb.conf.d/server.cnf`：

```bash
sudo tee /etc/mysql/mariadb.conf.d/server.cnf <<'EOF'
[mysqld]

# ===== 数据目录 =====
datadir=/data/db/mysql
lower_case_table_names=1

# ===== 性能与安全 =====
performance_schema=1
bind-address=127.0.0.1
skip-name-resolve=ON
log_bin_trust_function_creators = 1
max_sp_recursion_depth = 10

# ===== 字符集（utf8 → utf8mb4，支持完整 Unicode） =====
character-set-server = utf8mb4
collation-server = utf8mb4_general_ci
init_connect='SET NAMES utf8mb4 COLLATE utf8mb4_general_ci'
skip-character-set-client-handshake

# ===== 事务隔离 =====
transaction_isolation = READ-COMMITTED

# ===== 连接与线程 =====
back_log = 1000
max_connections = 500
thread_cache_size = 100
thread_stack = 256K

# ===== 表缓存 =====
table_open_cache = 8192
table_definition_cache = 8192

# ===== 数据包与缓冲 =====
max_allowed_packet = 64M
binlog_cache_size = 1M
max_heap_table_size = 128M
tmp_table_size = 64M

# ===== 读写缓冲（per-connection） =====
read_buffer_size = 1M
read_rnd_buffer_size = 1M
join_buffer_size = 1M

# ===== InnoDB 优化（新增） =====
innodb_buffer_pool_size = 4G
innodb_log_file_size = 512M
innodb_flush_log_at_trx_commit = 1
innodb_io_capacity = 2000

# ===== 二进制日志 =====
log-bin=mysql-bin
binlog_format=row
binlog_expire_logs_seconds = 259200

# ===== 慢查询日志（排障时开启，并同步配置日志轮转） =====
slow_query_log = OFF
long_query_time = 2

# ===== 全文搜索 =====
innodb_ft_min_token_size = 4
ft_min_word_len = 4
EOF
```

> **配置修订说明**（相比原配置文件的变更）：
>
> | 配置项 | 原值 | 修订后 | 原因 |
> |--------|------|--------|------|
> | `character-set-server` | `utf8` | `utf8mb4` | `utf8` 即 `utf8mb3`，不支持 4 字节字符（emoji、生僻字），已废弃 |
> | `collation-server` | `utf8_general_ci` | `utf8mb4_general_ci` | 配合字符集升级 |
> | `lower_case_table_names` | 系统默认 `0` | `1` | 兼容历史 SQL 中的表名大小写差异；必须在数据目录初始化时确定 |
> | `init_connect` 中的字符集 | `utf8` / `utf8_general_ci` | 单条 `SET NAMES utf8mb4 COLLATE utf8mb4_general_ci` | 避免重复配置时后一个值覆盖前一个值 |
> | `transaction-isolation` | `transaction-isolation` | `transaction_isolation` | 连字符形式已废弃，改用下划线形式 |
> | `max_allowed_packet` | `16M` | `64M` | 现代应用经常需要传输较大数据包（如批量 SQL、大 BLOB） |
> | `read_rnd_buffer_size` | `16M` | `1M` | 每连接缓冲，过大的值会随并发连接数快速放大内存占用 |
> | `thread_stack` | `192K` | `256K` | 192K 在复杂查询中可能栈溢出，256K 为现代默认值 |
> | `query_cache_size` | `128M` | **删除** | MariaDB 仍兼容该功能，但高并发写入场景容易产生全局锁竞争，不建议作为通用生产默认值 |
> | `query_cache_limit` | `8M` | **删除** | 未启用查询缓存时无需配置 |
> | `thread_concurrency` | `5000` | **删除** | MySQL 5.7.1 移除，MariaDB 中为空操作（no-op） |
> | `innodb_buffer_pool_size` | 缺失 | `4G` | **新增**：InnoDB 最重要参数，默认仅 128M 严重不足 |
> | `innodb_log_file_size` | 缺失 | `512M` | **新增**：影响写入性能和崩溃恢复 |
> | `innodb_flush_method` | 缺失 | **不强制设置** | MariaDB 会根据版本和平台选择默认值，硬编码会降低跨环境兼容性 |
> | `innodb_io_capacity` | 缺失 | `2000` | **新增**：适配 SSD 磁盘 IOPS |
> | `innodb_buffer_pool_instances` | 缺失 | **不添加** | MariaDB 10.5.1 起已忽略该参数 |
> | `innodb_flush_log_at_trx_commit` | 缺失 | `1` | **新增**：保证事务持久性（默认值，显式声明） |
> | `skip-name-resolve` | 缺失 | `ON` | **新增**：跳过 DNS 反查，避免连接延迟 |
> | `innodb_ft_min_token_size` | 缺失 | `4` | **新增**：InnoDB 全文索引最小词长（原 `ft_min_word_len` 仅影响 MyISAM） |
>
> **保留不变的配置项**：`datadir`、`performance_schema`、`log_bin_trust_function_creators`、`max_sp_recursion_depth`、`back_log`、`table_open_cache`、`table_definition_cache`、`binlog_cache_size`、`max_heap_table_size`、`log-bin`、`long_query_time`、`ft_min_word_len`。二进制日志改用 `ROW` 格式，并用秒数参数设置 3 天过期时间。
>
> **参数调整建议**：`innodb_buffer_pool_size` 应设为物理内存的 50%-70%（示例 4G，请根据实际内存调整）。

#### 5.2.5 启动并创建业务库

```bash
sudo systemctl enable mariadb
sudo systemctl start mariadb
sudo mariadb-secure-installation
```

设置 root 密码和安全策略后，创建业务数据库和最小权限账号。请先把下方占位符替换为随机生成的强密码：

```bash
sudo mariadb <<'SQL'
CREATE DATABASE IF NOT EXISTS cict
  CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
CREATE USER IF NOT EXISTS 'cict'@'127.0.0.1'
  IDENTIFIED BY '<CICT_DATABASE_PASSWORD>';
GRANT ALL PRIVILEGES ON cict.* TO 'cict'@'127.0.0.1';
FLUSH PRIVILEGES;
SQL
```

> 不要在生产环境使用文档中的示例密码，也不要为应用配置 MariaDB root 账号。

#### 5.2.6 验证

```bash
mariadb -uroot -p
SHOW VARIABLES LIKE "version";
SHOW VARIABLES LIKE "datadir";
SHOW VARIABLES LIKE "character_set_server";
```

### 5.3 安装 Redis 7.4

#### 5.3.1 安装编译依赖

```bash
sudo apt install -y build-essential tcl libsystemd-dev pkg-config
```

#### 5.3.2 下载并编译

```bash
cd /data/app
wget https://download.redis.io/releases/redis-7.4.10.tar.gz
tar -zxvf redis-7.4.10.tar.gz
cd redis-7.4.10

make USE_SYSTEMD=yes
sudo make USE_SYSTEMD=yes install PREFIX=/data/app/redis
```

#### 5.3.3 配置 Redis

```bash
sudo useradd -r -s /sbin/nologin redis
sudo mkdir -p /etc/redis /data/app/redis/{data,logs}
sudo cp redis.conf /etc/redis/redis.conf
sudo chown -R redis:redis /data/app/redis
sudo chown redis:redis /etc/redis/redis.conf
```

修改配置文件 `/etc/redis/redis.conf`：

```ini
bind 127.0.0.1 ::1
protected-mode yes
daemonize no
supervised systemd
dir /data/app/redis/data
logfile "/data/app/redis/logs/redis.log"
pidfile /run/redis/redis-server.pid
```

> CICT 与 Redis 部署在同一主机，因此默认只监听回环地址。若确需跨主机访问，应同时配置 Redis ACL、TLS 和防火墙白名单，不能使用 `0.0.0.0` + `protected-mode no` 的无认证组合。

#### 5.3.4 配置 systemd 服务

创建 `/etc/systemd/system/redis.service`：

```ini
[Unit]
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
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable redis
```

#### 5.3.5 启动并验证

```bash
sudo systemctl start redis
/data/app/redis/bin/redis-cli ping
```

输出：

```
PONG
```

### 5.4 安装 RabbitMQ 4.3

> RabbitMQ 3.8 已停止支持，且不能与 Erlang 25/26 混用。本节改用当前受支持的 RabbitMQ 4.3 系列和 Erlang 27，并使用 Team RabbitMQ 为 Ubuntu 24.04 提供的软件源。以下仓库只提供 amd64 包；arm64 请按 RabbitMQ 官方文档改用其 Launchpad Erlang 仓库。

#### 5.4.1 添加 Team RabbitMQ 软件源

```bash
sudo apt-get install -y curl gnupg apt-transport-https

curl -1sLf "https://keys.openpgp.org/vks/v1/by-fingerprint/0A9AF2115F4687BD29803A206B73A36E6026DFCA" \
  | sudo gpg --dearmor \
  | sudo tee /usr/share/keyrings/com.rabbitmq.team.gpg >/dev/null

sudo tee /etc/apt/sources.list.d/rabbitmq.list <<'EOF'
deb [arch=amd64 signed-by=/usr/share/keyrings/com.rabbitmq.team.gpg] https://deb1.rabbitmq.com/rabbitmq-erlang/ubuntu/noble noble main
deb [arch=amd64 signed-by=/usr/share/keyrings/com.rabbitmq.team.gpg] https://deb2.rabbitmq.com/rabbitmq-erlang/ubuntu/noble noble main
deb [arch=amd64 signed-by=/usr/share/keyrings/com.rabbitmq.team.gpg] https://deb1.rabbitmq.com/rabbitmq-server/ubuntu/noble noble main
deb [arch=amd64 signed-by=/usr/share/keyrings/com.rabbitmq.team.gpg] https://deb2.rabbitmq.com/rabbitmq-server/ubuntu/noble noble main
EOF

# 锁定受支持的 Erlang 27 和 RabbitMQ 4.3 系列，避免 apt 自动跨大版本升级
sudo tee /etc/apt/preferences.d/rabbitmq <<'EOF'
Package: erlang*
Pin: version 1:27.*
Pin-Priority: 1000

Package: rabbitmq-server
Pin: version 4.3.*
Pin-Priority: 1000
EOF

sudo apt update
```

#### 5.4.2 安装 Erlang 与 RabbitMQ

```bash
sudo apt-get install -y erlang-base erlang-asn1 erlang-crypto erlang-eldap \
  erlang-ftp erlang-inets erlang-mnesia erlang-os-mon erlang-parsetools \
  erlang-public-key erlang-runtime-tools erlang-snmp erlang-ssl \
  erlang-syntax-tools erlang-tftp erlang-tools erlang-xmerl
sudo apt-get install -y rabbitmq-server --fix-missing

erl -noshell -eval 'io:format("~s~n", [erlang:system_info(otp_release)]), halt().'
sudo rabbitmqctl version
```

#### 5.4.3 启动并设置开机自启

```bash
sudo systemctl enable rabbitmq-server
sudo systemctl start rabbitmq-server
```

#### 5.4.4 启用 Web 管理界面并创建账号

先将下方两个密码占位符替换为不同的随机强密码，并保存到密码管理系统：

```bash
sudo rabbitmq-plugins enable rabbitmq_management

# 应用账号：仅用于 CICT 连接消息队列（重复执行不修改已有密码）
if ! sudo rabbitmqctl list_users --silent | awk '{print $1}' | grep -qx cict; then
  sudo rabbitmqctl add_user cict '<RABBITMQ_APP_PASSWORD>'
fi
sudo rabbitmqctl set_permissions -p / cict '.*' '.*' '.*'

# 运维账号：用于登录管理控制台（重复执行不修改已有密码）
if ! sudo rabbitmqctl list_users --silent | awk '{print $1}' | grep -qx cict_admin; then
  sudo rabbitmqctl add_user cict_admin '<RABBITMQ_ADMIN_PASSWORD>'
fi
sudo rabbitmqctl set_user_tags cict_admin administrator
sudo rabbitmqctl set_permissions -p / cict_admin '.*' '.*' '.*'

# 默认 guest 账号只允许本机登录，生产环境存在时删除
if sudo rabbitmqctl list_users --silent | awk '{print $1}' | grep -qx guest; then
  sudo rabbitmqctl delete_user guest
fi

# CICT 与 RabbitMQ 在同一主机，只监听回环地址
sudo tee /etc/rabbitmq/rabbitmq.conf <<'EOF'
listeners.tcp.default = 127.0.0.1:5672
management.tcp.ip = 127.0.0.1
management.tcp.port = 15672
EOF
sudo systemctl restart rabbitmq-server
```

先执行 `ssh -L 15672:127.0.0.1:15672 <用户>@<服务器IP>` 建立隧道，再访问 `http://127.0.0.1:15672`。

使用上面创建的 `cict_admin` 账号登录。管理端口应仅向运维网段开放。

### 5.5 安装 JDK 17

```bash
sudo apt install -y openjdk-17-jdk
```

编辑 `/etc/profile.d/java.sh`：

```bash
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-$(dpkg --print-architecture)
export PATH=$JAVA_HOME/bin:$PATH
```

```bash
source /etc/profile
java -version
```

### 5.6 安装 Nginx

#### 5.6.1 安装编译依赖

```bash
sudo apt install -y libpcre3 libpcre3-dev zlib1g zlib1g-dev gcc make libssl-dev
```

#### 5.6.2 下载并编译

```bash
cd /data/app
wget https://nginx.org/download/nginx-1.28.3.tar.gz
tar -zxvf nginx-1.28.3.tar.gz
cd nginx-1.28.3
./configure --prefix=/data/app/nginx
make && make install
```

#### 5.6.3 配置 systemd 服务

创建 `/etc/systemd/system/nginx.service`：

```ini
[Unit]
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
LimitNOFILE=65535
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable nginx
```

#### 5.6.4 启动与验证

```bash
sudo systemctl start nginx
sudo systemctl status nginx
```

访问：`http://<服务器IP>`

#### 5.6.5 配置优化（gzip 与独立配置文件）

主配置 `/data/app/nginx/conf/nginx.conf`：

```nginx
user www-data;
worker_processes auto;
error_log /data/app/nginx/logs/error.log warn;
pid /data/app/nginx/logs/nginx.pid;

events {
    worker_connections 10240;
}

http {
    include       mime.types;
    default_type  application/octet-stream;

    gzip on;
    gzip_comp_level 6;
    gzip_min_length 1k;
    gzip_proxied any;
    gzip_vary on;
    gzip_types text/plain text/css application/json application/javascript
               application/xml application/xml+rss image/svg+xml;

    include conf.d/*.conf;
}
```

```bash
sudo mkdir -p /data/app/nginx/conf/conf.d
```

CICT 站点配置 `/data/app/nginx/conf/conf.d/cict.conf`：

```nginx
server {
    listen 80 default_server;
    server_name _;
    root /data/app/nginx/html;
    index index.html;

    # 尾斜杠会将对外的 /api/ 前缀替换为后端的 /
    location /api/ {
        proxy_pass http://127.0.0.1:9090/;
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

    location ~* \.(ico|jpg|jpeg|png|gif|svg|css|js|woff|woff2|ttf|eot)$ {
        expires 30d;
        access_log off;
    }
}
```

> 不要把前端和 API 代理拆成两个都监听 80 的虚拟主机，否则使用服务器 IP 访问时会落到错误的默认站点。后端 Spring Boot 服务仅在 `127.0.0.1:9090` 监听，由 Nginx 对外提供统一入口。

> 以上是便于首次验证的 HTTP 配置。生产环境应配置受信任证书、HTTPS 跳转和合适的 TLS 策略，再开放 443 端口。

```bash
sudo rm -f /data/app/nginx/conf/conf.d/frontend.conf /data/app/nginx/conf/conf.d/backend.conf
sudo /data/app/nginx/sbin/nginx -t
sudo systemctl reload nginx
```

### 5.7 安装 RustFS

> **安全要求**：部署前生成独立的 `RUSTFS_ACCESS_KEY` 和 `RUSTFS_SECRET_KEY`；生产环境禁止使用公开默认值 `rustfsadmin`。

#### 5.7.1 下载并安装

```bash
cd /data/app

case "$(uname -m)" in
  x86_64)  RUSTFS_ARCHIVE=rustfs-linux-x86_64-musl-v1.0.0-rc.2.zip ;;
  aarch64) RUSTFS_ARCHIVE=rustfs-linux-aarch64-musl-v1.0.0-rc.2.zip ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

wget "https://github.com/rustfs/rustfs/releases/download/1.0.0-rc.2/${RUSTFS_ARCHIVE}"
wget https://github.com/rustfs/rustfs/releases/download/1.0.0-rc.2/SHA256SUMS
grep "${RUSTFS_ARCHIVE}$" SHA256SUMS | sha256sum -c -
unzip "$RUSTFS_ARCHIVE"
chmod +x rustfs
sudo mv rustfs /usr/local/bin/
```

#### 5.7.2 配置环境变量

创建配置文件 `/etc/default/rustfs`：

```bash
sudo useradd --system --home /nonexistent --shell /usr/sbin/nologin rustfs 2>/dev/null || true
sudo install -m 0640 -o root -g rustfs /dev/null /etc/default/rustfs
sudo tee /etc/default/rustfs <<'EOF'
RUSTFS_ACCESS_KEY=<RUSTFS_ACCESS_KEY>
RUSTFS_SECRET_KEY=<RUSTFS_SECRET_KEY>
RUSTFS_VOLUMES="/data/rustfs0"
RUSTFS_ADDRESS="127.0.0.1:9000"
RUSTFS_CONSOLE_ADDRESS="127.0.0.1:9001"
RUSTFS_CONSOLE_ENABLE=true
RUSTFS_CORS_ALLOWED_ORIGINS="*"
RUSTFS_CONSOLE_CORS_ALLOWED_ORIGINS="*"
RUST_LOG=error
RUSTFS_OBS_LOG_DIRECTORY="/var/log/rustfs/"
EOF
sudo chown root:rustfs /etc/default/rustfs
sudo chmod 640 /etc/default/rustfs
```

> 将两个占位符替换为分别至少 16 和 32 字节的随机值，并保存到密码管理系统。不要使用 `rustfsadmin` 等公开默认值。

> `RUSTFS_CORS_ALLOWED_ORIGINS=*` 使浏览器可直接使用后端生成的预签名上传/下载 URL；`RUSTFS_CONSOLE_CORS_ALLOWED_ORIGINS` 只影响管理控制台。公网生产环境建议将 `*` 改为逗号分隔的实际前端 Origin，例如 `https://cict.example.com`。

#### 5.7.3 创建存储目录

```bash
sudo mkdir -p /data/rustfs0 /var/log/rustfs
sudo chown -R rustfs:rustfs /data/rustfs0 /var/log/rustfs
sudo chmod -R 750 /data/rustfs0 /var/log/rustfs
```

#### 5.7.4 配置 systemd 服务

创建 `/etc/systemd/system/rustfs.service`：

```bash
sudo tee /etc/systemd/system/rustfs.service <<'EOF'
[Unit]
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
EOF
```

> **注意**：使用 `<<'EOF'`（单引号）防止 shell 提前展开 `${RUSTFS_VOLUMES}`。

```bash
sudo systemctl daemon-reload
```

**故障排查**——如果服务启动失败：

1. 确认配置文件：`cat /etc/default/rustfs`
2. 检查目录权限：`ls -ld /data/rustfs0 /var/log/rustfs`
3. 手动测试：`source /etc/default/rustfs && /usr/local/bin/rustfs ${RUSTFS_VOLUMES}`
4. 查看日志：`journalctl -u rustfs -n 50 --no-pager` 和 `tail -n 50 /var/log/rustfs/rustfs.log`

> RustFS 会根据 `RUSTFS_OBS_LOG_DIRECTORY` 自行写入滚动日志。systemd 的 stdout/stderr 必须保持为 journald，不能再指向同一个 `rustfs.log`，否则 RustFS 会因日志输出冲突拒绝启动。

#### 5.7.5 启动并验证

```bash
sudo systemctl enable --now rustfs
sudo systemctl status rustfs
ss -lntp | grep -E ':9000|:9001'
```

S3 API 供本机后端通过 `http://127.0.0.1:9000` 使用。管理时先执行 `ssh -L 9001:127.0.0.1:9001 <用户>@<服务器IP>`，再访问 `http://127.0.0.1:9001`，使用 `/etc/default/rustfs` 中配置的访问密钥登录。

> 后端服务配置中引用了 RustFS 的 `pike` 存储桶，请在 RustFS 控制台中创建该存储桶。

### 5.8 后端运行环境配置

后端无需 Tomcat，直接通过 systemd 运行 Spring Boot JAR 包。

#### 5.8.1 创建目录结构

```bash
sudo useradd --system --home /data/app/cict --shell /usr/sbin/nologin cict 2>/dev/null || true
sudo mkdir -p /etc/cict /data/app/cict/{releases,logs}
sudo chown -R cict:cict /data/app/cict
sudo chown root:cict /etc/cict
sudo chmod 750 /etc/cict
```

| 目录 | 用途 |
|------|------|
| `/etc/cict/` | 存放 `cict.conf` 和 `application.properties` |
| `/data/app/cict/releases/` | 存放带版本号的 JAR，便于审计和回滚 |
| `/data/app/cict/logs/` | 存放应用日志 |
| `/data/app/cict/current.jar` | 由运维明确切换、指向当前版本的符号链接 |

#### 5.8.2 创建 JVM 参数配置

创建 `/etc/cict/cict.conf`：

```bash
sudo tee /etc/cict/cict.conf <<'EOF'
JAVA_OPTS="-Xms1g -Xmx4g"
CICT_DATABASE_PASSWORD=<CICT_DATABASE_PASSWORD>
RABBITMQ_APP_PASSWORD=<RABBITMQ_APP_PASSWORD>
RUSTFS_ACCESS_KEY=<RUSTFS_ACCESS_KEY>
RUSTFS_SECRET_KEY=<RUSTFS_SECRET_KEY>
EOF
sudo chown root:cict /etc/cict/cict.conf
sudo chmod 640 /etc/cict/cict.conf
```

> 四个密码/密钥占位符必须分别与 MariaDB 业务账号、RabbitMQ 应用账号和 `/etc/default/rustfs` 中的实际值一致。

| 参数 | 说明 |
|------|------|
| `-Xms1g` | JVM 初始堆内存 1GB（根据实际负载调整） |
| `-Xmx4g` | JVM 最大堆内存 4GB（必须结合 MariaDB 和宿主机总内存调整） |

> 配置文件包含密码，必须保持 `640 root:cict`。生产模板不得默认启用 JDWP；临时调试时也只能绑定回环地址并通过 SSH 隧道访问。

#### 5.8.3 创建 Spring Boot 配置文件

创建 `/etc/cict/application.properties`：

```bash
sudo tee /etc/cict/application.properties <<'EOF'
# ===== API 白名单 =====
pike.api.white-list[0]=/api/generator/**
pike.api.white-list[1]=/ssoLogin/sso/social-callback/**
pike.api.white-list[2]=/ssoLogin/auth-binding/**
pike.api.white-list[3]=/py
pike.api.white-list[4]=/api/v1/**
pike.api.white-list[5]=/webhook/trigger/**
pike.api.white-list[6]=/brand-config/public
pike.api.white-list[7]=/brand-config/asset/**
pike.api.white-list[8]=/auth/login

# ===== AutoCrud 扫描包配置 =====
autocrud.base-packages=com.polelink

# ===== 数据库配置 =====
spring.datasource.url=jdbc:mariadb://127.0.0.1:3306/cict
spring.datasource.username=cict
spring.datasource.password=${CICT_DATABASE_PASSWORD}

# ===== Quartz 配置 =====
# 业务 SQL 以小写 qrtz_ 创建表，Linux MariaDB 默认区分表名大小写
spring.quartz.properties.org.quartz.jobStore.tablePrefix=qrtz_

# ===== Redis 配置 =====
# Spring Boot 2.x 使用 spring.redis.*；若项目已升级到 3.x，请改为 spring.data.redis.*
spring.redis.host=127.0.0.1
spring.redis.port=6379

# ===== RabbitMQ 配置 =====
spring.rabbitmq.host=127.0.0.1
spring.rabbitmq.port=5672
spring.rabbitmq.username=cict
spring.rabbitmq.password=${RABBITMQ_APP_PASSWORD}

# ===== RustFS / S3 配置 =====
pike.file.s3.endpoint=http://127.0.0.1:9000
pike.file.s3.bucket=pike
pike.file.s3.accessKey=<your-access-key>
pike.file.s3.secretKey=<your-secret-key>

# ===== 日志配置 =====
logging.file.path=/data/app/cict/logs
logging.file.name=/data/app/cict/logs/cict.log

# ===== 服务端口 =====
server.address=127.0.0.1
server.port=9090
EOF
sudo chown root:cict /etc/cict/application.properties
sudo chmod 640 /etc/cict/application.properties
```

> **配置项说明**：
> - `spring.datasource.url`：修改为实际的数据库地址、端口和数据库名
> - `spring.datasource.username/password`：使用 1.2.5 创建的业务账号，密码来自受保护的 `cict.conf`
> - `spring.quartz.properties.org.quartz.jobStore.tablePrefix`：必须与实际 Quartz 表前缀的大小写一致；本指南的业务 SQL 使用 `qrtz_`
> - Redis 与 RabbitMQ：默认只连接本机；密码和账号必须与前述服务配置一致
> - `pike.file.s3.endpoint`：修改为 RustFS 服务器地址
> - `pike.file.s3.accessKey` / `secretKey`：替换为 RustFS 中创建的访问密钥
> - `server.port=9090`：后端服务监听端口，Nginx 代理需指向此端口

> Spring Boot 对多个配置源中的 List 通常采用整体替换，不是逐项合并。外部配置了 `pike.api.white-list[n]` 后，应把所有必需的公开端点列全；Nginx 会去掉对外的 `/api` 前缀，因此登录只放行后端实际路径 `/auth/login`，不要放宽为 `/auth/**`。

#### 5.8.4 创建 systemd 服务

创建 `/etc/systemd/system/cict.service`：

```bash
sudo tee /etc/systemd/system/cict.service <<'EOF'
[Unit]
Description=CICT Java Application
Wants=network-online.target mariadb.service redis.service rabbitmq-server.service rustfs.service
After=network-online.target mariadb.service redis.service rabbitmq-server.service rustfs.service
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
Type=simple
User=cict
Group=cict
EnvironmentFile=/etc/cict/cict.conf
WorkingDirectory=/data/app/cict

ExecStart=java $JAVA_OPTS -jar /data/app/cict/current.jar --spring.config.additional-location=/etc/cict/application.properties

StandardOutput=journal
StandardError=journal
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
```

| 配置项 | 作用 |
|--------|------|
| `current.jar` | 运维明确指定当前版本，避免按修改时间误选 JAR；回滚时切换符号链接即可 |
| `ExecStart` | 使用 `$JAVA_OPTS`（来自 `cict.conf`）运行 `current.jar`，并加载外部配置文件 |
| `Restart=on-failure` | 仅在 Java 进程以非 0 状态退出时自动重启；普通日志异常不会触发 |
| `StartLimitIntervalSec=300` / `StartLimitBurst=3` | 5 分钟内最多尝试启动 3 次，防止配置错误导致长期重启循环（参数位于 `[Unit]`） |

```bash
sudo systemctl daemon-reload
sudo systemctl enable cict
```

> 达到启动限制后，systemd 会将服务保持为 `failed`。修复配置后执行 `sudo systemctl reset-failed cict && sudo systemctl start cict`，不要在未处理根因时反复手工启动。

### 5.9 部署前后端代码

#### 前端

在构建机上传压缩包：

```bash
scp dist.zip <用户>@<服务器IP>:/tmp/cict-dist.zip
```

登录服务器后解压到临时目录并验证 `index.html`，再替换站点文件：

```bash
FRONTEND_TMP="$(mktemp -d)"
unzip -q /tmp/cict-dist.zip -d "$FRONTEND_TMP"

if [ -f "$FRONTEND_TMP/index.html" ]; then
  FRONTEND_SOURCE="$FRONTEND_TMP"
elif [ -f "$FRONTEND_TMP/dist/index.html" ]; then
  FRONTEND_SOURCE="$FRONTEND_TMP/dist"
else
  echo "index.html not found in dist.zip" >&2
  exit 1
fi

sudo rsync -a --delete --chown=root:www-data --chmod=D755,F644 \
  "$FRONTEND_SOURCE/" /data/app/nginx/html/
sudo -u www-data test -r /data/app/nginx/html/index.html
sudo systemctl reload nginx
```

访问：`http://<服务器IP>`

#### 后端

在构建机上传 JAR：

```bash
scp cict-dist-2.1.2-SNAPSHOT.jar <用户>@<服务器IP>:/tmp/cict-app.jar
```

登录服务器后安装并重启：

```bash
sudo install -o cict -g cict -m 0644 /tmp/cict-app.jar /data/app/cict/releases/cict-dist-2.1.2-SNAPSHOT.jar
sudo ln -sfn releases/cict-dist-2.1.2-SNAPSHOT.jar /data/app/cict/current.jar
sudo systemctl restart cict
sudo systemctl status cict
journalctl -u cict -n 50 --no-pager
tail -f /data/app/cict/logs/cict.log
ss -lntp | grep 9090
```

通过 Nginx 访问：`http://<服务器IP>`；9090 只监听本机，不直接对外开放。

---

## 第 6 章 备选路线：Ubuntu 直装离线安装

> 在无网络的 Ubuntu 24.04 环境下，先在联网的同版本机器上下载全部 deb 包和源码，打包后拷贝至离线服务器安装。安装步骤和配置与第 5 章相同，区别在于获取软件包的方式。

### 6.1 联网机器准备

在一台与离线服务器**同架构、同系统版本**的 Ubuntu 24.04 联网机器上操作：

```bash
sudo apt update && sudo apt install -y dpkg-dev apt-rdepends wget curl unzip tar gnupg

mkdir -p ~/offline_packages/{mariadb,redis,redis-deps,rabbitmq,jdk,nginx,nginx-deps,rustfs,base-tools,deb-repo}
```

### 6.2 离线包下载清单

> 以下所有命令在**联网机器**上执行。

#### 6.2.1 基础工具包

```bash
cd ~/offline_packages/base-tools

apt-get download $(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances \
  build-essential gcc make pkg-config net-tools unzip curl wget tar libssl-dev ca-certificates gnupg rsync ufw | grep "^\w" | sort -u)
```

校验：`dpkg --info *.deb | grep Depends`

#### 6.2.2 MariaDB 11.4

```bash
cd ~/offline_packages/mariadb

sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://mariadb.org/mariadb_release_signing_key.asc \
  | sudo gpg --dearmor -o /etc/apt/keyrings/mariadb-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/mariadb-keyring.gpg] https://mirrors.aliyun.com/mariadb/repo/11.4/ubuntu noble main" \
  | sudo tee /etc/apt/sources.list.d/mariadb.list
sudo apt update

apt-get download $(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances \
  mariadb-server mariadb-client | grep "^\w" | sort -u)
```

#### 6.2.3 Redis 源码与编译依赖

```bash
# 下载 Redis 源码
cd ~/offline_packages/redis
wget https://download.redis.io/releases/redis-7.4.10.tar.gz

# 下载编译依赖
cd ~/offline_packages/redis-deps
apt-get download $(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances \
  libsystemd-dev tcl pkg-config | grep "^\w" | sort -u)
```

#### 6.2.4 RabbitMQ 与 Erlang

> 下列 Team RabbitMQ 仓库仅提供 amd64 包。arm64 制包机必须按官方文档改用支持 Erlang 27 的 Launchpad 仓库，且制包机与离线服务器架构必须一致。

```bash
cd ~/offline_packages/rabbitmq

curl -1sLf "https://keys.openpgp.org/vks/v1/by-fingerprint/0A9AF2115F4687BD29803A206B73A36E6026DFCA" \
  | sudo gpg --dearmor \
  | sudo tee /usr/share/keyrings/com.rabbitmq.team.gpg >/dev/null

sudo tee /etc/apt/sources.list.d/rabbitmq.list <<'EOF'
deb [arch=amd64 signed-by=/usr/share/keyrings/com.rabbitmq.team.gpg] https://deb1.rabbitmq.com/rabbitmq-erlang/ubuntu/noble noble main
deb [arch=amd64 signed-by=/usr/share/keyrings/com.rabbitmq.team.gpg] https://deb2.rabbitmq.com/rabbitmq-erlang/ubuntu/noble noble main
deb [arch=amd64 signed-by=/usr/share/keyrings/com.rabbitmq.team.gpg] https://deb1.rabbitmq.com/rabbitmq-server/ubuntu/noble noble main
deb [arch=amd64 signed-by=/usr/share/keyrings/com.rabbitmq.team.gpg] https://deb2.rabbitmq.com/rabbitmq-server/ubuntu/noble noble main
EOF

sudo tee /etc/apt/preferences.d/rabbitmq <<'EOF'
Package: erlang*
Pin: version 1:27.*
Pin-Priority: 1000

Package: rabbitmq-server
Pin: version 4.3.*
Pin-Priority: 1000
EOF

sudo apt update

apt-get download $(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances \
  erlang-base erlang-asn1 erlang-crypto erlang-eldap erlang-ftp erlang-inets erlang-mnesia \
  erlang-os-mon erlang-parsetools erlang-public-key erlang-runtime-tools erlang-snmp erlang-ssl \
  erlang-syntax-tools erlang-tftp erlang-tools erlang-xmerl rabbitmq-server | grep "^\w" | sort -u)
```

#### 6.2.5 Nginx 源码与编译依赖

```bash
cd ~/offline_packages/nginx
wget https://nginx.org/download/nginx-1.28.3.tar.gz

cd ~/offline_packages/nginx-deps
apt-get download $(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances \
  libpcre3 libpcre3-dev zlib1g zlib1g-dev libssl-dev gcc make | grep "^\w" | sort -u)
```

#### 6.2.6 JDK 17（deb 包）

```bash
cd ~/offline_packages/jdk
apt-get download $(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances \
  openjdk-17-jdk-headless | grep "^\w" | sort -u)
```

#### 6.2.7 RustFS（MUSL 二进制包）

```bash
cd ~/offline_packages/rustfs

case "$(uname -m)" in
  x86_64)  RUSTFS_ARCHIVE=rustfs-linux-x86_64-musl-v1.0.0-rc.2.zip ;;
  aarch64) RUSTFS_ARCHIVE=rustfs-linux-aarch64-musl-v1.0.0-rc.2.zip ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

wget "https://github.com/rustfs/rustfs/releases/download/1.0.0-rc.2/${RUSTFS_ARCHIVE}"
wget https://github.com/rustfs/rustfs/releases/download/1.0.0-rc.2/SHA256SUMS
grep "${RUSTFS_ARCHIVE}$" SHA256SUMS | sha256sum -c -
```

#### 6.2.8 生成本地 apt 仓库和校验清单

```bash
cd ~/offline_packages
find base-tools mariadb redis-deps rabbitmq jdk nginx-deps -name '*.deb' -exec cp -n {} deb-repo/ \;
cd deb-repo
dpkg-scanpackages . /dev/null | gzip -9c > Packages.gz

cd ~/offline_packages
find . -type f ! -name bundle.sha256 -print0 | sort -z | xargs -0 sha256sum > bundle.sha256
```

### 6.3 打包并传输

```bash
cd ~
tar -czf offline_packages.tar.gz offline_packages/
scp offline_packages.tar.gz root@<离线服务器IP>:/data/app/
```

### 6.4 离线服务器安装

> 以下命令在**离线服务器**上执行。

```bash
cd /data/app
tar -zxvf offline_packages.tar.gz
cd /data/app/offline_packages
sha256sum -c bundle.sha256
```

#### 6.4.1 配置本地 apt 仓库并安装基础工具

```bash
echo 'deb [trusted=yes] file:/data/app/offline_packages/deb-repo ./' \
  | sudo tee /etc/apt/sources.list.d/cict-offline.list
sudo apt update
sudo apt-get install -y --no-download build-essential gcc make pkg-config net-tools \
  unzip curl wget tar libssl-dev ca-certificates gnupg rsync ufw
```

#### 6.4.2 安装 MariaDB

```bash
sudo apt-get install -y --no-download mariadb-server mariadb-client
```

按 [1.2.3 初始化数据目录](#123-初始化数据目录) 在空的 `/data/db/mysql` 上以 `lower_case_table_names=1` 初始化；不得复制已按默认值 `0` 初始化的 `/var/lib/mysql`。

创建配置文件 `/etc/mysql/mariadb.conf.d/server.cnf`，内容与 [5.2.4 配置 MariaDB](#524-配置-mariadb) 完全相同。

```bash
sudo systemctl enable mariadb
sudo systemctl start mariadb
sudo mariadb-secure-installation
```

随后按 [1.2.5 启动并创建业务库](#125-启动并创建业务库) 创建 `cict` 数据库和业务账号。

#### 6.4.3 安装 Redis

```bash
# 安装编译依赖
sudo apt-get install -y --no-download libsystemd-dev tcl pkg-config

# 解压并编译
cd /data/app
tar -zxvf /data/app/offline_packages/redis/redis-7.4.10.tar.gz
cd redis-7.4.10
make USE_SYSTEMD=yes
sudo make USE_SYSTEMD=yes install PREFIX=/data/app/redis
```

后续 Redis 配置、systemd 服务创建与 [1.3.3 - 1.3.5](#133-配置-redis) 完全相同。

#### 6.4.4 安装 RabbitMQ

```bash
sudo apt-get install -y --no-download erlang-base erlang-asn1 erlang-crypto erlang-eldap \
  erlang-ftp erlang-inets erlang-mnesia erlang-os-mon erlang-parsetools \
  erlang-public-key erlang-runtime-tools erlang-snmp erlang-ssl \
  erlang-syntax-tools erlang-tftp erlang-tools erlang-xmerl rabbitmq-server

sudo systemctl enable rabbitmq-server
sudo systemctl start rabbitmq-server
sudo rabbitmq-plugins enable rabbitmq_management
```

随后按 [1.4.4 启用 Web 管理界面并创建账号](#144-启用-web-管理界面并创建账号) 创建应用账号和管理账号。

#### 6.4.5 安装 JDK 17

```bash
sudo apt-get install -y --no-download openjdk-17-jdk-headless
java -version
```

#### 6.4.6 安装 Nginx

```bash
sudo apt-get install -y --no-download libpcre3 libpcre3-dev zlib1g zlib1g-dev libssl-dev gcc make

cd /data/app
tar -zxvf /data/app/offline_packages/nginx/nginx-1.28.3.tar.gz
cd nginx-1.28.3
./configure --prefix=/data/app/nginx
make && make install
```

后续 Nginx systemd 服务创建与配置与 [1.6.3 - 1.6.5](#163-配置-systemd-服务) 完全相同。

#### 6.4.7 安装 RustFS

```bash
cd /data/app/offline_packages/rustfs
case "$(uname -m)" in
  x86_64)  RUSTFS_ARCHIVE=rustfs-linux-x86_64-musl-v1.0.0-rc.2.zip ;;
  aarch64) RUSTFS_ARCHIVE=rustfs-linux-aarch64-musl-v1.0.0-rc.2.zip ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac
grep "${RUSTFS_ARCHIVE}$" SHA256SUMS | sha256sum -c -
unzip "$RUSTFS_ARCHIVE"
chmod +x rustfs
sudo mv rustfs /usr/local/bin/
```

后续 RustFS 配置与 systemd 服务创建与 [1.7.2 - 1.7.5](#172-配置环境变量) 完全相同。

#### 6.4.8 后端运行环境配置

与 [1.8 后端运行环境配置](#18-后端运行环境配置) 完全相同。

#### 6.4.9 部署前后端代码

与 [5.9 部署前后端代码](#59-部署前后端代码) 完全相同。


---

## 附录 A：更新流程与运维速查

### A.1 直接安装（第 5-6 章）更新流程

| 更新内容 | 操作步骤 |
|---------|---------|
| 前端代码 | 按 [5.9 部署前后端代码](#59-部署前后端代码) 先解压到临时目录、验证 `index.html`，再用 `rsync --delete` 替换并 reload |
| 后端代码 | 1. 上传新 JAR 到 `/data/app/cict/releases/`<br>2. 将 `current.jar` 原子切换到新版本<br>3. `sudo systemctl restart cict`；回滚时把符号链接切回旧版本 |

### A.2 直接安装服务管理速查表

| 组件 | 启动 | 停止 | 日志位置 |
|------|------|------|---------|
| MariaDB | `sudo systemctl start mariadb` | `sudo systemctl stop mariadb` | `/var/log/mysql/mariadb.log` |
| Redis | `sudo systemctl start redis` | `sudo systemctl stop redis` | `/data/app/redis/logs/redis.log` |
| RabbitMQ | `sudo systemctl start rabbitmq-server` | `sudo systemctl stop rabbitmq-server` | `/var/log/rabbitmq/` |
| CICT 后端 | `sudo systemctl start cict` | `sudo systemctl stop cict` | `/data/app/cict/logs/cict.log` |
| Nginx | `sudo systemctl start nginx` | `sudo systemctl stop nginx` | `/data/app/nginx/logs/error.log` |
| RustFS | `sudo systemctl start rustfs` | `sudo systemctl stop rustfs` | `/var/log/rustfs/rustfs.log` |

### A.3 Docker 部署更新手册（第 3-4 章）

> 本手册是 Docker 主路线**唯一**的更新操作入口：3.11 的首次代码部署、4.5 的离线后端更新都指向这里。除特别说明外，所有命令在部署目录 `/data/app/cict-docker` 下执行。
>
> 三条原则：**每次发布使用唯一镜像 tag**（禁止复用旧 tag 或 latest）；**有状态服务更新前先备份**（方法见 A.7）；**回滚靠本地保留的旧 tag 镜像**，历史镜像就是回滚点。

#### A.3.0 更新前检查（所有场景通用）

```bash
cd /data/app/cict-docker

# 1. 记录当前版本号（回滚时要用）
grep '^CICT_IMAGE_TAG=' .env
docker compose ps

# 2. 后端更新若包含数据库结构变更，必须先按 A.7 完成备份
# 3. 后端/组件更新会有约 1 分钟服务重启，提前通知使用方
```

#### A.3.1 前端更新（无需构建镜像，最常用）

前端文件是挂载进 Nginx 容器的目录，替换文件即生效，**不涉及任何镜像构建和容器重启**。

```bash
# === 本地机器：上传前端包 ===
scp dist.zip <用户>@<服务器IP>:/tmp/

# === 服务器：替换文件 ===
FRONTEND_TMP="$(mktemp -d)"
unzip -q /tmp/dist.zip -d "$FRONTEND_TMP"
if [ -f "$FRONTEND_TMP/index.html" ]; then
  FRONTEND_SOURCE="$FRONTEND_TMP"
elif [ -f "$FRONTEND_TMP/dist/index.html" ]; then
  FRONTEND_SOURCE="$FRONTEND_TMP/dist"
else
  echo "index.html not found in dist.zip" >&2
  exit 1
fi
rsync -a --delete --chmod=D755,F644 "$FRONTEND_SOURCE/" /data/app/cict-docker/nginx/html/
rm -rf "$FRONTEND_TMP" /tmp/dist.zip
```

说明：`--delete` 会清理旧构建的 hash 资源文件，目录不会越积越大；`nginx -s reload` 只在修改 Nginx **配置**时才需要，纯文件替换不必执行。

**验证**：浏览器访问 `http://<服务器IP>` 并强制刷新（Ctrl+F5）；或 `curl -sI http://127.0.0.1/ | head -1` 返回 `200`。

**回滚**：前端没有镜像版本，回滚 = 用旧版 `dist.zip` 重跑本节命令（旧包要归档保留）。

#### A.3.2 后端更新（在线环境）

```bash
# === 本地机器：上传新 JAR ===
scp cict-dist-2.1.3.jar <用户>@<服务器IP>:/tmp/

# === 服务器 ===
cd /data/app/cict-docker
NEW_TAG=2.1.3                      # 本次发布的唯一版本号

# 1. 替换 JAR（部署目录内固定文件名 app.jar）
install -m 0644 /tmp/cict-dist-2.1.3.jar cict/app.jar

# 2. 把 .env 中的镜像版本号改为 NEW_TAG 并确认
sed -i "s/^CICT_IMAGE_TAG=.*/CICT_IMAGE_TAG=$NEW_TAG/" .env
grep '^CICT_IMAGE_TAG=' .env

# 3. 构建镜像（只重打 JAR 一层，秒级）并滚动更新
docker compose build cict
docker compose up -d cict

# 4. 跟踪启动日志，看到应用启动完成（Started 字样）即成功，Ctrl+C 退出跟踪
docker compose logs -f cict
```

**验证**：

```bash
docker compose ps cict                                            # 状态为 running
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/api/auth/login
# 返回 405/400/200 任一都说明后端已响应；502/504 才表示未就绪
docker compose logs --since 10m cict | grep -iE 'error|exception' || echo 无错误日志
```

**回滚**：旧版本镜像仍保留在本地，**不需要旧 JAR 文件**（旧 JAR 已打包在旧镜像内）：

```bash
cd /data/app/cict-docker
sed -i "s/^CICT_IMAGE_TAG=.*/CICT_IMAGE_TAG=2.1.2/" .env     # 改回旧版本号
docker compose up -d cict
docker images | grep cict/backend                              # 查看本地保留的历史版本
```

#### A.3.3 后端更新（离线环境）

**方式一（推荐）**：使用 4.0 一键安装包——联网机器用新 JAR + 新 tag 重新制包，目标机重跑 `sudo ./install.sh`。

**方式二（只传后端镜像）**：

```bash
# === 联网机器 ===
cd /data/app/cict-docker
NEW_TAG=2.1.3
install -m 0644 /tmp/cict-dist-2.1.3.jar cict/app.jar
sed -i "s/^CICT_IMAGE_TAG=.*/CICT_IMAGE_TAG=$NEW_TAG/" .env
docker compose build cict
docker save -o cict-backend-$NEW_TAG.tar cict/backend:$NEW_TAG
sha256sum cict-backend-$NEW_TAG.tar > cict-backend-$NEW_TAG.tar.sha256
scp cict-backend-$NEW_TAG.tar cict-backend-$NEW_TAG.tar.sha256 <用户>@<离线服务器IP>:/tmp/
```

```bash
# === 离线服务器 ===
cd /data/app/cict-docker
sha256sum -c /tmp/cict-backend-2.1.3.tar.sha256    # 校验失败立即停止，重新传输
docker load -i /tmp/cict-backend-2.1.3.tar
sed -i "s/^CICT_IMAGE_TAG=.*/CICT_IMAGE_TAG=2.1.3/" .env
docker compose up -d cict
docker compose logs -f cict
```

验证与回滚同 A.3.2。

#### A.3.4 组件镜像更新（MariaDB / Redis / RabbitMQ / RustFS / Nginx）

镜像版本固定写在 `docker-compose.yml` 中，更新 = 修改对应 `image:` 的版本号后重建该服务。以 RustFS 为例：

```bash
cd /data/app/cict-docker
# 1. 有状态服务先按 A.7 备份（MariaDB、Redis、RabbitMQ、RustFS 都有数据卷）
# 2. 编辑 docker-compose.yml，把 rustfs/rustfs:1.0.0-rc.2 改为目标版本号
# 3. 拉取新镜像并应用（离线环境改为 docker load -i <镜像>.tar）
docker compose pull rustfs
docker compose up -d rustfs
docker compose logs -f rustfs
# 4. 回滚：把 image: 改回旧版本号，再执行 up -d rustfs
```

**警告**：MariaDB、RabbitMQ 的**大版本升级不能直接改 tag**——数据目录格式不兼容会导致服务起不来（见本文档开头的版本说明），必须按官方支持路径逐级升级或导出迁移；Redis、Nginx 的小版本更新相对安全，但同样先备份再操作。

#### A.3.5 整包更新（周期性交付 / 前后端同时发版）

```bash
# === 联网机器 ===
cd deploy/bundle
./build-bundle.sh --app-jar <新JAR> --frontend <新前端zip> --tag <新版本号>
# 将 dist/cict-offline-bundle-<tag>.tar.gz 与同名 .sha256 传输到目标机

# === 目标机 ===
sha256sum -c cict-offline-bundle-<tag>.tar.gz.sha256
tar -xzf cict-offline-bundle-<tag>.tar.gz
cd cict-offline-bundle && sudo ./install.sh    # 幂等：保留 .env 密码与数据卷
```

在线环境的整包升级更简单：在目标机仓库内重跑 3.0 的 `install.sh --online` 并换新 tag。

#### A.3.6 更新后总体验证清单

```bash
cd /data/app/cict-docker
docker compose ps                                   # 六个服务全部 running；mariadb/redis/rabbitmq 为 healthy
curl -sI http://127.0.0.1/ | head -1                 # HTTP/1.1 200 OK
docker compose logs --since 10m | grep -iE 'error|exception' || echo 无错误日志
```

最后用浏览器完整登录一次核心页面，确认业务功能正常后再通知使用方。

### A.4 Docker 服务管理速查表

| 操作 | 命令 |
|------|------|
| 启动全部服务 | `docker compose up -d` |
| 停止全部服务 | `docker compose down` |
| 重启全部服务 | `docker compose restart` |
| 查看服务状态 | `docker compose ps` |
| 查看某服务日志 | `docker compose logs -f cict` |
| 查看全部服务日志 | `docker compose logs -f` |
| 重启单个服务 | `docker compose restart cict` |
| 进入容器 | `docker compose exec mariadb sh` |
| 重新构建并启动 | `docker compose up -d --build` |
| 查看资源占用 | `docker stats` |

### A.5 Docker 各服务日志位置

| 服务 | 宿主机日志路径 | 容器内日志路径 |
|------|---------------|---------------|
| CICT 后端 | `/data/app/cict-docker/logs/cict/cict.log` | `/data/logs/cict.log` |
| Nginx | `/data/app/cict-docker/logs/nginx/` | `/var/log/nginx/` |
| RustFS | `/data/app/cict-docker/logs/rustfs/` | `/var/log/rustfs/` |
| MariaDB | `docker compose logs mariadb` | — |
| Redis | `docker compose logs redis` | — |
| RabbitMQ | `docker compose logs rabbitmq` | — |

### A.6 直接安装与 Docker 部署对照

| 维度 | 直接部署（systemd） | Docker 部署 |
|------|-------------------|-------------|
| 服务管理 | `systemctl start/stop/restart` | `docker compose start/stop/restart` |
| 日志查看 | `journalctl -u cict` / `tail -f log` | `docker compose logs -f cict` |
| 自动重启 | `Restart=on-failure` | `restart: unless-stopped` |
| 启动顺序 | `After=` + 应用连接重试 | `depends_on`；有原生检查的服务使用 `service_healthy`，其余服务要求应用连接重试 |
| 服务发现 | `127.0.0.1:<port>` | Docker 内部 DNS（服务名） |
| 环境隔离 | 共享宿主机环境 | 容器级别隔离 |
| 数据持久化 | 直接写宿主机文件系统 | Docker volume 挂载 |
| 离线安装 | deb 包 + 源码编译 | docker save/load 镜像 tar |
| 更新后端 | 替换 JAR + restart | 重新 build 镜像 + up -d |
| 数据库地址 | `127.0.0.1:3306` | `mariadb:3306`（Docker DNS） |
| RustFS 地址 | `127.0.0.1:9000` | `rustfs:9000` |

### A.7 数据备份与恢复

#### 直接安装

```bash
sudo install -d -m 0700 /backup

# MariaDB：root 通过本机 Unix socket 认证，不把密码放进命令行
sudo mariadb-dump --all-databases --single-transaction --routines --events --triggers \
  | sudo tee "/backup/mariadb_$(date +%F).sql" >/dev/null

# Redis
sudo -u redis /data/app/redis/bin/redis-cli SAVE
sudo cp /data/app/redis/data/dump.rdb "/backup/redis_$(date +%F).rdb"

# RabbitMQ 拓扑定义（消息数据另行制定快照策略）
sudo rabbitmqctl export_definitions "/backup/rabbitmq-definitions_$(date +%F).json"

# MariaDB 恢复示例
sudo mariadb < /backup/mariadb_2026-08-17.sql
```

> RustFS 不能只在运行中复制部分数据文件。应通过 S3 复制到另一存储，或在停止写入后对整个 `/data/rustfs0` 做文件系统快照，并定期执行恢复演练。

#### Docker 部署

```bash
cd /data/app/cict-docker

sudo install -d -m 0700 -o "$(id -un)" -g "$(id -gn)" /backup

# MariaDB：先在容器内生成，避免宿主机 shell/PowerShell 重定向改变编码
docker compose exec -T mariadb sh -c \
  'mariadb-dump -uroot -p"$MARIADB_ROOT_PASSWORD" --all-databases --single-transaction --routines --events --triggers > /tmp/cict-backup.sql'
docker compose cp mariadb:/tmp/cict-backup.sql "/backup/mariadb_$(date +%F).sql"

# Redis
docker compose exec -T redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli SAVE'
docker compose cp redis:/data/dump.rdb "/backup/redis_$(date +%F).rdb"

# RabbitMQ 拓扑定义
docker compose exec -T rabbitmq rabbitmqctl export_definitions /tmp/definitions.json
docker compose cp rabbitmq:/tmp/definitions.json "/backup/rabbitmq-definitions_$(date +%F).json"

# 恢复
docker compose cp /backup/mariadb_2026-08-17.sql mariadb:/tmp/cict-restore.sql
docker compose exec -T mariadb sh -c \
  'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" < /tmp/cict-restore.sql'
```

> Windows 使用 named volume 时，上述 `docker compose cp` 同样适用，只需把 `/backup/...` 换成 `C:\backup\...`，日期可用 `Get-Date -Format yyyy-MM-dd` 生成。RustFS 仍应通过 S3 复制或一致性快照备份，不能依赖宿主机项目目录中的文件。

#### 自动备份定时任务

```bash
sudo install -d -m 0700 /backup
sudo crontab -e
```

```cron
# 直接安装（root crontab）
0 2 * * * /usr/bin/mariadb-dump --all-databases --single-transaction --routines --events --triggers > /backup/mariadb_$(date +\%F).sql
10 2 * * * /data/app/redis/bin/redis-cli SAVE && cp /data/app/redis/data/dump.rdb /backup/redis_$(date +\%F).rdb

# Docker 部署（root crontab，按部署方式二选一）
0 2 * * * cd /data/app/cict-docker && /usr/bin/docker compose exec -T mariadb sh -c 'mariadb-dump -uroot -p"$MARIADB_ROOT_PASSWORD" --all-databases --single-transaction --routines --events --triggers > /tmp/cict-backup.sql' && /usr/bin/docker compose cp mariadb:/tmp/cict-backup.sql /backup/mariadb_$(date +\%F).sql
10 2 * * * cd /data/app/cict-docker && /usr/bin/docker compose exec -T redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli SAVE' && /usr/bin/docker compose cp redis:/data/dump.rdb /backup/redis_$(date +\%F).rdb

# 保留 14 天；执行前先确认 /backup 是专用备份目录
30 3 * * * find /backup -type f -mtime +14 -delete
```

### A.8 日志轮转

#### 直接安装

```
# /etc/logrotate.d/nginx
/data/app/nginx/logs/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    sharedscripts
    postrotate
        [ -f /data/app/nginx/logs/nginx.pid ] && kill -USR1 `cat /data/app/nginx/logs/nginx.pid`
    endscript
}

# /etc/logrotate.d/cict
/data/app/cict/logs/*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}

# /etc/logrotate.d/redis-rustfs
/data/app/redis/logs/*.log /var/log/rustfs/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
}
```

#### Docker 部署

Docker 容器日志由 Docker 自身管理，在 `/etc/docker/daemon.json` 中配置：

```json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "50m",
    "max-file": "5"
  }
}
```

宿主机文件日志轮转：

```
# /etc/logrotate.d/cict-docker
/data/app/cict-docker/logs/cict/*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}

/data/app/cict-docker/logs/rustfs/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
}

/data/app/cict-docker/logs/nginx/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    sharedscripts
    postrotate
        docker compose -f /data/app/cict-docker/docker-compose.yml exec -T nginx nginx -s reload 2>/dev/null || true
    endscript
}
```

#### Docker 磁盘空间清理

```bash
docker system df              # 查看磁盘占用
docker system prune -f        # 清理未使用的镜像、容器、网络
docker image prune -a -f      # 清理未使用的镜像
docker volume prune -f        # 清理未使用的卷（谨慎！）
```

> 离线环境慎用 `docker image prune -a`，它可能删除短期未运行但无法重新拉取的业务镜像。`docker volume prune` 可能永久删除 named volume 中的数据，执行前必须先核对 `docker volume ls` 并验证备份可恢复。

---

## 附录 B：CentOS 与 Ubuntu 差异对照

> 原始文档基于 CentOS 7.8，本文档适配为 Ubuntu 24.04。以下为关键差异对照，供从 CentOS 迁移时参考。

| 项目 | CentOS 7 (原文档) | Ubuntu 24 (本文档) |
|------|-------------------|-------------------|
| 包格式 | `.rpm` | `.deb` |
| 包管理器 | `yum` / `rpm` | `apt` / `dpkg` |
| 离线下载 | `yum install --downloadonly --downloaddir=<dir> <pkg>` | `apt-get download $(apt-cache depends --recurse ... <pkg>)` |
| 离线安装 | `yum localinstall -y *.rpm` | 本地 file apt 仓库 + `apt-get --no-download install` |
| 依赖校验 | `rpm -qpR <pkg>.rpm` | `dpkg --info <pkg>.deb \| grep Depends` |
| systemd 开发包 | `systemd-devel` | `libsystemd-dev` |
| PCRE 开发包 | `pcre` / `pcre-devel` | `libpcre3` / `libpcre3-dev` |
| zlib 开发包 | `zlib` / `zlib-devel` | `zlib1g` / `zlib1g-dev` |
| SSL 开发包 | `openssl-devel` | `libssl-dev` |
| MariaDB 日志路径 | `/var/log/mariadb/mariadb.log` | `/var/log/mysql/mariadb.log` |
| JDK 默认路径 (apt) | N/A | `/usr/lib/jvm/java-17-openjdk-<架构>` |
| 系统版本代号 | N/A | `noble` (Ubuntu 24.04) |
| 后端运行方式 | Tomcat 容器部署 | systemd 直接运行 JAR |
| 后端端口 | 8080 (Tomcat) | 9090 (Spring Boot) |

---

## 附录 C：官方参考资料

- [RabbitMQ：Ubuntu/Debian 安装](https://www.rabbitmq.com/docs/install-debian)
- [RabbitMQ：Erlang 兼容矩阵](https://www.rabbitmq.com/docs/which-erlang)
- [MariaDB：仓库配置](https://mariadb.org/download/?t=repo-config)
- [Docker Engine：Ubuntu 安装](https://docs.docker.com/engine/install/ubuntu/)
- [RustFS：Docker 安装](https://docs.rustfs.com/en/installation/container/docker)
