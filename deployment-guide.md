# 部署指南

> 本文档涵盖 CICT 全套服务在 Ubuntu 24 和 Windows 上的部署，包含直接安装和 Docker 两种方式，每种方式均覆盖在线与离线场景。
>
> 后端通过 systemd（Ubuntu 直接安装）或 Docker 容器运行 Spring Boot JAR 包，无需 Tomcat。
>
> 修订基线：2026-08-17。执行生产部署前仍需在与目标环境相同的系统版本、CPU 架构和网络条件下完成一次全流程验收。

---

## 目录

- [第 1 章 Ubuntu 24 在线安装](#第-1-章-ubuntu-24-在线安装)
- [第 2 章 Ubuntu 24 离线安装](#第-2-章-ubuntu-24-离线安装)
- [第 3 章 Ubuntu 在线使用 Docker 安装](#第-3-章-ubuntu-在线使用-docker-安装)
- [第 4 章 Ubuntu 离线使用 Docker 安装](#第-4-章-ubuntu-离线使用-docker-安装)
- [第 5 章 Windows 在线使用 Docker 安装](#第-5-章-windows-在线使用-docker-安装)
- [第 6 章 Windows 离线使用 Docker 安装](#第-6-章-windows-离线使用-docker-安装)
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

## 第 1 章 Ubuntu 24 在线安装

> 在联网的 Ubuntu 24.04 LTS 环境下，通过 apt 源和源码编译直接安装各组件，使用 systemd 管理服务。

### 1.1 环境准备

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

### 1.2 安装 MariaDB 11.4

#### 1.2.1 添加 MariaDB 官方源

```bash
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://mariadb.org/mariadb_release_signing_key.asc \
  | sudo gpg --dearmor -o /etc/apt/keyrings/mariadb-keyring.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/mariadb-keyring.gpg] https://mirrors.aliyun.com/mariadb/repo/11.4/ubuntu noble main" \
  | sudo tee /etc/apt/sources.list.d/mariadb.list
```

> 如果阿里云镜像不可用，可替换为官方源 `https://deb.mariadb.org/11.4/ubuntu noble main`。

#### 1.2.2 安装

```bash
sudo apt update
sudo apt install -y mariadb-server mariadb-client
```

#### 1.2.3 初始化数据目录

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

#### 1.2.4 配置 MariaDB

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

#### 1.2.5 启动并创建业务库

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

#### 1.2.6 验证

```bash
mariadb -uroot -p
SHOW VARIABLES LIKE "version";
SHOW VARIABLES LIKE "datadir";
SHOW VARIABLES LIKE "character_set_server";
```

### 1.3 安装 Redis 7.4

#### 1.3.1 安装编译依赖

```bash
sudo apt install -y build-essential tcl libsystemd-dev pkg-config
```

#### 1.3.2 下载并编译

```bash
cd /data/app
wget https://download.redis.io/releases/redis-7.4.10.tar.gz
tar -zxvf redis-7.4.10.tar.gz
cd redis-7.4.10

make USE_SYSTEMD=yes
sudo make USE_SYSTEMD=yes install PREFIX=/data/app/redis
```

#### 1.3.3 配置 Redis

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

#### 1.3.4 配置 systemd 服务

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

#### 1.3.5 启动并验证

```bash
sudo systemctl start redis
/data/app/redis/bin/redis-cli ping
```

输出：

```
PONG
```

### 1.4 安装 RabbitMQ 4.3

> RabbitMQ 3.8 已停止支持，且不能与 Erlang 25/26 混用。本节改用当前受支持的 RabbitMQ 4.3 系列和 Erlang 27，并使用 Team RabbitMQ 为 Ubuntu 24.04 提供的软件源。以下仓库只提供 amd64 包；arm64 请按 RabbitMQ 官方文档改用其 Launchpad Erlang 仓库。

#### 1.4.1 添加 Team RabbitMQ 软件源

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

#### 1.4.2 安装 Erlang 与 RabbitMQ

```bash
sudo apt-get install -y erlang-base erlang-asn1 erlang-crypto erlang-eldap \
  erlang-ftp erlang-inets erlang-mnesia erlang-os-mon erlang-parsetools \
  erlang-public-key erlang-runtime-tools erlang-snmp erlang-ssl \
  erlang-syntax-tools erlang-tftp erlang-tools erlang-xmerl
sudo apt-get install -y rabbitmq-server --fix-missing

erl -noshell -eval 'io:format("~s~n", [erlang:system_info(otp_release)]), halt().'
sudo rabbitmqctl version
```

#### 1.4.3 启动并设置开机自启

```bash
sudo systemctl enable rabbitmq-server
sudo systemctl start rabbitmq-server
```

#### 1.4.4 启用 Web 管理界面并创建账号

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

### 1.5 安装 JDK 17

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

### 1.6 安装 Nginx

#### 1.6.1 安装编译依赖

```bash
sudo apt install -y libpcre3 libpcre3-dev zlib1g zlib1g-dev gcc make libssl-dev
```

#### 1.6.2 下载并编译

```bash
cd /data/app
wget https://nginx.org/download/nginx-1.28.3.tar.gz
tar -zxvf nginx-1.28.3.tar.gz
cd nginx-1.28.3
./configure --prefix=/data/app/nginx
make && make install
```

#### 1.6.3 配置 systemd 服务

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
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable nginx
```

#### 1.6.4 启动与验证

```bash
sudo systemctl start nginx
sudo systemctl status nginx
```

访问：`http://<服务器IP>`

#### 1.6.5 配置优化（gzip 与独立配置文件）

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

    # 不带结尾斜杠，保留原始 /api 前缀
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

### 1.7 安装 RustFS

> **安全要求**：部署前生成独立的 `RUSTFS_ACCESS_KEY` 和 `RUSTFS_SECRET_KEY`；生产环境禁止使用公开默认值 `rustfsadmin`。

#### 1.7.1 下载并安装

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

#### 1.7.2 配置环境变量

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
RUST_LOG=error
RUSTFS_OBS_LOG_DIRECTORY="/var/log/rustfs/"
EOF
sudo chown root:rustfs /etc/default/rustfs
sudo chmod 640 /etc/default/rustfs
```

> 将两个占位符替换为分别至少 16 和 32 字节的随机值，并保存到密码管理系统。不要使用 `rustfsadmin` 等公开默认值。

#### 1.7.3 创建存储目录

```bash
sudo mkdir -p /data/rustfs0 /var/log/rustfs
sudo chown -R rustfs:rustfs /data/rustfs0 /var/log/rustfs
sudo chmod -R 750 /data/rustfs0 /var/log/rustfs
```

#### 1.7.4 配置 systemd 服务

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

#### 1.7.5 启动并验证

```bash
sudo systemctl enable --now rustfs
sudo systemctl status rustfs
ss -lntp | grep -E ':9000|:9001'
```

S3 API 供本机后端通过 `http://127.0.0.1:9000` 使用。管理时先执行 `ssh -L 9001:127.0.0.1:9001 <用户>@<服务器IP>`，再访问 `http://127.0.0.1:9001`，使用 `/etc/default/rustfs` 中配置的访问密钥登录。

> 后端服务配置中引用了 RustFS 的 `pike` 存储桶，请在 RustFS 控制台中创建该存储桶。

### 1.8 后端运行环境配置

后端无需 Tomcat，直接通过 systemd 运行 Spring Boot JAR 包。

#### 1.8.1 创建目录结构

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

#### 1.8.2 创建 JVM 参数配置

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

#### 1.8.3 创建 Spring Boot 配置文件

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

#### 1.8.4 创建 systemd 服务

创建 `/etc/systemd/system/cict.service`：

```bash
sudo tee /etc/systemd/system/cict.service <<'EOF'
[Unit]
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

ExecStart=java $JAVA_OPTS -jar /data/app/cict/current.jar --spring.config.additional-location=/etc/cict/application.properties

StandardOutput=journal
StandardError=journal
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
```

| 配置项 | 作用 |
|--------|------|
| `current.jar` | 运维明确指定当前版本，避免按修改时间误选 JAR；回滚时切换符号链接即可 |
| `ExecStart` | 使用 `$JAVA_OPTS`（来自 `cict.conf`）运行 `current.jar`，并加载外部配置文件 |
| `Restart=on-failure` | 进程异常退出时自动重启 |
| `StartLimitBurst=3` | 60 秒内最多重启 3 次，超过后停止重试（参数位于 `[Unit]`） |

```bash
sudo systemctl daemon-reload
sudo systemctl enable cict
```

### 1.9 部署前后端代码

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

sudo rsync -a --delete "$FRONTEND_SOURCE/" /data/app/nginx/html/
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

## 第 2 章 Ubuntu 24 离线安装

> 在无网络的 Ubuntu 24.04 环境下，先在联网的同版本机器上下载全部 deb 包和源码，打包后拷贝至离线服务器安装。安装步骤和配置与第 1 章相同，区别在于获取软件包的方式。

### 2.1 联网机器准备

在一台与离线服务器**同架构、同系统版本**的 Ubuntu 24.04 联网机器上操作：

```bash
sudo apt update && sudo apt install -y dpkg-dev apt-rdepends wget curl unzip tar gnupg

mkdir -p ~/offline_packages/{mariadb,redis,redis-deps,rabbitmq,jdk,nginx,nginx-deps,rustfs,base-tools,deb-repo}
```

### 2.2 离线包下载清单

> 以下所有命令在**联网机器**上执行。

#### 2.2.1 基础工具包

```bash
cd ~/offline_packages/base-tools

apt-get download $(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances \
  build-essential gcc make pkg-config net-tools unzip curl wget tar libssl-dev ca-certificates gnupg rsync ufw | grep "^\w" | sort -u)
```

校验：`dpkg --info *.deb | grep Depends`

#### 2.2.2 MariaDB 11.4

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

#### 2.2.3 Redis 源码与编译依赖

```bash
# 下载 Redis 源码
cd ~/offline_packages/redis
wget https://download.redis.io/releases/redis-7.4.10.tar.gz

# 下载编译依赖
cd ~/offline_packages/redis-deps
apt-get download $(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances \
  libsystemd-dev tcl pkg-config | grep "^\w" | sort -u)
```

#### 2.2.4 RabbitMQ 与 Erlang

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

#### 2.2.5 Nginx 源码与编译依赖

```bash
cd ~/offline_packages/nginx
wget https://nginx.org/download/nginx-1.28.3.tar.gz

cd ~/offline_packages/nginx-deps
apt-get download $(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances \
  libpcre3 libpcre3-dev zlib1g zlib1g-dev libssl-dev gcc make | grep "^\w" | sort -u)
```

#### 2.2.6 JDK 17（deb 包）

```bash
cd ~/offline_packages/jdk
apt-get download $(apt-cache depends --recurse --no-recommends --no-suggests --no-conflicts --no-breaks --no-replaces --no-enhances \
  openjdk-17-jdk-headless | grep "^\w" | sort -u)
```

#### 2.2.7 RustFS（MUSL 二进制包）

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

#### 2.2.8 生成本地 apt 仓库和校验清单

```bash
cd ~/offline_packages
find base-tools mariadb redis-deps rabbitmq jdk nginx-deps -name '*.deb' -exec cp -n {} deb-repo/ \;
cd deb-repo
dpkg-scanpackages . /dev/null | gzip -9c > Packages.gz

cd ~/offline_packages
find . -type f ! -name bundle.sha256 -print0 | sort -z | xargs -0 sha256sum > bundle.sha256
```

### 2.3 打包并传输

```bash
cd ~
tar -czf offline_packages.tar.gz offline_packages/
scp offline_packages.tar.gz root@<离线服务器IP>:/data/app/
```

### 2.4 离线服务器安装

> 以下命令在**离线服务器**上执行。

```bash
cd /data/app
tar -zxvf offline_packages.tar.gz
cd /data/app/offline_packages
sha256sum -c bundle.sha256
```

#### 2.4.1 配置本地 apt 仓库并安装基础工具

```bash
echo 'deb [trusted=yes] file:/data/app/offline_packages/deb-repo ./' \
  | sudo tee /etc/apt/sources.list.d/cict-offline.list
sudo apt update
sudo apt-get install -y --no-download build-essential gcc make pkg-config net-tools \
  unzip curl wget tar libssl-dev ca-certificates gnupg rsync ufw
```

#### 2.4.2 安装 MariaDB

```bash
sudo apt-get install -y --no-download mariadb-server mariadb-client
```

按 [1.2.3 初始化数据目录](#123-初始化数据目录) 在空的 `/data/db/mysql` 上以 `lower_case_table_names=1` 初始化；不得复制已按默认值 `0` 初始化的 `/var/lib/mysql`。

创建配置文件 `/etc/mysql/mariadb.conf.d/server.cnf`，内容与 [1.2.4 配置 MariaDB](#124-配置-mariadb) 完全相同。

```bash
sudo systemctl enable mariadb
sudo systemctl start mariadb
sudo mariadb-secure-installation
```

随后按 [1.2.5 启动并创建业务库](#125-启动并创建业务库) 创建 `cict` 数据库和业务账号。

#### 2.4.3 安装 Redis

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

#### 2.4.4 安装 RabbitMQ

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

#### 2.4.5 安装 JDK 17

```bash
sudo apt-get install -y --no-download openjdk-17-jdk-headless
java -version
```

#### 2.4.6 安装 Nginx

```bash
sudo apt-get install -y --no-download libpcre3 libpcre3-dev zlib1g zlib1g-dev libssl-dev gcc make

cd /data/app
tar -zxvf /data/app/offline_packages/nginx/nginx-1.28.3.tar.gz
cd nginx-1.28.3
./configure --prefix=/data/app/nginx
make && make install
```

后续 Nginx systemd 服务创建与配置与 [1.6.3 - 1.6.5](#163-配置-systemd-服务) 完全相同。

#### 2.4.7 安装 RustFS

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

#### 2.4.8 后端运行环境配置

与 [1.8 后端运行环境配置](#18-后端运行环境配置) 完全相同。

#### 2.4.9 部署前后端代码

与 [1.9 部署前后端代码](#19-部署前后端代码) 完全相同。


---

## 第 3 章 Ubuntu 在线使用 Docker 安装

> 通过 Docker Compose 编排全部服务。Docker 环境中服务间通过 Docker 内部 DNS 通信，数据库地址使用服务名而非 `127.0.0.1`。

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

> **重要**：第 3-6 章共用以下配置文件，Docker 部署的配置与直接安装不同——服务间通过 Docker 内部 DNS 通信，地址使用服务名。

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

> 配置项修订说明见 [1.2.4 配置 MariaDB](#124-配置-mariadb) 中的配置修订表。

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

    # 不带结尾斜杠，保留原始 /api 前缀
    location /api/ {
        proxy_pass http://cict:9090;
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

> 前端和 API 必须位于同一个默认虚拟主机中。`proxy_pass` 中的 `cict` 是 Docker Compose 服务名；这里不使用结尾斜杠，以保留后端路由所需的 `/api` 前缀。

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

> 中国大陆环境如需镜像加速，只添加云厂商为当前账号或网络明确提供的最新地址，并先用 `docker pull hello-world` 验证。公共镜像地址经常失效，不应把未经验证的地址固化到生产文档。第 4、6 章的联网制包机器同样适用。

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

### 3.11 部署前后端代码

#### 前端

```bash
FRONTEND_TMP="$(mktemp -d)"
unzip -q dist.zip -d "$FRONTEND_TMP"
if [ -f "$FRONTEND_TMP/index.html" ]; then
  FRONTEND_SOURCE="$FRONTEND_TMP"
elif [ -f "$FRONTEND_TMP/dist/index.html" ]; then
  FRONTEND_SOURCE="$FRONTEND_TMP/dist"
else
  echo "index.html not found in dist.zip" >&2
  exit 1
fi
rsync -a --delete "$FRONTEND_SOURCE/" /data/app/cict-docker/nginx/html/
cd /data/app/cict-docker
docker compose exec nginx nginx -s reload
```

#### 后端

```bash
install -m 0644 cict-dist-2.1.2-SNAPSHOT.jar /data/app/cict-docker/cict/app.jar
cd /data/app/cict-docker

# 每次发布先把 .env 中 CICT_IMAGE_TAG 改为唯一版本号，禁止长期复用 latest 或同一 tag
docker compose build cict
docker compose up -d cict
docker compose logs -f cict
```


---

## 第 4 章 Ubuntu 离线使用 Docker 安装

> 在联网机器上拉取全部镜像并导出为 tar 文件，同时下载 Docker Engine 离线 deb 包，打包拷贝至离线服务器后加载使用。配置文件与第 3 章完全相同。

### 4.1 联网机器准备

在一台与离线服务器**同架构、同系统版本**的联网 Ubuntu 24.04 机器上操作。

#### 4.1.1 安装 Docker

同 [3.5 安装 Docker Engine](#35-安装-docker-engine) 步骤。

#### 4.1.2 准备项目文件

将完整的项目目录结构（`docker-compose.yml`、`.env`、`cict/`、`nginx/`、`mariadb/`）拷贝到联网机器上。

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

离线环境无法直接 `docker compose build`，需在联网机器上构建并导出：

```bash
# === 联网机器 ===
install -m 0644 cict-dist-2.1.2-SNAPSHOT.jar /data/app/cict-docker/cict/app.jar
cd /data/app/cict-docker
# 将 .env 中 CICT_IMAGE_TAG 改为本次发布的唯一版本号
docker compose build cict
CICT_IMAGE="$(docker compose config --images | awk '/^cict\/backend:/{print; exit}')"
docker save -o cict-backend.tar "$CICT_IMAGE"
sha256sum cict-backend.tar > cict-backend.tar.sha256
scp cict-backend.tar cict-backend.tar.sha256 root@<离线服务器IP>:/data/app/docker-images/
```

```bash
# === 离线服务器 ===
cd /data/app/docker-images
sha256sum -c cict-backend.tar.sha256
docker load -i cict-backend.tar
cd /data/app/cict-docker
# 将 .env 中 CICT_IMAGE_TAG 更新为联网机器构建时使用的相同值
docker compose up -d cict
docker compose logs -f cict
```

### 4.6 离线镜像管理参考

#### 镜像导出/导入命令速查

| 操作 | 命令 |
|------|------|
| 导出单个镜像 | `docker save -o <name>.tar <image>:<tag>` |
| 导出多个镜像 | `docker save -o all.tar image1:tag image2:tag` |
| 导入镜像 | `docker load -i <name>.tar` |
| 查看镜像列表 | `docker images` |
| 删除镜像 | `docker rmi <image>:<tag>` |

#### 批量导出/导入脚本

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

## 第 5 章 Windows 在线使用 Docker 安装

> Windows 上使用 Docker Desktop 的 WSL2 后端。Docker Desktop 只支持满足其当前系统要求的 Windows 10/11 客户端系统，**不支持 Windows Server**。Docker Desktop 会自行管理 `docker-desktop` 环境，日常运行容器不要求用户另外安装 Ubuntu 发行版。

### 5.1 架构说明

```
Windows 宿主机
├── Docker Desktop（管理界面 + 守护进程）
│   └── WSL2 轻量级 Linux 内核（自动安装，用户不可见）
│       └── docker-desktop（隐藏发行版）
│           └── Docker Engine (dockerd)
│               └── 容器们（mariadb/redis/nginx/cict...）
└── Docker CLI（docker / docker compose 命令）
    └── 通过命名管道与 Docker Engine 通信
```

所有容器都是标准 Linux 容器，服务配置与 Ubuntu 版相同。若项目放在 `C:\` 等 Windows 文件系统中，数据库数据应改用 Docker named volume，不能把 Linux 数据库目录长期 bind mount 到 NTFS；本章会增加一个自动加载的覆盖文件。

### 5.2 安装步骤

1. **系统要求**：Windows 10/11 64 位客户端系统，并满足当前 Docker Desktop 对版本、虚拟化和内存的要求。Windows Server 请改用 Linux 虚拟机或受支持的服务器容器方案
2. **启用 WSL2**：以管理员身份运行 PowerShell：

   ```powershell
   wsl --install --no-distribution
   wsl --update
   ```

   > `--no-distribution` 才表示不安装默认 Ubuntu；不带该参数的 `wsl --install` 通常会同时安装默认 Linux 发行版。

3. **重启**：按提示重启计算机
4. **安装 Docker Desktop**：从 https://www.docker.com/products/docker-desktop/ 下载官方安装程序。公开下载通常是 `Docker Desktop Installer.exe`；企业 MSI 需通过相应企业分发渠道取得
5. **验证**：

   ```powershell
   docker --version
   docker compose version
   ```

### 5.3 项目目录选择

| 场景 | 项目目录放哪里 | IO 性能 | 说明 |
|------|-------------|--------|------|
| 只运行容器，不频繁构建镜像 | Windows 目录（如 `C:\cict-docker\`） | 可接受 | Docker 通过 9P 协议跨文件系统访问 volume，IO 有损耗但功能正常 |
| 频繁构建镜像（`docker compose build`） | WSL2 内部文件系统 | 接近原生 Linux | 需额外安装一个 Ubuntu 发行版（见下方说明） |

**是否需要额外安装 Ubuntu WSL 发行版？**

- **不需要**：如果只是 `docker compose up -d` 运行容器、偶尔构建镜像，直接把项目放在 Windows 目录下即可
- **建议安装**：如果需要频繁构建后端镜像（每次更新 JAR 都要 `docker compose build`），安装一个 Ubuntu 发行版可以获得 Linux 原生 ext4 文件系统的 IO 性能

```powershell
# 安装 Ubuntu（从 Microsoft Store）
wsl --install -d Ubuntu

# 安装完成后，项目目录应放在 WSL2 内部
# 路径示例：\\wsl$\Ubuntu\home\<user>\cict-docker\
```

> 只有执行了上述命令安装了 Ubuntu 发行版后，`\\wsl$\Ubuntu\` 路径才存在。Docker Desktop 自动创建的 `docker-desktop` 发行版是隐藏的，不用于存放用户文件。

### 5.4 准备项目文件

将第 3 章中的配置文件和目录结构创建到 Windows 目录（或 WSL2 内部目录）。基础内容使用第 3 章 [3.3 配置文件](#33-配置文件) 和 [3.4 docker-compose.yml](#34-docker-composeyml)。将后端 JAR 固定命名为 `cict\app.jar`。

在 PowerShell 中生成 Windows 版 `.env`，避免复制公开示例密码：

```powershell
function New-HexSecret([int]$ByteCount) {
  $Bytes = New-Object byte[] $ByteCount
  $Rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try { $Rng.GetBytes($Bytes) } finally { $Rng.Dispose() }
  return (($Bytes | ForEach-Object { $_.ToString('x2') }) -join '')
}

$MariaRoot = New-HexSecret 24
$MariaApp = New-HexSecret 24
$RedisPass = New-HexSecret 24
$RabbitPass = New-HexSecret 24
$RabbitAdminPass = New-HexSecret 24
$RustAccess = New-HexSecret 16
$RustSecret = New-HexSecret 32

@"
MARIADB_ROOT_PASSWORD=$MariaRoot
MARIADB_DATABASE=cict
MARIADB_USER=cict
MARIADB_PASSWORD=$MariaApp
REDIS_PASSWORD=$RedisPass
RABBITMQ_DEFAULT_USER=cict
RABBITMQ_DEFAULT_PASS=$RabbitPass
RABBITMQ_ADMIN_PASSWORD=$RabbitAdminPass
RUSTFS_ACCESS_KEY=$RustAccess
RUSTFS_SECRET_KEY=$RustSecret
JAVA_TOOL_OPTIONS=-Xms1g -Xmx4g
CICT_IMAGE_TAG=2.1.2-SNAPSHOT
NGINX_PORT=80
ADMIN_BIND_ADDRESS=127.0.0.1
RABBITMQ_MGMT_PORT=15672
RUSTFS_PORT=9000
RUSTFS_CONSOLE_PORT=9001
"@ | Set-Content -Encoding ASCII .env

icacls .env /inheritance:r /grant:r "$($env:USERNAME):(R,W)"
Remove-Variable MariaRoot,MariaApp,RedisPass,RabbitPass,RabbitAdminPass,RustAccess,RustSecret
```

将 `.env` 中的密码安全备份到密码管理系统，不要提交到 Git。

### 5.5 Windows 数据卷覆盖文件

如果项目位于 `C:\cict-docker` 等 Windows 文件系统中，创建 `docker-compose.override.yml`，让数据库和对象数据进入 Docker 管理的 Linux volume：

```yaml
services:
  mariadb:
    volumes:
      - mariadb-data:/var/lib/mysql
  redis:
    volumes:
      - redis-data:/data
  rabbitmq:
    volumes:
      - rabbitmq-data:/var/lib/rabbitmq
  rustfs:
    volumes:
      - rustfs-data:/data

volumes:
  mariadb-data:
  redis-data:
  rabbitmq-data:
  rustfs-data:
```

> Compose 会自动加载 `docker-compose.override.yml`。如果整个项目已经放在 Ubuntu WSL 发行版的 ext4 文件系统中，可以不创建该覆盖文件，继续使用第 3 章的 bind mount。

### 5.6 构建并启动

```powershell
cd C:\cict-docker
docker compose build cict
$CictImage = docker compose config --images | Where-Object { $_ -like 'cict/backend:*' } | Select-Object -First 1
docker compose up -d
```

### 5.7 验证

在 PowerShell 中执行：

```powershell
docker compose ps
docker compose exec -T mariadb sh -c 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" -e "SELECT VERSION();"'
docker compose exec -T redis sh -c 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli ping'

# 首次部署时创建独立管理账号，并移除应用账号的管理标签
docker compose exec -T rabbitmq sh -c 'rabbitmqctl set_user_tags "$RABBITMQ_DEFAULT_USER"; if ! rabbitmqctl list_users --silent | awk "{print \$1}" | grep -qx cict_admin; then rabbitmqctl add_user cict_admin "$RABBITMQ_ADMIN_PASSWORD"; fi; rabbitmqctl set_user_tags cict_admin administrator; rabbitmqctl set_permissions -p / cict_admin ".*" ".*" ".*"'
```

随后访问 `http://127.0.0.1`、`http://127.0.0.1:15672` 和 `http://127.0.0.1:9001`；RabbitMQ 使用 `cict_admin` 登录。

### 5.8 在 RustFS 中创建存储桶

访问 `http://127.0.0.1:9001`，使用 `.env` 中的 RustFS 密钥登录并创建 `pike` 存储桶。

### 5.9 部署前后端代码

#### 前端

先解压到临时目录并确认 `index.html` 所在层级，再替换站点文件：

```powershell
$FrontendTemp = Join-Path $env:TEMP "cict-frontend"
Remove-Item $FrontendTemp -Recurse -Force -ErrorAction SilentlyContinue
Expand-Archive .\dist.zip $FrontendTemp -Force

if (Test-Path (Join-Path $FrontendTemp "index.html")) {
  $FrontendSource = $FrontendTemp
} elseif (Test-Path (Join-Path $FrontendTemp "dist\index.html")) {
  $FrontendSource = Join-Path $FrontendTemp "dist"
} else {
  throw "index.html not found in dist.zip"
}

Remove-Item C:\cict-docker\nginx\html\* -Recurse -Force
Copy-Item (Join-Path $FrontendSource "*") C:\cict-docker\nginx\html\ -Recurse -Force

cd C:\cict-docker
docker compose exec nginx nginx -s reload
```

#### 后端

```powershell
# 将 JAR 包复制到 cict 目录
Copy-Item -Force .\cict-dist-2.1.2-SNAPSHOT.jar C:\cict-docker\cict\app.jar

cd C:\cict-docker
# 每次发布先把 .env 中 CICT_IMAGE_TAG 改为唯一版本号
docker compose build cict
docker compose up -d cict
docker compose logs -f cict
```

### 5.10 Windows 环境注意事项

| 注意点 | 说明 |
|--------|------|
| 换行符 | Windows 上编辑配置文件（`nginx.conf`、`application.properties`）时，确保使用 LF 换行符而非 CRLF，否则容器内可能解析异常。建议使用 VS Code 并在右下角切换为 LF |
| 文件权限 | Docker Desktop 的 WSL2 后端会自动处理 Windows 与 Linux 的权限映射，通常无需手动调整 |
| 端口占用 | 确保 Windows 上没有其他服务占用 80、15672、9000、9001 端口。IIS 等服务可能占用 80 端口 |
| 开机启动 | Docker Desktop 默认开机自启，容器设置为 `restart: unless-stopped` 后会随 Docker 自动启动 |

> **生产环境建议**：Docker Desktop 的授权取决于组织规模和使用场景；它不支持 Windows Server。服务器生产环境优先使用 Linux 主机，或在 Windows Server 上部署受支持的 Linux 虚拟机后运行 Docker Engine。

---

## 第 6 章 Windows 离线使用 Docker 安装

> 在联网的 Windows 10/11 机器上下载 WSL 离线安装包和 Docker Desktop 安装程序，拉取全部镜像并导出为 tar 文件，再拷贝至离线 Windows 10/11 主机。Docker Desktop 不支持 Windows Server。

### 6.1 联网机器准备

#### 6.1.1 安装 Docker Desktop

同 [5.2 安装步骤](#52-安装步骤)。

#### 6.1.2 准备项目文件

将完整的项目目录结构拷贝到联网机器上（配置文件内容与第 3 章相同）。

#### 6.1.3 下载 WSL 与 Docker Desktop 离线安装包

1. 从 Microsoft WSL GitHub Releases 下载与目标架构匹配的 WSL MSI，例如 `wsl.<版本>.x64.msi` 或 arm64 版本。
2. 从 https://www.docker.com/products/docker-desktop/ 下载 `Docker Desktop Installer.exe`；企业用户也可使用其分发渠道提供的 MSI。
3. 记录两个安装包的版本和 SHA-256，并与镜像包一起传输。

### 6.2 拉取并导出全部镜像

```powershell
$BundleBuildDir = "C:\cict-offline-build"
$ImageDir = Join-Path $BundleBuildDir "docker-images"
New-Item -ItemType Directory -Force $ImageDir | Out-Null

# 拉取官方镜像
docker pull mariadb:11.4
docker pull redis:7.4.10
docker pull rabbitmq:4.3.4-management
docker pull nginx:1.28.3
docker pull eclipse-temurin:17-jre-jammy
docker pull rustfs/rustfs:1.0.0-rc.2

# 构建后端镜像
cd C:\cict-docker
docker compose build cict
$CictImage = docker compose config --images | Where-Object { $_ -like 'cict/backend:*' } | Select-Object -First 1

# 始终使用绝对输出目录，避免 cd 后产生嵌套 docker-images 目录
docker save -o (Join-Path $ImageDir "mariadb.tar") mariadb:11.4
docker save -o (Join-Path $ImageDir "redis.tar") redis:7.4.10
docker save -o (Join-Path $ImageDir "rabbitmq.tar") rabbitmq:4.3.4-management
docker save -o (Join-Path $ImageDir "nginx.tar") nginx:1.28.3
docker save -o (Join-Path $ImageDir "eclipse-temurin.tar") eclipse-temurin:17-jre-jammy
docker save -o (Join-Path $ImageDir "rustfs.tar") rustfs/rustfs:1.0.0-rc.2
docker save -o (Join-Path $ImageDir "cict-backend.tar") $CictImage

# 生成可由 PowerShell 自动验证的校验清单
Get-ChildItem $ImageDir -Filter *.tar | Get-FileHash -Algorithm SHA256 |
  Select-Object @{Name='File';Expression={Split-Path $_.Path -Leaf}}, Hash |
  Export-Csv (Join-Path $ImageDir "images.sha256.csv") -NoTypeInformation -Encoding UTF8
```

### 6.3 打包并传输

将以下内容打包拷贝到离线 Windows 服务器：

```
cict-docker-offline-bundle/
├── Docker Desktop Installer.exe # Docker Desktop 安装包
├── wsl.<版本>.<架构>.msi         # WSL 离线安装包
├── cict-docker/                # 项目文件
│   ├── docker-compose.yml
│   ├── docker-compose.override.yml # 项目位于 Windows 文件系统时使用
│   ├── .env
│   ├── cict/
│   ├── nginx/
│   └── mariadb/
└── docker-images/              # 镜像 tar 文件
    ├── mariadb.tar
    ├── redis.tar
    ├── ...
    └── images.sha256.csv
```

### 6.4 离线服务器安装

> 以下操作在**离线 Windows 服务器**上执行。

#### 6.4.1 离线启用并安装 WSL2

以管理员身份运行 PowerShell：

```powershell
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart

# 文件名按实际下载版本替换
Start-Process msiexec.exe -Wait -ArgumentList '/i', '.\wsl.<版本>.<架构>.msi', '/quiet', '/norestart'
```

重启计算机后验证：

```powershell
wsl --version
wsl --set-default-version 2
```

#### 6.4.2 安装 Docker Desktop

运行 `Docker Desktop Installer.exe`。安装完成后启动 Docker Desktop，选择 WSL2 后端并等待 Docker Engine 就绪。

```powershell
docker --version
docker compose version
```

#### 6.4.3 加载离线镜像

```powershell
cd <解压目录>\docker-images

# 先验证全部 tar 文件
Import-Csv .\images.sha256.csv | ForEach-Object {
  $Actual = (Get-FileHash (Join-Path (Get-Location) $_.File) -Algorithm SHA256).Hash
  if ($Actual -ne $_.Hash) { throw "SHA256 mismatch: $($_.File)" }
}

# 逐个加载镜像
docker load -i mariadb.tar
docker load -i redis.tar
docker load -i rabbitmq.tar
docker load -i nginx.tar
docker load -i eclipse-temurin.tar
docker load -i rustfs.tar
docker load -i cict-backend.tar

# 验证镜像列表
docker images
```

#### 6.4.4 启动全部服务

```powershell
cd <解压目录>\cict-docker
docker compose up -d
```

> 离线环境不需要执行 `docker compose build`，镜像已在 6.2 步骤中构建并导入。

#### 6.4.5 验证

同 [5.7 验证](#57-验证)。

### 6.5 离线更新后端

```powershell
# === 联网机器 ===
Copy-Item -Force .\cict-dist-2.1.2-SNAPSHOT.jar C:\cict-docker\cict\app.jar
cd C:\cict-docker
# 将 .env 中 CICT_IMAGE_TAG 改为本次发布的唯一版本号
docker compose build cict
$CictImage = docker compose config --images | Where-Object { $_ -like 'cict/backend:*' } | Select-Object -First 1
docker save -o cict-backend.tar $CictImage
(Get-FileHash cict-backend.tar -Algorithm SHA256).Hash |
  Set-Content cict-backend.tar.sha256
# 将 tar 和 sha256 文件一起拷贝到离线服务器
```

```powershell
# === 离线服务器 ===
$Expected = (Get-Content .\cict-backend.tar.sha256).Trim()
$Actual = (Get-FileHash .\cict-backend.tar -Algorithm SHA256).Hash
if ($Actual -ne $Expected) { throw "SHA256 mismatch: cict-backend.tar" }
docker load -i cict-backend.tar
cd C:\cict-docker
# 将 .env 中 CICT_IMAGE_TAG 更新为联网机器构建时使用的相同值
docker compose up -d cict
docker compose logs -f cict
```

---

## 附录 A：更新流程与运维速查

### A.1 直接安装（第 1-2 章）更新流程

| 更新内容 | 操作步骤 |
|---------|---------|
| 前端代码 | 按 [1.9 部署前后端代码](#19-部署前后端代码) 先解压到临时目录、验证 `index.html`，再用 `rsync --delete` 替换并 reload |
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

### A.3 Docker 部署（第 3-6 章）更新流程

| 更新内容 | 在线环境 | 离线环境 |
|---------|---------|---------|
| 前端代码 | 1. 替换 `nginx/html/` 中的文件<br>2. `docker compose exec nginx nginx -s reload` | 同左 |
| 后端代码 | 1. 覆盖 `cict/app.jar` 并设置唯一 `CICT_IMAGE_TAG`<br>2. `docker compose build cict`<br>3. `docker compose up -d cict`；回滚时把 tag 改回旧版本 | 1. 联网机器用唯一 tag 构建并导出镜像<br>2. 校验后传输到离线服务器<br>3. `docker load -i cict-backend.tar`<br>4. 使用相同 tag 执行 `docker compose up -d cict` |
| RustFS | 1. 明确新版本并修改镜像 tag<br>2. `docker compose pull rustfs`<br>3. 备份后执行 `docker compose up -d rustfs` | 1. 联网机器拉取指定 tag 并导出<br>2. 传输、校验、`docker load -i rustfs.tar`<br>3. 修改相同 tag 后 `docker compose up -d rustfs` |

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
- [Docker Desktop：Windows 安装要求](https://docs.docker.com/desktop/setup/install/windows-install/)
- [Microsoft：安装 WSL](https://learn.microsoft.com/windows/wsl/install)
- [Microsoft：离线安装 WSL](https://learn.microsoft.com/windows/wsl/install#offline-install)
- [RustFS：Docker 安装](https://docs.rustfs.com/en/installation/container/docker)
