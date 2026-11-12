import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from litegame_server.app import create_app
from litegame_server.config import Settings
from litegame_server.db import connect, initialize
from litegame_server.security import hash_password, token_hash
from manage import backup_database, create_user, main, restore_database, update_user

PASSWORD = "test-password-123456"
SHA = "a" * 64


@pytest.fixture
def setup(tmp_path):
    settings = Settings(db_path=tmp_path / "test.sqlite3", cors_origins=("https://game.example.com",),
                        login_limit=1000)
    initialize(settings.db_path)
    with connect(settings.db_path) as conn:
        for name in ("Alice", "Bob", "中文用户"):
            create_user(conn, name, hash_password(PASSWORD))
    return settings


@pytest.fixture
def client(setup):
    with TestClient(create_app(setup)) as client:
        yield client


def login(client, name="Alice"):
    response = client.post("/api/v1/auth/login", json={"username": name, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def change(key="litegame.test", value="hello", version=0):
    return {"key": key, "value": value, "expected_version": version}


def batch(client, auth, *changes):
    return client.post("/api/v1/records/batch", headers=auth, json={"changes": list(changes)})


def snapshot():
    return {"format": 1, "emulator": "jsnes-2.1.0", "sha256": SHA, "title": "测试游戏",
            "savedAt": "2026-11-12T00:00:00Z", "state": {k: {} for k in ("cpu", "ppu", "mmap", "papu")}}


def test_info_health_no_registration(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    info = client.get("/api/v1/info")
    assert info.json()["registration"] == "cli-only"
    assert info.headers["cache-control"] == "no-store"
    assert client.post("/api/v1/auth/register", json={}).status_code == 404
    assert client.get("/api/v1/records").status_code == 401
    assert client.get("/openapi.json").status_code == 200


def test_login_logout_and_password_not_echoed(client, setup):
    auth = login(client, "alice")
    assert client.get("/api/v1/auth/me", headers=auth).json()["username"] == "Alice"
    with connect(setup.db_path) as conn:
        stored = conn.execute("SELECT token_hash FROM sessions").fetchone()[0]
        assert stored == token_hash(auth["Authorization"][7:])
        assert stored != auth["Authorization"][7:]
    assert client.post("/api/v1/auth/logout", headers=auth).json() == {"ok": True}
    assert client.get("/api/v1/auth/me", headers=auth).status_code == 401
    for name in ("Alice", "missing"):
        response = client.post("/api/v1/auth/login", json={"username": name, "password": "bad"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_credentials"
    response = client.post("/api/v1/auth/login", json={"username": "Alice", "password": 123456})
    assert response.status_code == 422
    assert "123456" not in response.text
    assert client.get("/api/v1/auth/me", headers=login(client, "中文用户")).status_code == 200


def test_user_isolation_and_atomic_conflicts(client):
    a, b = login(client), login(client, "Bob")
    response = batch(client, a, change(value='{"score":42}'))
    assert response.status_code == 200
    assert response.json()["records"][0]["version"] == 1
    assert client.get("/api/v1/records", headers=b).json()["records"] == []
    assert batch(client, b, change(value="Bob data")).status_code == 200
    conflict = batch(client, a, change(value="stale"), change("litegame.other"))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["conflicts"][0]["current"]["value"] == '{"score":42}'
    records = client.get("/api/v1/records", headers=a).json()["records"]
    assert len(records) == 1
    assert batch(client, a, change(value="new", version=1)).status_code == 200


def test_deletion_resurrection_and_paging(client):
    auth = login(client)
    batch(client, auth, change(), change("chogan.test", ""))
    first = client.get("/api/v1/records?limit=1", headers=auth).json()
    assert first["has_more"] and first["next_cursor"] == 1
    second = client.get(f"/api/v1/records?since={first['next_cursor']}&limit=1", headers=auth).json()
    assert not second["has_more"] and second["records"][0]["value"] == ""
    deleted = batch(client, auth, change(value=None, version=1)).json()["records"][0]
    assert deleted["deleted"] and deleted["value"] is None and deleted["version"] == 3
    assert batch(client, auth, change(version=0)).status_code == 409
    assert batch(client, auth, change(version=3)).status_code == 200
    assert client.get("/api/v1/records?since=999", headers=auth).json()["error"]["code"] == "cursor_invalid"


@pytest.mark.parametrize("key", ["unrelated", "litegame.", "litegame.sync.token", "litegame.auth", "litegame.server.url", "chogan.bad\nkey"])
def test_reserved_keys(client, key):
    assert batch(client, login(client), change(key)).status_code == 422


def test_strict_validation(client):
    auth = login(client)
    assert batch(client, auth, change(), change()).status_code == 422
    assert batch(client, auth, change(value={"wrong": "type"})).status_code == 422
    assert batch(client, auth, change(version=True)).status_code == 422
    assert batch(client, auth, change(version=-1)).status_code == 422
    assert client.post("/api/v1/records/batch", headers=auth, json={"changes": []}).status_code == 422


def test_nes_roundtrip_conflict_delete_and_isolation(client):
    auth, other = login(client), login(client, "Bob")
    path = f"/api/v1/nes/{SHA}"
    payload = {"expected_version": 0, "snapshot": snapshot()}
    assert client.put(path, headers=auth, json=payload).status_code == 200
    assert client.get(path, headers=auth).json()["snapshot"] == snapshot()
    assert client.get(path, headers=other).status_code == 404
    records = client.get("/api/v1/records", headers=auth).json()["records"]
    assert records[0]["kind"] == "nes" and "value" not in records[0]
    assert client.put(path, headers=auth, json=payload).status_code == 409
    assert client.delete(path + "?expected_version=1", headers=auth).status_code == 200
    deleted = client.get(path, headers=auth).json()
    assert deleted["deleted"] and deleted["snapshot"] is None
    payload["expected_version"] = 2
    assert client.put(path, headers=auth, json=payload).status_code == 200
    payload["snapshot"]["sha256"] = "b" * 64
    assert client.put(path, headers=auth, json=payload).status_code == 422
    assert client.get("/api/v1/nes/not-a-hash", headers=auth).status_code == 422


def test_disabled_reset_expired_and_revoked_sessions(client, setup):
    auth = login(client)
    with connect(setup.db_path) as conn:
        update_user(conn, "Alice", "disable")
    assert batch(client, auth, change()).status_code == 401
    assert client.post("/api/v1/auth/login", json={"username": "Alice", "password": PASSWORD}).status_code == 401
    with connect(setup.db_path) as conn:
        update_user(conn, "Alice", "enable")
    assert client.get("/api/v1/auth/me", headers=auth).status_code == 401
    auth = login(client)
    with connect(setup.db_path) as conn:
        update_user(conn, "Alice", "reset-password", hash_password(PASSWORD))
    assert client.get("/api/v1/auth/me", headers=auth).status_code == 401
    auth = login(client)
    with connect(setup.db_path) as conn:
        conn.execute("UPDATE sessions SET expires_at=?", (int(time.time()) - 1,))
    assert client.get("/api/v1/auth/me", headers=auth).status_code == 401


def test_limits_and_quota_atomicity(setup):
    settings = replace(setup, max_record_bytes=8, user_quota_bytes=12, max_records=2, max_nes_bytes=10)
    with TestClient(create_app(settings)) as client:
        auth = login(client)
        assert batch(client, auth, change(value="中" * 3)).json()["error"]["code"] == "record_too_large"
        assert batch(client, auth, change(value="12345678")).status_code == 200
        response = batch(client, auth, change("litegame.other", "12345"))
        assert response.json()["error"]["code"] == "quota_exceeded"
        assert len(client.get("/api/v1/records", headers=auth).json()["records"]) == 1
        assert batch(client, auth, change(value=None, version=1)).status_code == 200
        assert batch(client, auth, change("litegame.other", "12345")).status_code == 200
        assert batch(client, auth, change("litegame.third", "")).status_code == 413
        assert client.put(f"/api/v1/nes/{SHA}", headers=auth,
                          json={"snapshot": snapshot(), "expected_version": 0}).status_code == 413


def test_body_limit_cors_and_chunked(setup):
    with TestClient(create_app(replace(setup, max_body_bytes=100))) as client:
        headers = {"Origin": "https://game.example.com"}
        response = client.post("/api/v1/auth/login", content="x" * 101, headers=headers)
        assert response.status_code == 413
        assert response.headers["access-control-allow-origin"] == headers["Origin"]
        assert response.headers["cache-control"] == "no-store"
        assert "Retry-After" in response.headers["access-control-expose-headers"]
        assert client.post("/api/v1/auth/login", content=iter([b"x" * 60, b"y" * 60])).status_code == 413
        response = client.options("/api/v1/records/batch", headers={
            **headers, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type"})
        assert response.status_code == 200
        response = client.get("/api/v1/info", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in response.headers


def test_login_rate_limit_persistent(setup):
    settings = replace(setup, login_limit=2)
    for expected in (401, 401, 429):
        # Recreating the app must not reset rate limiting stored in SQLite.
        with TestClient(create_app(settings)) as client:
            response = client.post("/api/v1/auth/login", json={"username": "Alice", "password": "wrong"})
            assert response.status_code == expected
            if expected == 429:
                assert int(response.headers["retry-after"]) > 0


def test_concurrent_writes_only_one_wins(client):
    auth = login(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda value: batch(client, auth, change(value=value)).status_code, ("A", "B")))
    assert sorted(results) == [200, 409]


def test_restart_backup_restore(client, setup, tmp_path):
    auth = login(client)
    batch(client, auth, change(value="before backup"))
    backup = backup_database(setup.db_path, tmp_path / "backup.sqlite3")
    with pytest.raises(FileExistsError):
        backup_database(setup.db_path, backup)
    batch(client, auth, change(value="after backup", version=1))
    with TestClient(create_app(setup)) as restarted:
        assert restarted.get("/api/v1/records", headers=auth).json()["records"][0]["value"] == "after backup"
    restored = tmp_path / "restored.sqlite3"
    restore_database(backup, restored)
    with TestClient(create_app(replace(setup, db_path=restored))) as restored_client:
        assert restored_client.get("/api/v1/auth/me", headers=auth).status_code == 401
        auth2 = login(restored_client)
        assert restored_client.get("/api/v1/records", headers=auth2).json()["records"][0]["value"] == "before backup"


def test_cli_user_operations(setup, monkeypatch, capsys):
    monkeypatch.setenv("LITEGAME_DB", str(setup.db_path))
    monkeypatch.setattr("manage.read_password", lambda: hash_password(PASSWORD))
    assert main(["init-db"]) == 0
    assert main(["user", "add", "自定义用户"]) == 0
    assert main(["user", "add", "ALICE"]) == 1
    assert main(["user", "list"]) == 0
    output = capsys.readouterr().out
    assert "自定义用户" in output and PASSWORD not in output and "scrypt$" not in output
    for action in ("reset-password", "disable", "enable", "revoke-sessions"):
        assert main(["user", action, "自定义用户"]) == 0
    assert main(["user", "disable", "不存在"]) == 1
    assert main(["restore", "/missing"]) == 1
    assert main(["login-limit-clear"]) == 0


def test_database_newer_version_refused(tmp_path):
    path = tmp_path / "newer.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=999")
    with pytest.raises(RuntimeError, match="newer"):
        initialize(path)


def test_session_count_is_bounded(client, setup):
    oldest = login(client)
    for _ in range(20):
        newest = login(client)
    assert client.get("/api/v1/auth/me", headers=oldest).status_code == 401
    assert client.get("/api/v1/auth/me", headers=newest).status_code == 200
    with connect(setup.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 20


def test_restore_over_existing_database(setup, tmp_path):
    backup = backup_database(setup.db_path, tmp_path / "original.sqlite3")
    with connect(setup.db_path) as conn:
        create_user(conn, "after-backup", hash_password(PASSWORD))
    restore_database(backup, setup.db_path)
    with connect(setup.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_write_receipt_does_not_skip_other_device_changes(client):
    auth = login(client)
    batch(client, auth, change("litegame.device-a", "a"))
    batch(client, auth, change("litegame.device-b", "b"))
    receipt = batch(client, auth, change("litegame.device-a", "a2", 1)).json()
    assert receipt["revision"] == 3 and "next_cursor" not in receipt
    pulled = client.get("/api/v1/records?since=1", headers=auth).json()
    assert [row["key"] for row in pulled["records"]] == ["litegame.device-b", "litegame.device-a"]
