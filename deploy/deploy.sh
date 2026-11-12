#!/usr/bin/env bash
# Run from a reviewed checkout. Does not modify Nginx, firewall or certificates.
set -euo pipefail
umask 027
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/litegame-server
STATE_DIR=/var/lib/litegame-server
CONFIG=/etc/litegame-server.env
ORIGINS=""
SKIP_PACKAGES=0

usage() {
    cat <<'EOF'
用法：sudo bash deploy/deploy.sh [--origin https://game.example.com] [--skip-packages]
首次安装需要 --origin（可重复）。后续更新保留配置和数据，直接再次执行即可。
--skip-packages：跳过 apt，需自行提供 Python >=3.11、venv、pip、curl、rsync。
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --origin)
            [[ $# -ge 2 ]] || { usage; exit 1; }
            ORIGINS="${ORIGINS:+${ORIGINS},}$2"
            shift 2 ;;
        --skip-packages) SKIP_PACKAGES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage; exit 1 ;;
    esac
done
[[ ${EUID} -eq 0 ]] || { echo "请使用 sudo 运行" >&2; exit 1; }
[[ $(uname -s) == Linux ]] || { echo "一键部署仅支持 Linux + systemd；其他系统见 README 本地启动" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "需要 systemd" >&2; exit 1; }
if [[ ! -f $CONFIG && -z $ORIGINS ]]; then
    echo "首次安装请通过 --origin 指定游戏站点来源，例如 https://game.litetools.top" >&2
    exit 1
fi
if [[ $SKIP_PACKAGES -eq 0 ]]; then
    command -v apt-get >/dev/null || { echo "非 Debian/Ubuntu 系统请自行安装依赖并使用 --skip-packages" >&2; exit 1; }
    apt-get update
    apt-get install -y python3 python3-venv python3-pip curl rsync ca-certificates
fi
for tool in python3 curl rsync runuser useradd; do
    command -v "$tool" >/dev/null || { echo "缺少依赖：$tool" >&2; exit 1; }
done
python3 -c 'import sys; assert sys.version_info >= (3,11), "需要 Python 3.11+（推荐 Debian 12/Ubuntu 24.04+）"'
if [[ -n $ORIGINS ]]; then
    python3 - "$ORIGINS" <<'PY'
import sys
from urllib.parse import urlsplit
for origin in sys.argv[1].split(','):
    p = urlsplit(origin)
    if (p.scheme not in ('https', 'http') or not p.netloc or p.path or p.query
            or p.fragment or p.username or p.password or '*' in origin or any(c.isspace() for c in origin)):
        raise SystemExit('无效来源（不能含路径、结尾斜杠或空白）：' + origin)
PY
fi
id litegame >/dev/null 2>&1 || useradd --system --user-group --home-dir "$STATE_DIR" --shell /usr/sbin/nologin litegame
install -d -m 0755 "$APP_DIR"
install -d -o litegame -g litegame -m 0700 "$STATE_DIR" "$STATE_DIR/backups"

# Stop only our own service before updating code/dependencies.
if systemctl is-active --quiet litegame-server; then
    systemctl stop litegame-server
fi
if [[ -x $APP_DIR/.venv/bin/python && -f $CONFIG && -f $APP_DIR/manage.py ]]; then
    BACKUP="$STATE_DIR/backups/pre-update-$(date -u +%Y%m%dT%H%M%SZ)-$$.sqlite3"
    if [[ -f $STATE_DIR/litegame.sqlite3 ]]; then
        (cd "$APP_DIR" && runuser -u litegame -- env LITEGAME_ENV_FILE="$CONFIG" \
            "$APP_DIR/.venv/bin/python" "$APP_DIR/manage.py" backup "$BACKUP")
    fi
fi
if [[ $SOURCE_DIR != "$APP_DIR" ]]; then
    # Explicit allowlist, no broad --delete and no copying checkout secrets/data.
    rsync -a --chown=root:root --exclude='__pycache__' "$SOURCE_DIR/litegame_server/" "$APP_DIR/litegame_server/"
    rsync -a --chown=root:root "$SOURCE_DIR/deploy/" "$APP_DIR/deploy/"
    rsync -a --chown=root:root "$SOURCE_DIR/docs/" "$APP_DIR/docs/"
    for file in server.py manage.py requirements.txt requirements.lock README.md .env.example; do
        install -m 0644 "$SOURCE_DIR/$file" "$APP_DIR/$file"
    done
fi
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -r "$APP_DIR/requirements.lock"
# root's umask must not prevent the unprivileged service from reading its venv.
chmod -R a+rX "$APP_DIR/.venv" "$APP_DIR/litegame_server" "$APP_DIR/deploy" "$APP_DIR/docs"
if [[ ! -f $CONFIG ]]; then
    install -o root -g litegame -m 0640 "$APP_DIR/.env.example" "$CONFIG"
    python3 - "$CONFIG" "$ORIGINS" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
lines = p.read_text().splitlines()
values = {'LITEGAME_DB': '/var/lib/litegame-server/litegame.sqlite3', 'LITEGAME_CORS_ORIGINS': sys.argv[2]}
p.write_text('\n'.join(key + '=' + values[key] if (key := line.split('=', 1)[0]) in values else line for line in lines) + '\n')
PY
else
    echo "保留已有 $CONFIG；--origin 不覆盖已有配置。修改来源请编辑此文件。"
fi
install -m 0755 "$APP_DIR/deploy/litegame-server" /usr/local/bin/litegame-server
install -m 0644 "$APP_DIR/deploy/litegame-server.service" /etc/systemd/system/litegame-server.service
/usr/local/bin/litegame-server init-db
systemctl daemon-reload
systemctl enable litegame-server
systemctl restart litegame-server
# Read the configured port, including custom values retained on updates.
PORT="$(cd "$APP_DIR" && runuser -u litegame -- env LITEGAME_ENV_FILE="$CONFIG" \
    "$APP_DIR/.venv/bin/python" -c 'from litegame_server.config import Settings; print(Settings.load().port)')"
READY=0
for attempt in {1..20}; do
    if curl --noproxy '*' --max-time 2 --fail --silent "http://127.0.0.1:$PORT/healthz" >/dev/null; then
        READY=1
        break
    fi
    sleep 1
done
if [[ $READY -ne 1 ]]; then
    echo "服务健康检查失败，查看：sudo journalctl -u litegame-server -n 80 --no-pager" >&2
    exit 1
fi
cat <<EOF
部署完成，API 监听本机端口 $PORT。
创建用户：sudo litegame-server user add "你的用户名"
查看状态：sudo systemctl status litegame-server --no-pager
配置文件：$CONFIG
下一步：按 docs/DEPLOYMENT.md 配置 Nginx HTTPS；脚本不会自动修改现有站点。
EOF
