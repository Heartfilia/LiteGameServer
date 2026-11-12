# LiteGameServer

为 [LiteGame](https://github.com/Heartfilia/LiteGame) 提供私有游戏记录、收藏与 FC / NES 存档同步的 API。采用 **uv / Python / FastAPI / SQLite**，无需 MySQL、Redis 或 Node.js。

- **只有服务器命令行可以创建用户**，没有公开注册接口。
- 用户名自定义，密码使用 scrypt 哈希；用户之间的数据隔离。
- 普通记录按原始 `localStorage` 键值保存；NES 即时存档使用独立接口。
- 逐条版本校验、原子批量写入、增量拉取、删除标记，避免多设备静默覆盖。
- SQLite 在线备份、账号禁用、密码重置、会话撤销。

> 本仓库仅实现服务端。`~/code/JScode/LiteGame` 尚需按 [前端接入文档](docs/CLIENT_INTEGRATION.md) 新增页面入口及同步模块。部署 API **不会自动修改前端或让现有游戏开始同步**。

## 一键部署（Debian / Ubuntu + systemd）

支持 Debian / Ubuntu + systemd（包括系统 Python 为 3.10 的 Ubuntu 22.04）。服务器已安装 `uv` 即可，**不需要升级系统 Python**。在源码目录执行：

```bash
sudo bash deploy/deploy.sh --uv "$(command -v uv)" --origin https://game.litetools.top
```

`--origin` 是**游戏网页的来源**（协议 + 域名 + 可选端口），不是 API 地址；替换为自己的实际站点。需要多个来源可重复指定。首次安装必须指定，更新时保留已有配置。

`--uv` 让 sudo 能找到当前用户已安装的 uv。脚本由 uv 下载 `.python-version` 指定的 Python（当前 3.13），使用 `uv.lock` 同步生产依赖，配置 systemd、管理命令并检查健康状态。仅在缺少 curl/rsync/CA 时安装对应系统包，不安装 pip 或系统 Python，不添加 PPA。默认 API 只监听 `127.0.0.1:9494`，**不会擅自修改现有 Nginx、证书或防火墙**。

随后创建账户（交互输入至少 4 个字符的密码，公网建议使用更长的随机密码）：

```bash
sudo litegame-server user add "你的用户名"
```

检查服务：

```bash
curl http://127.0.0.1:9494/healthz
sudo systemctl status litegame-server --no-pager
```

按 [部署与运维文档](docs/DEPLOYMENT.md) 配置 API 子域名的 Nginx HTTPS。页面首次连接时输入服务器地址，再使用已创建的用户名、密码登录；以后在会话有效期内自动连接。

## 本地运行（macOS / Linux）

```bash
uv sync --locked
cp .env.example .env
uv run manage.py init-db
uv run manage.py user add "你的用户名"
uv run server.py
```

- 服务信息：`http://127.0.0.1:9494/api/v1/info`
- 健康检查：`http://127.0.0.1:9494/healthz`
- 交互式接口文档：`http://127.0.0.1:9494/docs`
- OpenAPI：`http://127.0.0.1:9494/openapi.json`

本地管理命令在项目目录使用 `uv run`，无需手动激活虚拟环境。首次运行会自动准备 Python 和依赖。`.env` 相对项目目录加载，实际环境变量优先；已有 `.env` 不要重复用 `cp` 覆盖。

## 用户管理速查

一键部署后，在服务器任意目录执行：

```bash
sudo litegame-server user add "用户名"
sudo litegame-server user list
sudo litegame-server user reset-password "用户名"
sudo litegame-server user disable "用户名"
sudo litegame-server user enable "用户名"
sudo litegame-server user revoke-sessions "用户名"
sudo litegame-server login-limit-clear
```

本地运行时，等价命令为 `uv run manage.py user ...`。用户名支持中文、不区分大小写（保留显示写法），不能包含空白或控制字符；没有默认用户或默认密码。禁用、重置密码、撤销会话均即时阻止旧凭据访问；重新启用不会恢复旧会话。没有删除用户命令，防止误删存档。

## 更新与备份

```bash
# 在服务器上的源码 checkout 中拉取后重新部署（不是 /opt 发布目录）
git pull --ff-only
sudo bash deploy/deploy.sh --uv "$(command -v uv)"

# 在线备份，文件名必须尚不存在
sudo litegame-server backup /var/lib/litegame-server/backups/manual-20261112.sqlite3
```

更新保留 `/etc/litegame-server.env` 和 `/var/lib/litegame-server/` 数据，并在存在数据库时生成更新前备份。恢复、定时备份、权限与故障排查见 [部署文档](docs/DEPLOYMENT.md)。不要直接复制正在使用的 `.sqlite3` 文件，WAL 中可能还有未合并的数据。

## 文档

| 文档 | 内容 |
| --- | --- |
| [前端接入协议](docs/CLIENT_INTEGRATION.md) | 给 LiteGame 前端工程/代码助手阅读，含所有接口、示例、同步算法、冲突和 NES 接入 |
| [部署与命令行操作](docs/DEPLOYMENT.md) | 一键部署、HTTPS、全部管理命令、更新、备份恢复、配置和排障 |

## 开发与测试

```bash
uv sync --locked
uv run --no-sync python -m pytest -q
bash -n deploy/deploy.sh deploy/litegame-server
```

`pyproject.toml` 定义项目及开发依赖，`uv.lock` 锁定完整依赖与校验哈希，`.python-version` 指定默认 Python。更新依赖用 `uv lock --upgrade`，随后 `uv sync --locked` 并重新测试。部署使用 `uv sync --locked --no-dev`；运行中的服务直接使用 uv 准备好的解释器，不会联网解析依赖。仓库 CI 覆盖 Python 3.11 / 3.13。

## 安全边界

这不是防作弊后端：存档由浏览器产生，不能用于可信竞技排名。公网必须使用 HTTPS。CORS 白名单只约束浏览器，不代替认证。令牌属于敏感凭据，不能放 URL、日志或游戏备份；SQLite 备份同样应作为敏感文件保管。轻量部署面向少量私人用户，不建议直接扩展为公开大规模服务。
