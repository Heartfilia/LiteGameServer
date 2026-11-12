# 部署、命令行与运维

## 1. 部署前准备

- Linux + systemd，推荐 Debian 12+ / Ubuntu 24.04+。
- Python >= 3.11。Ubuntu 22.04 默认的 Python 3.10 不满足要求；不要为了本服务覆盖操作系统 Python，升级系统或自行准备合适的运行环境。
- 源码仓库可正常拉取，服务器能访问系统包仓库与 PyPI。
- 知道游戏站点的真实来源，例如 `https://game.litetools.top`。
- 公网使用准备好一个 API 子域名及 HTTPS 证书，例如 `https://save.example.com`。不必与游戏网页部署在同一台机器。

首次克隆可选择已配好认证的 SSH 地址或 HTTPS 地址：

```bash
git clone https://github.com/Heartfilia/LiteGameServer.git
cd LiteGameServer
sudo bash deploy/deploy.sh --origin https://game.litetools.top
```

**这就是一键安装命令。** 不使用未经检查的 `curl | bash`。脚本需要 root 权限，会执行 apt 安装依赖、创建专用服务账户并启动服务；不会自动配置域名、安装证书、开放防火墙或改动现有 Nginx。

多个游戏站点：

```bash
sudo bash deploy/deploy.sh \
  --origin https://game.litetools.top \
  --origin http://127.0.0.1:8766
```

来源不能包含路径、查询参数或结尾 `/`。`http://localhost:8766` 与 `http://127.0.0.1:8766` 是两个来源，HTTPS 与 HTTP 也不同。

已有依赖或非 apt 系统可执行：

```bash
sudo bash deploy/deploy.sh --skip-packages --origin https://game.example.com
```

仍要求 systemd、Python >=3.11（含 venv 和 pip）、curl、rsync、runuser、useradd。非 Debian/Ubuntu 发行版需要自行验证系统账户工具和路径；自动部署主要以 Debian/Ubuntu 为目标。

### 安装位置

| 路径 | 用途 |
| --- | --- |
| `/opt/litegame-server` | 程序、虚拟环境和文档，不对普通服务账户开放写入 |
| `/etc/litegame-server.env` | 配置，root 所有、litegame 组可读 |
| `/var/lib/litegame-server/litegame.sqlite3` | 数据库 |
| `/var/lib/litegame-server/backups` | 备份，服务账户可写 |
| `/usr/local/bin/litegame-server` | 管理命令入口 |
| `/etc/systemd/system/litegame-server.service` | 服务单元 |

系统账户 `litegame` 是 Linux 进程用户，不是游戏登录用户名，也没有登录 shell。不要将 SQLite 文件放在静态网站目录或 Nginx 可下载目录中。systemd 限制程序只写 `/var/lib/litegame-server`，不建议一键部署后修改数据库到其他路径。

## 2. 创建、管理用户

**没有注册 API。只有有权登录部署机器并运行管理命令的人可以创建用户。**

```bash
# 初始化／迁移；部署脚本已自动执行，可安全重复
sudo litegame-server init-db

# 创建用户；输入和确认密码，输入时终端不会显示字符
sudo litegame-server user add "我的用户名"

# 查看用户 ID、启用状态、显示名、创建时间，不输出密码或哈希
sudo litegame-server user list

# 重置密码；即时撤销此用户全部登录
sudo litegame-server user reset-password "我的用户名"

# 禁用账号；保留数据，即时撤销全部登录
sudo litegame-server user disable "我的用户名"

# 重新启用；需要重新登录，旧会话不会恢复
sudo litegame-server user enable "我的用户名"

# 只撤销登录，不更改密码和账号状态
sudo litegame-server user revoke-sessions "我的用户名"

# 管理员排障时清除所有登录限流计数
sudo litegame-server login-limit-clear
```

用户名：1–64 个字符，支持中文，首尾空白会被去掉，内部不能含空白或控制字符；按 Unicode NFC 规范化、casefold 去重。因此 `Alice` 和 `alice` 是同一个账户。密码：12–1024 个字符，建议使用密码管理器生成的随机密码。

不支持 `--password 明文` 或通过管道创建密码，避免暴露在进程列表和 shell 历史中。需要在交互终端执行；SSH 远程执行时使用交互登录或 `ssh -t`。没有默认用户，没有用户删除命令。用户创建后无需重启服务。

### 本地开发时等价操作

在源码目录激活 `.venv` 后，用 `python3 manage.py` 替换 `sudo litegame-server`：

```bash
python3 manage.py --help
python3 manage.py user --help
python3 manage.py user add "我的用户名"
python3 manage.py backup backups/local.sqlite3
```

