import hashlib
import hmac
import secrets
import unicodedata
from datetime import datetime, timezone


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_username(username: str) -> tuple[str, str]:
    display = unicodedata.normalize("NFC", username).strip()
    if not 1 <= len(display) <= 64 or any(c.isspace() or unicodedata.category(c).startswith("C") for c in display):
        raise ValueError("用户名应为 1–64 个字符，不能包含空白或控制字符")
    return display, display.casefold()


def hash_password(password: str) -> str:
    if not 4 <= len(password) <= 1024:
        raise ValueError("密码长度必须为 4–1024 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt),
                                n=int(n), r=int(r), p=int(p), dklen=32)
        return hmac.compare_digest(digest.hex(), expected)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
