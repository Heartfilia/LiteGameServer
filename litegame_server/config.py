import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class Settings:
    db_path: Path
    host: str = "127.0.0.1"
    port: int = 9494
    cors_origins: tuple[str, ...] = ()
    session_days: int = 30
    max_body_bytes: int = 16 * 1024 * 1024
    max_record_bytes: int = 1024 * 1024
    max_nes_bytes: int = 12 * 1024 * 1024
    user_quota_bytes: int = 64 * 1024 * 1024
    max_records: int = 5000
    login_limit: int = 10
    login_window_seconds: int = 900

    @classmethod
    def load(cls) -> "Settings":
        load_dotenv(os.getenv("LITEGAME_ENV_FILE", str(ROOT / ".env")), override=False)
        db_path = Path(os.getenv("LITEGAME_DB", "data/litegame.sqlite3")).expanduser()
        if not db_path.is_absolute():
            db_path = ROOT / db_path
        origins = tuple(dict.fromkeys(
            item.strip() for item in os.getenv("LITEGAME_CORS_ORIGINS", "").split(",") if item.strip()
        ))
        for origin in origins:
            parts = urlsplit(origin)
            if (parts.scheme not in ("http", "https") or not parts.netloc
                    or parts.path or parts.query or parts.fragment or parts.username or parts.password
                    or "*" in origin or any(c.isspace() for c in origin)):
                raise ValueError(f"Invalid CORS origin: {origin}")
        return cls(
            db_path=db_path,
            host=os.getenv("LITEGAME_HOST", "127.0.0.1"),
            port=positive_int("LITEGAME_PORT", 9494),
            cors_origins=origins,
            session_days=positive_int("LITEGAME_SESSION_DAYS", 30),
            max_body_bytes=positive_int("LITEGAME_MAX_BODY_BYTES", 16 * 1024 * 1024),
            max_record_bytes=positive_int("LITEGAME_MAX_RECORD_BYTES", 1024 * 1024),
            max_nes_bytes=positive_int("LITEGAME_MAX_NES_BYTES", 12 * 1024 * 1024),
            user_quota_bytes=positive_int("LITEGAME_USER_QUOTA_BYTES", 64 * 1024 * 1024),
            max_records=positive_int("LITEGAME_MAX_RECORDS", 5000),
            login_limit=positive_int("LITEGAME_LOGIN_LIMIT", 10),
            login_window_seconds=positive_int("LITEGAME_LOGIN_WINDOW_SECONDS", 900),
        )
