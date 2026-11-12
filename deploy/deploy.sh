#!/usr/bin/env bash
# Deploy a reviewed checkout. Never replace the OS Python or change Nginx/firewall.
set -euo pipefail
umask 022
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR=/opt/litegame-server
STATE_DIR=/var/lib/litegame-server
CONFIG=/etc/litegame-server.env
ORIGINS=""
UV_BIN=""
SKIP_PACKAGES=0

usage() {
    cat <<'EOF'
用法：sudo bash deploy/deploy.sh [--origin https://game.example.com] [--uv /path/to/uv] [--skip-packages]
首次安装需要 --origin（可重复）。更新保留配置、用户和数据，直接再次执行即可。
--uv：指定已安装的 uv；sudo 找不到用户目录中的 uv 时使用。
--skip-packages：不安装系统包，需已有 curl、rsync、runuser、useradd 和 CA 证书。
Python 版本由 .python-version 决定，由 uv 自动下载到 /opt/litegame-server/python。
无需安装系统 Python、pip 或 python3-venv，不修改系统默认 python3。
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --origin)
            [[ $# -ge 2 && -n $2 ]] || { usage; exit 1; }
            ORIGINS="${ORIGINS:+${ORIGINS},}$2"
            shift 2 ;;
        --uv)
            [[ $# -ge 2 && -n $2 ]] || { usage; exit 1; }
            UV_BIN="$2"
            shift 2 ;;
        --skip-packages) SKIP_PACKAGES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage; exit 1 ;;
    esac
done
[[ ${EUID} -eq 0 ]] || { echo "请使用 sudo 运行" >&2; exit 1; }
[[ $(uname -s) == Linux ]] || { echo "一键部署需要 Linux + systemd；本地运行使用 uv run server.py。" >&2; exit 1; }
command -v systemctl >/dev/null || { echo "需要 systemd" >&2; exit 1; }
if [[ ! -f $CONFIG && -z $ORIGINS ]]; then
    echo "首次安装请通过 --origin 指定游戏网页来源，例如 https://game.litetools.top" >&2
    exit 1
fi
if [[ -z $UV_BIN ]]; then
    UV_BIN="$(command -v uv || true)"
    if [[ -z $UV_BIN && -x ${HOME}/.local/bin/uv ]]; then
        UV_BIN="${HOME}/.local/bin/uv"
    fi
fi
[[ -n $UV_BIN ]] && UV_BIN="$(command -v "$UV_BIN" || true)"
[[ -n $UV_BIN && -x $UV_BIN ]] || {
    echo '找不到 uv。请使用：sudo bash deploy/deploy.sh --uv "$(command -v uv)" --origin https://游戏网页域名' >&2
    exit 1
}
"$UV_BIN" --version
for file in pyproject.toml uv.lock .python-version; do
    [[ -f $SOURCE_DIR/$file ]] || { echo "源码缺少 $file，请检查 checkout。" >&2; exit 1; }
done
if [[ $SKIP_PACKAGES -eq 0 ]]; then
    command -v apt-get >/dev/null || { echo "非 apt 系统请自行安装依赖后使用 --skip-packages。" >&2; exit 1; }
    # Install only missing tools; do not upgrade/restart unrelated services on each deploy.
    PACKAGES=()
    command -v curl >/dev/null || PACKAGES+=(curl)
    command -v rsync >/dev/null || PACKAGES+=(rsync)
    [[ -s /etc/ssl/certs/ca-certificates.crt ]] || PACKAGES+=(ca-certificates)
    if [[ ${#PACKAGES[@]} -gt 0 ]]; then
        apt-get update
        apt-get install -y "${PACKAGES[@]}"
    fi
fi
for tool in curl rsync runuser useradd; do
    command -v "$tool" >/dev/null || { echo "缺少依赖：$tool" >&2; exit 1; }
done

# Keep managed Python out of /root/.local/share: ProtectHome and the unprivileged
# service must both be able to access it. uv is needed at deploy time only.
export UV_PYTHON_INSTALL_DIR="$APP_DIR/python"
export UV_LINK_MODE=copy
export UV_MANAGED_PYTHON=1
unset UV_PROJECT_ENVIRONMENT UV_PYTHON UV_PYTHON_PREFERENCE VIRTUAL_ENV UV_NO_CONFIG
install -d -m 0755 "$APP_DIR" "$UV_PYTHON_INSTALL_DIR"
PYTHON_VERSION="$(tr -d '[:space:]' < "$SOURCE_DIR/.python-version")"
"$UV_BIN" python install --no-bin "$PYTHON_VERSION"
PYTHON="$("$UV_BIN" python find --system --no-project --managed-python "$PYTHON_VERSION")"
chmod -R a+rX "$UV_PYTHON_INSTALL_DIR"
if [[ -n $ORIGINS ]]; then
    "$PYTHON" - "$ORIGINS" <<'PY'
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
install -d -o litegame -g litegame -m 0700 "$STATE_DIR" "$STATE_DIR/backups"

if systemctl is-active --quiet litegame-server; then
    systemctl stop litegame-server
fi
if [[ -f $STATE_DIR/litegame.sqlite3 ]]; then
    # Standard library backup also works when migrating an old/broken venv.
    BACKUP="$STATE_DIR/backups/pre-update-$(date -u +%Y%m%dT%H%M%SZ)-$$.sqlite3"
    runuser -u litegame -- "$PYTHON" - "$STATE_DIR/litegame.sqlite3" "$BACKUP" <<'PY'
import os
import sqlite3
import sys
from pathlib import Path
os.umask(0o077)
source, target = map(Path, sys.argv[1:])
fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
os.close(fd)
with sqlite3.connect(source.resolve().as_uri() + '?mode=ro', uri=True) as src:
    with sqlite3.connect(target) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise SystemExit('更新前备份完整性检查失败；已中止部署')
print('更新前备份：', target)
PY
fi
if [[ $SOURCE_DIR != "$APP_DIR" ]]; then
    # Explicit allowlist: never copy checkout secrets/data or broadly delete files.
    rsync -a --chown=root:root --exclude='__pycache__' "$SOURCE_DIR/litegame_server/" "$APP_DIR/litegame_server/"
    rsync -a --chown=root:root "$SOURCE_DIR/deploy/" "$APP_DIR/deploy/"
    rsync -a --chown=root:root "$SOURCE_DIR/docs/" "$APP_DIR/docs/"
    for file in server.py manage.py pyproject.toml uv.lock .python-version README.md .env.example; do
        install -m 0644 "$SOURCE_DIR/$file" "$APP_DIR/$file"
    done
fi
"$UV_BIN" sync --project "$APP_DIR" --locked --no-dev --python "$PYTHON" --default-index https://pypi.org/simple
chmod -R a+rX "$APP_DIR/.venv" "$APP_DIR/litegame_server" "$APP_DIR/deploy" "$APP_DIR/docs"
if [[ ! -f $CONFIG ]]; then
    install -o root -g litegame -m 0640 "$APP_DIR/.env.example" "$CONFIG"
    "$PYTHON" - "$CONFIG" "$ORIGINS" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
lines = p.read_text().splitlines()
values = {'LITEGAME_DB': '/var/lib/litegame-server/litegame.sqlite3', 'LITEGAME_CORS_ORIGINS': sys.argv[2]}
p.write_text('\n'.join(key + '=' + values[key] if (key := line.split('=', 1)[0]) in values else line for line in lines) + '\n')
PY
else
    echo "保留已有 ${CONFIG}；--origin 不覆盖已有配置。修改来源请编辑此文件。"
fi
install -m 0755 "$APP_DIR/deploy/litegame-server" /usr/local/bin/litegame-server
install -m 0644 "$APP_DIR/deploy/litegame-server.service" /etc/systemd/system/litegame-server.service
/usr/local/bin/litegame-server init-db
systemctl daemon-reload
systemctl enable litegame-server
systemctl restart litegame-server
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
部署完成，uv 管理的 Python ${PYTHON_VERSION}，API 本机端口 ${PORT}。
创建用户：sudo litegame-server user add "你的用户名"
查看状态：sudo systemctl status litegame-server --no-pager
配置文件：$CONFIG
下一步：按 docs/DEPLOYMENT.md 配置 Nginx HTTPS；脚本不会自动修改现有站点。
EOF
