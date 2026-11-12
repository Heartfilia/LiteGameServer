import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

import pytest

from litegame_server.config import Settings
from litegame_server.db import connect, initialize
from litegame_server.security import hash_password
from manage import create_user

ROOT = Path(__file__).resolve().parent.parent


def test_real_server_start_and_login(tmp_path):
    """Exercise server.py, environment loading, Uvicorn and HTTP outside TestClient."""
    db = tmp_path / "runtime.sqlite3"
    initialize(db)
    with connect(db) as conn:
        create_user(conn, "runtime", hash_password("runtime-test-password"))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {**os.environ, "LITEGAME_DB": str(db), "LITEGAME_HOST": "127.0.0.1",
           "LITEGAME_PORT": str(port), "LITEGAME_ENV_FILE": str(tmp_path / "absent.env")}
    with (tmp_path / "server.log").open("w+") as log:
        proc = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env, stdout=log, stderr=log)
        try:
            base = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    with urlopen(base + "/healthz", timeout=1) as response:
                        assert json.load(response) == {"status": "ok"}
                    break
                except (URLError, ConnectionError):
                    if proc.poll() is not None:
                        log.seek(0)
                        pytest.fail(log.read())
                    time.sleep(0.05)
            else:
                pytest.fail("Server did not become ready")
            request = Request(base + "/api/v1/auth/login", data=json.dumps({
                "username": "runtime", "password": "runtime-test-password"}).encode(),
                headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                token = json.load(response)["access_token"]
            with urlopen(Request(base + "/api/v1/auth/me", headers={"Authorization": "Bearer " + token}), timeout=5) as response:
                assert json.load(response)["username"] == "runtime"
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@pytest.mark.parametrize("origin", ["*", "https://*.example.com", "https://example.com/", "https://example.com/path",
                                   "https://example.com?q=1", "https://user:pass@example.com", "https://bad host"])
def test_invalid_cors_origin_rejected(origin, monkeypatch, tmp_path):
    monkeypatch.setenv("LITEGAME_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setenv("LITEGAME_CORS_ORIGINS", origin)
    with pytest.raises(ValueError, match="Invalid CORS"):
        Settings.load()