## 3. Nginx 与 HTTPS

默认只监听 `127.0.0.1:9494`，不要直接将此端口暴露公网。使用独立 API 子域名最简单。

1. 将 `save.example.com` 的 DNS 指向服务器。
2. 使用自己的证书管理方式（例如 Certbot）签发并配置 HTTPS；证书续期验证按现有环境设置。
3. 参考仓库 `deploy/nginx.conf`，将域名、证书路径和端口改成实际值，再安装至自己的 Nginx `http {}` 配置目录。
4. 检查与重载：

```bash
sudo nginx -t
sudo systemctl reload nginx
curl --fail https://save.example.com/healthz
curl --fail https://save.example.com/api/v1/info
```

不要直接覆盖 `/etc/nginx/nginx.conf`，也不要在没有证书时安装引用不存在证书文件的配置。模板不是自动申请证书脚本。

配置 API 的响应不应被 CDN/反向代理缓存；应用返回 `Cache-Control: no-store`。令牌通过 `Authorization: Bearer ...` 传递，Nginx 默认会转发该请求头。不能在 access log 中额外记录 Authorization 或请求体。

### 与现有 LiteGame 站点同域部署的注意事项

现有 `LiteGame/deploy/nginx.conf` 有 server 层 `GET/HEAD` 限制和 `client_max_body_size 16k`，会拦截登录、上传、OPTIONS 预检以及 NES 大存档。**仅增加 proxy_pass 不够**，必须把静态站点的方法限制移到静态 location，为 API 允许 `GET/POST/PUT/DELETE/OPTIONS` 并设置合适请求体限制。

本服务目前以域名根路径部署：页面输入 `https://save.example.com`，接口为 `/api/v1/...`。不提供任意子路径前缀的自动发现；优先使用独立子域名，避免改动现有站点。

## 4. 配置

一键部署编辑 `/etc/litegame-server.env`；本地编辑项目 `.env`。实际进程环境变量优先。修改后重启：

```bash
sudoedit /etc/litegame-server.env
sudo systemctl restart litegame-server
```

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `LITEGAME_DB` | `data/litegame.sqlite3` | 本地默认数据库；部署脚本设为 `/var/lib/litegame-server/litegame.sqlite3` |
| `LITEGAME_HOST` | `127.0.0.1` | 监听地址 |
| `LITEGAME_PORT` | `9494` | 监听端口；更改后同时修改 Nginx proxy_pass |
| `LITEGAME_CORS_ORIGINS` | 空（示例中有开发来源） | 逗号分隔的确切游戏来源，不能使用 `*` |
| `LITEGAME_SESSION_DAYS` | `30` | 登录有效天数，固定期限、不自动续期 |
| `LITEGAME_MAX_BODY_BYTES` | `16777216` | 整个请求体上限 16 MiB，含 JSON 转义开销 |
| `LITEGAME_MAX_RECORD_BYTES` | `1048576` | 单条普通记录 UTF-8 值上限 1 MiB |
| `LITEGAME_MAX_NES_BYTES` | `12582912` | 单个 NES JSON 存档上限 12 MiB |
| `LITEGAME_USER_QUOTA_BYTES` | `67108864` | 单用户所有存档值合计 64 MiB |
| `LITEGAME_MAX_RECORDS` | `5000` | 单用户记录数量上限，含普通、NES 与删除标记 |
| `LITEGAME_LOGIN_LIMIT` | `10` | 每个窗口每 IP、每用户名的登录尝试上限，成功也计数 |
| `LITEGAME_LOGIN_WINDOW_SECONDS` | `900` | 登录窗口 15 分钟 |
| `LITEGAME_ENV_FILE` | 项目 `.env` | 指定环境文件；部署管理命令与服务固定指向 `/etc/litegame-server.env` |

大小按 UTF-8 字节计算，不是 JavaScript 字符数。外层 JSON 可能比存档值大，客户端要同时遵守请求体限制。删除后不再占用值容量，但保留删除标记以阻止离线旧设备复活数据；服务不会自动清除标记。

登录限流持久化在 SQLite，重启不会重置。默认不信任请求中的 `X-Forwarded-For`，在本机 Nginx 后应用看到的 IP 是 `127.0.0.1`，所以 IP 限额是通过此代理访问的用户共享的。私人少用户场景可用；模板另有基于真实连接地址的 Nginx 登录限流。不要为了绕过限流而盲目信任任意转发头；人数增多时应审查代理边界并调整额度。

同一用户最多保留 20 个有效会话，新增第 21 个会话会撤销最旧的一个。没有公开改密接口或无限期 API key。

## 5. 服务操作与更新

