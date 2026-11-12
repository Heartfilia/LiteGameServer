import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def executable(path, text):
    path.write_text("#!/usr/bin/env bash\nset -euo pipefail\n" + text)
    path.chmod(0o755)


def test_deploy_help():
    result = subprocess.run(["bash", str(ROOT / "deploy/deploy.sh"), "--help"], capture_output=True, text=True)
    assert result.returncode == 0
    assert "--uv" in result.stdout and "--python" not in result.stdout


def test_deploy_uv_workflow_without_system_changes(tmp_path):
    """Run installer control flow with stubbed system tools, never touching /opt/etc.

    Complements real uv sync/API tests; does not claim systemd integration coverage.
    """
    source = tmp_path / "source"
    source.mkdir()
    for name in ("deploy", "litegame_server", "docs"):
        shutil.copytree(ROOT / name, source / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("server.py", "manage.py", "pyproject.toml", "uv.lock", ".python-version", "README.md", ".env.example"):
        shutil.copyfile(ROOT / name, source / name)
    app = tmp_path / "app"
    state = tmp_path / "state"
    config = tmp_path / "config.env"
    system = tmp_path / "system"
    (system / "bin").mkdir(parents=True)
    (system / "units").mkdir()
    script = source / "deploy/deploy.sh"
    text = script.read_text()
    text = text.replace("APP_DIR=/opt/litegame-server", f'APP_DIR="{app}"')
    text = text.replace("STATE_DIR=/var/lib/litegame-server", f'STATE_DIR="{state}"')
    text = text.replace("CONFIG=/etc/litegame-server.env", f'CONFIG="{config}"')
    text = text.replace("/usr/local/bin/litegame-server", str(system / "bin/litegame-server"))
    text = text.replace("/etc/systemd/system/litegame-server.service", str(system / "units/litegame-server.service"))
    text = text.replace("[[ ${EUID} -eq 0 ]]", "true")
    script.write_text(text)
    tools = tmp_path / "tools"
    tools.mkdir()
    log = tmp_path / "calls.log"
    env = {**os.environ, "PATH": str(tools) + ":" + os.environ["PATH"],
           "CALLS": str(log), "TEST_APP": str(app)}
    executable(tools / "uname", 'echo Linux\n')
    executable(tools / "id", 'exit 0\n')
    executable(tools / "useradd", 'exit 0\n')
    executable(tools / "runuser", """printf 'runuser %s\n' "$*" >> "$CALLS"
if [[ $* == *'print(Settings.load().port)'* ]]; then echo 9494; fi
""")
    executable(tools / "systemctl", """printf 'systemctl %s\n' "$*" >> "$CALLS"
if [[ ${1:-} == is-active ]]; then exit 1; fi
""")
    executable(tools / "curl", """printf 'curl %s\n' "$*" >> "$CALLS"
""")
    executable(tools / "rsync", """args=("$@")
n=${#args[@]}
mkdir -p "${args[n-1]}"
cp -R "${args[n-2]}." "${args[n-1]}"
""")
    executable(tools / "install", """if [[ ${1:-} == -d ]]; then
    while [[ $# -gt 0 && $1 == -* ]]; do
        case "$1" in -d) shift;; -m|-o|-g) shift 2;; esac
    done
    mkdir -p "$@"
else
    while [[ $# -gt 0 && $1 == -* ]]; do shift 2; done
    if [[ $2 == */system/bin/litegame-server ]]; then
        printf '#!/usr/bin/env bash\nexit 0\n' > "$2"
        chmod +x "$2"
    else
        cp "$1" "$2"
    fi
fi
""")
    executable(tools / "managed-python", """# Simulate origin validation and config generation.
if [[ $# -ge 3 && $2 == *.env ]]; then
    sed "s|^LITEGAME_CORS_ORIGINS=.*|LITEGAME_CORS_ORIGINS=$3|" "$2" > "$2.new"
    mv "$2.new" "$2"
fi
cat >/dev/null
""")
    executable(tools / "uv", """printf 'uv %s\n' "$*" >> "$CALLS"
case "${1:-}" in
    --version) echo 'uv 0.10.9';;
    python)
        if [[ $2 == find ]]; then echo "$(dirname "$0")/managed-python"; fi;;
    sync)
        mkdir -p "$TEST_APP/.venv/bin"
        touch "$TEST_APP/.venv/bin/python";;
esac
""")
    command = ["bash", str(script), "--uv", str(tools / "uv"), "--skip-packages", "--origin", "https://game.example.com"]
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text()
    assert "python install --no-bin 3.13" in calls
    assert "python find --system --no-project --managed-python 3.13" in calls
    assert f"sync --project {app} --locked --no-dev --python" in calls
    assert "systemctl restart litegame-server" in calls
    assert "http://127.0.0.1:9494/healthz" in calls
    assert "LITEGAME_CORS_ORIGINS=https://game.example.com" in config.read_text()
    assert (app / "uv.lock").is_file()
    assert not (app / "requirements.txt").exists()
    # Updates must not overwrite administrators' existing configuration.
    config.write_text("LITEGAME_PORT=12345\nLITEGAME_CORS_ORIGINS=https://kept.example.com\n")
    (state / "litegame.sqlite3").touch()  # Backup invocation is stubbed, no real DB required.
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "https://kept.example.com" in config.read_text()
    assert "pre-update-" in log.read_text()
