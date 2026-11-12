import hashlib
import json
import secrets
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .config import Settings
from .db import connect, initialize
from .models import Batch, Login, NesWrite
from .security import hash_password, normalize_username, token_hash, verify_password
from .storage import fail, list_changes, read_nes, require_session, write_records


class BodyLimitMiddleware:
    """Bound bodies before JSON parsing, including chunked requests without Content-Length."""

    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def safe_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend([
                    (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff"),
                ])
            await send(message)

        chunks, length = [], 0
        headers = dict(scope.get("headers", []))
        try:
            declared = int(headers.get(b"content-length", b"0"))
        except ValueError:
            declared = self.limit + 1
        if declared > self.limit:
            response = JSONResponse({"error": {"code": "body_too_large", "message": "Request body exceeds limit"}}, 413)
            return await response(scope, receive, safe_send)
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            length += len(chunk)
            if length > self.limit:
                response = JSONResponse({"error": {"code": "body_too_large", "message": "Request body exceeds limit"}}, 413)
                return await response(scope, receive, safe_send)
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        consumed = False

        async def buffered_receive():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}
            return await receive()

        await self.app(scope, buffered_receive, safe_send)


def create_app(settings: Settings | None = None):
    settings = settings or Settings.load()
    # Unknown users still pay the password verification cost.
    dummy_hash = hash_password(secrets.token_urlsafe(24))

    @asynccontextmanager
    async def lifespan(app):
        initialize(settings.db_path)
        yield

    app = FastAPI(title="LiteGameServer", version=__version__, lifespan=lifespan,
                  description="Private save sync. Users can only be provisioned by the server CLI.")
    app.state.settings = settings
    app.add_middleware(BodyLimitMiddleware, limit=settings.max_body_bytes)
    # Outermost user middleware: even body-limit errors get CORS headers.
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                       allow_credentials=False, allow_methods=["GET", "POST", "PUT", "DELETE"],
                       allow_headers=["Authorization", "Content-Type"],
                       expose_headers=["Retry-After"], max_age=600)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        error = exc.detail if isinstance(exc.detail, dict) else {"code": "http_error", "message": str(exc.detail)}
        headers = dict(exc.headers or {})
        if exc.status_code == 401:
            headers["WWW-Authenticate"] = "Bearer"
        return JSONResponse({"error": error}, exc.status_code, headers=headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Never echo raw request inputs: they may contain passwords or private saves.
        fields = [{"loc": list(item["loc"]), "type": item["type"], "message": item["msg"]} for item in exc.errors()]
        return JSONResponse({"error": {"code": "validation_error", "message": "Invalid request", "fields": fields}}, 422)

    def authenticated(authorization: Annotated[str | None, Header()] = None):
        if not authorization or not authorization.startswith("Bearer "):
            fail(401, "unauthorized", "Bearer token required")
        token = authorization[7:]
        if not 32 <= len(token) <= 256:
            fail(401, "unauthorized", "Invalid bearer token")
        hashed = token_hash(token)
        with connect(settings.db_path) as conn:
            require_session(conn, hashed)
        return hashed

    Auth = Annotated[str, Depends(authenticated)]

    @app.get("/healthz")
    def health():
        with connect(settings.db_path) as conn:
            conn.execute("SELECT 1 FROM users LIMIT 1")
        return {"status": "ok"}

    @app.get("/api/v1/info")
    def info():
        return {
            "service": "litegame-sync", "version": __version__, "protocol_version": 1,
            "api_base": "/api/v1", "auth": "password-bearer", "registration": "cli-only",
            "features": ["local-storage", "nes-snapshots", "optimistic-concurrency", "deletion-tombstones"],
            "limits": {"body_bytes": settings.max_body_bytes, "record_bytes": settings.max_record_bytes,
                       "nes_bytes": settings.max_nes_bytes, "user_bytes": settings.user_quota_bytes,
                       "record_count": settings.max_records, "batch_count": 100},
        }

    @app.post("/api/v1/auth/login")
    def login(body: Login, request: Request):
        try:
            _, key = normalize_username(body.username)
        except ValueError:
            key = "invalid:" + hashlib.sha256(body.username.encode()).hexdigest()
        now = int(time.time())
        # Never trust X-Forwarded-For supplied by an arbitrary HTTP client. With the
        # default loopback reverse proxy, IP limits apply to the proxy as a whole.
        ip = request.client.host if request.client else "unknown"
        scopes = ["ip:" + ip, "user:" + key]
        with connect(settings.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM login_attempts WHERE started_at<=?", (now - settings.login_window_seconds,))
            for scope in scopes:
                row = conn.execute("SELECT * FROM login_attempts WHERE scope=?", (scope,)).fetchone()
                if row and row["attempts"] >= settings.login_limit:
                    retry = max(1, row["started_at"] + settings.login_window_seconds - now)
                    conn.commit()
                    raise HTTPException(429, {"code": "login_rate_limited", "message": "Too many login attempts"},
                                        headers={"Retry-After": str(retry)})
            for scope in scopes:
                conn.execute("INSERT INTO login_attempts VALUES(?,?,1) ON CONFLICT(scope) "
                             "DO UPDATE SET attempts=attempts+1", (scope, now))
            user = conn.execute("SELECT * FROM users WHERE username_key=?", (key,)).fetchone()
            conn.commit()
        verified = verify_password(body.password, user["password_hash"] if user else dummy_hash)
        if not verified or not user or not user["enabled"]:
            fail(401, "invalid_credentials", "Invalid username or password")
        token = secrets.token_urlsafe(32)
        expires = now + settings.session_days * 86400
        with connect(settings.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Check again after expensive password hashing; disable/reset may race it.
            fresh = conn.execute("SELECT * FROM users WHERE id=?", (user["id"],)).fetchone()
            if not fresh or not fresh["enabled"] or fresh["password_hash"] != user["password_hash"]:
                fail(401, "invalid_credentials", "Invalid username or password")
            conn.execute("DELETE FROM sessions WHERE expires_at<=?", (now,))
            conn.execute("INSERT INTO sessions VALUES(?,?,?,?)", (token_hash(token), user["id"], now, expires))
            # Keep session growth bounded. Creating a 21st session expires the oldest.
            conn.execute("DELETE FROM sessions WHERE user_id=? AND token_hash NOT IN "
                         "(SELECT token_hash FROM sessions WHERE user_id=? ORDER BY created_at DESC, rowid DESC LIMIT 20)",
                         (user["id"], user["id"]))
            conn.commit()
        return {"access_token": token, "token_type": "bearer", "expires_at": expires,
                "user": {"id": user["id"], "username": user["username"]}}

    @app.post("/api/v1/auth/logout")
    def logout(auth: Auth):
        with connect(settings.db_path) as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (auth,))
        return {"ok": True}

    @app.get("/api/v1/auth/me")
    def me(auth: Auth):
        with connect(settings.db_path) as conn:
            conn.execute("BEGIN")
            user = require_session(conn, auth)
            usage = conn.execute("SELECT COALESCE(SUM(value_bytes),0),COUNT(*) FROM records WHERE user_id=?",
                                 (user["id"],)).fetchone()
            return {"id": user["id"], "username": user["username"], "revision": user["revision"],
                    "usage_bytes": usage[0], "record_count": usage[1]}

    @app.get("/api/v1/records")
    def records(auth: Auth, since: Annotated[int, Query(ge=0)] = 0,
                limit: Annotated[int, Query(ge=1, le=100)] = 50):
        return list_changes(settings, auth, since, limit)

    @app.post("/api/v1/records/batch")
    def batch(body: Batch, auth: Auth):
        return write_records(settings, auth, "local", [change.model_dump() for change in body.changes])

    Sha = Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")]

    @app.get("/api/v1/nes/{sha256}")
    def nes_get(sha256: Sha, auth: Auth):
        return read_nes(settings, auth, sha256)

    @app.put("/api/v1/nes/{sha256}")
    def nes_put(sha256: Sha, body: NesWrite, auth: Auth):
        if body.snapshot.sha256 != sha256:
            fail(422, "sha256_mismatch", "Snapshot SHA-256 differs from URL")
        try:
            value = json.dumps(body.snapshot.model_dump(), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (ValueError, TypeError):
            fail(422, "invalid_snapshot", "Snapshot must contain finite JSON values")
        return write_records(settings, auth, "nes", [
            {"key": sha256, "value": value, "expected_version": body.expected_version},
        ])

    @app.delete("/api/v1/nes/{sha256}")
    def nes_delete(sha256: Sha, auth: Auth, expected_version: Annotated[int, Query(ge=0)]):
        return write_records(settings, auth, "nes", [
            {"key": sha256, "value": None, "expected_version": expected_version},
        ])

    return app
