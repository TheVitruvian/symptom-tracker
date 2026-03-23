import os
import secrets
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from typing import Optional

DB_PATH = os.environ.get("DB_PATH", "symptoms.db")
SECRET_KEY_PATH = Path(".app_secret_key")
APP_ENV_PATH = Path(".env")
SESSION_TTL_SECONDS = 60 * 60 * 8   # 8 hours
INACTIVITY_TIMEOUT_MS = 30 * 60 * 1000  # 30 minutes
SESSION_COOKIE_NAME = "profile_session"
CSRF_COOKIE_NAME = "csrf_token"
MAX_PHOTO_SIZE = 5 * 1024 * 1024

PHYSICIAN_COOKIE_NAME = "physician_session"
PHYSICIAN_CTX_COOKIE  = "physician_ctx"
_current_user_id: ContextVar[int]           = ContextVar("_current_user_id", default=0)
_physician_ctx:   ContextVar[Optional[str]] = ContextVar("_physician_ctx",   default=None)
_client_now: ContextVar[Optional[datetime]] = ContextVar("_client_now", default=None)
_client_tz_offset_min: ContextVar[Optional[int]] = ContextVar("_client_tz_offset_min", default=None)

UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "uploads"))
UPLOAD_DIR.mkdir(exist_ok=True)

PUBLIC_PATHS = {"/", "/login", "/signup", "/logout", "/forgot-password", "/forgot-username", "/reset-password", "/verify-email", "/api/check-username", "/api/check-email"}
RESET_TOKEN_TTL_SECONDS = 3600  # 1 hour
VERIFICATION_TOKEN_TTL_SECONDS = 86400  # 24 hours
APP_BASE_URL = os.environ.get("APP_BASE_URL", "").strip().rstrip("/")


def _parse_allowed_hosts(raw: str) -> list[str]:
    hosts: list[str] = []
    for item in raw.split(","):
        host = item.strip().lower()
        if host:
            hosts.append(host)
    return hosts


def _allowed_hosts() -> list[str]:
    raw = os.environ.get("ALLOWED_HOSTS", "").strip()
    if raw:
        return _parse_allowed_hosts(raw)
    if APP_BASE_URL:
        parsed = urlsplit(APP_BASE_URL)
        if parsed.netloc:
            return [parsed.netloc.lower()]
    return ["localhost", "127.0.0.1", "testserver"]


ALLOWED_HOSTS = _allowed_hosts()

FREQ_LABELS: dict[str, str] = {
    "once_daily":  "Once daily",
    "twice_daily": "Twice daily",
    "three_daily": "Three times daily",
    "prn":         "As needed (PRN)",
}


def _set_client_clock(tz_offset_cookie: str):
    """Set per-request client-local clock derived from JS timezone offset cookie."""
    offset = None
    try:
        offset = int((tz_offset_cookie or "").strip())
    except ValueError:
        offset = None
    if offset is not None and -840 <= offset <= 840:
        _client_tz_offset_min.set(offset)
        _client_now.set(datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=offset))
        return
    _client_tz_offset_min.set(None)
    _client_now.set(datetime.now())


def _now_local() -> datetime:
    return _client_now.get() or datetime.now()


def _today_local() -> date:
    return _now_local().date()


def _to_utc_storage(dt_local: datetime) -> str:
    """Convert request-local naive datetime to UTC storage format."""
    offset = _client_tz_offset_min.get()
    if offset is not None:
        dt_utc = dt_local + timedelta(minutes=offset)
        return dt_utc.strftime("%Y-%m-%d %H:%M:%S")
    # Fallback: interpret naive datetime in server local timezone.
    server_tz = datetime.now().astimezone().tzinfo
    dt_utc = dt_local.replace(tzinfo=server_tz).astimezone(timezone.utc).replace(tzinfo=None)
    return dt_utc.strftime("%Y-%m-%d %H:%M:%S")


def _from_utc_storage(ts: str) -> datetime:
    """Convert UTC storage string to request-local naive datetime."""
    dt_utc = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    offset = _client_tz_offset_min.get()
    if offset is not None:
        return dt_utc - timedelta(minutes=offset)
    server_tz = datetime.now().astimezone().tzinfo
    return dt_utc.replace(tzinfo=timezone.utc).astimezone(server_tz).replace(tzinfo=None)


def _load_secret_key() -> str:
    env_key = os.environ.get("APP_SECRET_KEY", "").strip()
    if env_key:
        return env_key
    if SECRET_KEY_PATH.exists():
        os.chmod(SECRET_KEY_PATH, 0o600)
        return SECRET_KEY_PATH.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    # Write with owner-only permissions (rw-------) so the key isn't world-readable
    fd = os.open(str(SECRET_KEY_PATH), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    return key


SECRET_KEY = _load_secret_key()

if APP_ENV_PATH.exists():
    try:
        os.chmod(APP_ENV_PATH, 0o600)
    except OSError:
        pass
