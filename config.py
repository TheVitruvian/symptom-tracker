import os
import secrets
from contextvars import ContextVar
from pathlib import Path
from typing import Optional

DB_PATH = "symptoms.db"
SECRET_KEY_PATH = Path(".app_secret_key")
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14
SESSION_COOKIE_NAME = "profile_session"
CSRF_COOKIE_NAME = "csrf_token"
MAX_PHOTO_SIZE = 5 * 1024 * 1024

PHYSICIAN_COOKIE_NAME = "physician_session"
PHYSICIAN_CTX_COOKIE  = "physician_ctx"
_current_user_id: ContextVar[int]           = ContextVar("_current_user_id", default=1)
_physician_ctx:   ContextVar[Optional[str]] = ContextVar("_physician_ctx",   default=None)

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

PUBLIC_PATHS = {"/login", "/signup", "/logout"}


def _load_secret_key() -> str:
    env_key = os.environ.get("APP_SECRET_KEY", "").strip()
    if env_key:
        return env_key
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    SECRET_KEY_PATH.write_text(key, encoding="utf-8")
    return key


SECRET_KEY = _load_secret_key()
