import json

from fastapi import HTTPException

from .db import connect
from .security import utcnow


def fail(status: int, code: str, message: str, **extra):
    raise HTTPException(status, {"code": code, "message": message, **extra})


def public_record(row, include_value=True):
    result = {key: row[key] for key in ("kind", "key", "version", "updated_at")}
    result["deleted"] = bool(row["deleted"])
    result["value_bytes"] = row["value_bytes"]
    if include_value:
        result["value"] = row["value"]
    return result


def require_session(conn, hashed_token):
    import time

    user = conn.execute(
        "SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id "
        "WHERE s.token_hash=? AND s.expires_at>? AND u.enabled=1",
        (hashed_token, int(time.time())),
    ).fetchone()
    if not user:
        fail(401, "unauthorized", "Login required or session expired")
    return user


def write_records(settings, hashed_token, kind, changes):
    """A batch either entirely commits or entirely conflicts, under a write lock."""
    with connect(settings.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        user = require_session(conn, hashed_token)
        current = {}
        conflicts = []
        for change in changes:
            row = conn.execute(
                "SELECT * FROM records WHERE user_id=? AND kind=? AND key=?",
                (user["id"], kind, change["key"]),
            ).fetchone()
            current[change["key"]] = row
            if change["expected_version"] != (row["version"] if row else 0):
                # NES conflicts include only metadata; fetch its snapshot separately.
                conflicts.append({"key": change["key"], "current": public_record(row, kind == "local") if row else None})
        if conflicts:
            fail(409, "version_conflict", "No changes were applied", conflicts=conflicts)
        usage = conn.execute(
            "SELECT COALESCE(SUM(value_bytes),0), COUNT(*) FROM records WHERE user_id=?", (user["id"],)
        ).fetchone()
        size, count = usage
        for change in changes:
            value = change["value"]
            length = len(value.encode("utf-8")) if value is not None else 0
            limit = settings.max_record_bytes if kind == "local" else settings.max_nes_bytes
            if length > limit:
                fail(413, "record_too_large", "Record exceeds byte limit", key=change["key"], limit=limit)
            old = current[change["key"]]
            size += length - (old["value_bytes"] if old else 0)
            count += int(old is None)
        if size > settings.user_quota_bytes or count > settings.max_records:
            fail(413, "quota_exceeded", "Account storage or record count limit exceeded")
        revision = user["revision"]
        results = []
        for change in changes:
            revision += 1
            value = change["value"]
            conn.execute(
                "INSERT INTO records(user_id,kind,key,value,value_bytes,version,deleted,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,kind,key) DO UPDATE SET "
                "value=excluded.value,value_bytes=excluded.value_bytes,version=excluded.version,"
                "deleted=excluded.deleted,updated_at=excluded.updated_at",
                (user["id"], kind, change["key"], value, len(value.encode("utf-8")) if value is not None else 0,
                 revision, int(value is None), utcnow()),
            )
            row = conn.execute("SELECT * FROM records WHERE user_id=? AND kind=? AND key=?",
                               (user["id"], kind, change["key"])).fetchone()
            results.append(public_record(row, kind == "local"))
        conn.execute("UPDATE users SET revision=? WHERE id=?", (revision, user["id"]))
        conn.commit()
        # This is a write receipt, NOT a pull cursor: other device changes may precede it.
        return {"records": results, "revision": revision}


def list_changes(settings, hashed_token, since, limit):
    with connect(settings.db_path) as conn:
        conn.execute("BEGIN")
        user = require_session(conn, hashed_token)
        if since > user["revision"]:
            fail(409, "cursor_invalid", "Cursor is ahead of server; perform full reconciliation")
        rows = conn.execute(
            "SELECT * FROM records WHERE user_id=? AND version>? ORDER BY version LIMIT ?",
            (user["id"], since, limit + 1),
        ).fetchall()
        more = len(rows) > limit
        rows = rows[:limit]
        return {
            "records": [public_record(row, row["kind"] == "local") for row in rows],
            "next_cursor": rows[-1]["version"] if more else user["revision"],
            "has_more": more,
            "revision": user["revision"],
        }


def read_nes(settings, hashed_token, key):
    with connect(settings.db_path) as conn:
        conn.execute("BEGIN")
        user = require_session(conn, hashed_token)
        row = conn.execute("SELECT * FROM records WHERE user_id=? AND kind='nes' AND key=?",
                           (user["id"], key)).fetchone()
        if row is None:
            fail(404, "not_found", "No snapshot has been stored for this cartridge")
        result = public_record(row, False)
        result["snapshot"] = None if row["deleted"] else json.loads(row["value"])
        return result