```bash
sudo systemctl status litegame-server --no-pager
sudo systemctl restart litegame-server
sudo systemctl stop litegame-server
sudo systemctl start litegame-server
sudo journalctl -u litegame-server -n 100 --no-pager
sudo journalctl -u litegame-server -f
```

后续更新在**最初的源码 checkout** 中执行：

```bash
git pull --ff-only
sudo bash deploy/deploy.sh
```

更新会短暂停服，先备份再同步程序、安装锁定依赖、初始化／迁移数据库并启动。保留配置与数据；再次传 `--origin` 不会覆盖已有配置，修改来源请编辑配置文件。备份位于 `backups/pre-update-*.sqlite3`，不自动清理。

脚本不执行自动代码回滚。如果更新中网络或安装失败导致服务停止，查看输出、修复依赖问题后重新执行；不要用旧程序强行打开新版本数据库。建议发布前保留源码版本号，并先在测试环境检查。

## 6. 在线备份

运行中的 SQLite 使用 WAL 模式，**不要只复制主数据库文件**。命令使用 SQLite backup API，能在线获得一致性备份：

```bash
sudo litegame-server backup "/var/lib/litegame-server/backups/manual-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
```

目标文件必须不存在；同名文件不会被覆盖。命令使用 `litegame` 系统账户写入，不能写 `/root` 等无权限目录。备份包含用户名、密码哈希、记录和会话哈希，应存放私有目录、加密异地保存，不可发布到代码库或网页目录。

可选：root 的 crontab 每天 03:15 备份（crontab 内 `%` 必须转义）：

```cron
15 3 * * * /usr/local/bin/litegame-server backup /var/lib/litegame-server/backups/daily-$(date -u +\%Y\%m\%dT\%H\%M\%SZ).sqlite3 >>/var/log/litegame-backup.log 2>&1
```

维护者自行设置保留天数、监控磁盘和定期验证恢复；本服务不会自动删除备份。

## 7. 恢复（必须停服）

恢复会覆盖当前数据库。先停止 API，备份当前数据，再确认恢复：

```bash
sudo systemctl stop litegame-server
sudo litegame-server backup "/var/lib/litegame-server/backups/before-restore-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
sudo litegame-server restore /var/lib/litegame-server/backups/需要恢复的备份.sqlite3 --confirm-restore
sudo systemctl start litegame-server
curl --fail http://127.0.0.1:9494/healthz
```

恢复来源必须由 `litegame` 系统账户读取。工具检查完整性与数据库版本，使用 SQLite backup API 恢复，不手工删除 WAL 文件。工具无法证明所有外部进程均已停止，`--confirm-restore` 是管理员对停服和覆盖的确认，不能在运行中使用。

**恢复会撤销备份中的所有登录并清除限流计数。** 用户需要重新登录，客户端必须按接入文档进行全量核对，而不是继续沿用恢复前的版本和游标。恢复可能让修订号回退；不要把旧本地数据自动批量覆盖回去。

## 8. 常见问题

| 现象 | 检查与处理 |
| --- | --- |
| Python 版本过低 | `python3 --version`；要求 >=3.11，推荐 Debian 12 / Ubuntu 24.04 |
| 外网连不上 9494 | 默认设计如此，通过 HTTPS Nginx 访问，而不是开放 API 端口 |
| Nginx 502 | 检查 `systemctl status`、journal、端口、proxy_pass |
| 浏览器 CORS 报错 | 确认白名单是游戏网页来源，不是 API 来源；匹配协议、域名和端口；重启服务 |
| HTTPS 页面无法连接 HTTP API | 浏览器混合内容限制，给 API 配 HTTPS；不要尝试前端绕过 |
| 401 | 检查用户名密码、禁用状态、过期/撤销令牌，重新登录 |
| 413 | 检查 Nginx 请求体限制、单条限制、外层 JSON 大小、账户容量与记录数 |
| 409 | 正常的并发冲突；由前端提示选择版本，不循环强推 |
| 422 | 请求字段/类型/存储键不符合协议；不会回显密码输入 |
| 429 | 等待 `Retry-After`；必要时管理员运行 `login-limit-clear`，Nginx 的独立限流仍可能生效 |
| permission denied / unable to open database | 确认目录归 litegame、配置可读；不要使用 root 手动创建不可读数据库；不要把 DB 移出 systemd 允许目录 |
| 接口正常但游戏不同步 | 当前前端必须先按 CLIENT_INTEGRATION.md 接入，不是部署即自动同步 |

SQLite 面向单机、小用户量。在多台机器之间不要共享 SQLite 文件，不要放 NFS；不要擅自增加多实例部署。默认一个 Uvicorn 进程，写事务使用 SQLite 锁保证版本检查与提交原子性。
