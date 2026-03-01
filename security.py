import hashlib
import hmac
import logging
import os
import secrets
import smtplib
import threading
from collections import defaultdict
from email.mime.text import MIMEText
from time import time
from typing import Optional

import requests
from fastapi import Request

from config import (
    SESSION_COOKIE_NAME,
    SESSION_TTL_SECONDS,
    CSRF_COOKIE_NAME,
    PHYSICIAN_COOKIE_NAME,
    SECRET_KEY,
)
from db import get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory rate limiting (per-IP, resets on server restart)
# ---------------------------------------------------------------------------
_rate_lock = threading.Lock()
_login_buckets: dict[str, list[float]] = defaultdict(list)
_reset_buckets: dict[str, list[float]] = defaultdict(list)
_physician_login_buckets: dict[str, list[float]] = defaultdict(list)
_physician_signup_buckets: dict[str, list[float]] = defaultdict(list)

_LOGIN_WINDOW = 300   # 5 minutes
_LOGIN_MAX = 10       # attempts per window per IP
_RESET_WINDOW = 900   # 15 minutes
_RESET_MAX = 5        # attempts per window per IP


def _check_rate_limit(bucket: dict, ip: str, window: int, max_attempts: int) -> bool:
    """Return True if the request should be allowed, False if rate limited."""
    now = time()
    with _rate_lock:
        bucket[ip] = [t for t in bucket[ip] if now - t < window]
        if len(bucket[ip]) >= max_attempts:
            return False
        bucket[ip].append(now)
        return True


def _is_login_allowed(ip: str) -> bool:
    return _check_rate_limit(_login_buckets, ip, _LOGIN_WINDOW, _LOGIN_MAX)


def _is_reset_allowed(ip: str) -> bool:
    return _check_rate_limit(_reset_buckets, ip, _RESET_WINDOW, _RESET_MAX)


def _is_physician_login_allowed(ip: str) -> bool:
    return _check_rate_limit(_physician_login_buckets, ip, _LOGIN_WINDOW, _LOGIN_MAX)


def _is_physician_signup_allowed(ip: str) -> bool:
    return _check_rate_limit(_physician_signup_buckets, ip, _RESET_WINDOW, _RESET_MAX)


def _request_origin_host(request: Request) -> str:
    header = request.headers.get("origin") or request.headers.get("referer") or ""
    if not header:
        return ""
    try:
        if "://" in header:
            return header.split("://", 1)[1].split("/", 1)[0].lower()
        return ""
    except Exception:
        return ""


def _is_same_origin(request: Request) -> bool:
    origin_host = _request_origin_host(request)
    if not origin_host:
        return False
    return origin_host == request.url.netloc.lower()


def _ensure_csrf_cookie(request: Request, response):
    if request.cookies.get(CSRF_COOKIE_NAME):
        return response
    response.set_cookie(
        CSRF_COOKIE_NAME,
        secrets.token_urlsafe(32),
        httponly=False,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return response


def _csrf_header_valid(request: Request) -> bool:
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
    header_token = request.headers.get("x-csrf-token", "")
    return bool(cookie_token) and hmac.compare_digest(cookie_token, header_token)


def _csrf_query_valid(request: Request) -> bool:
    """Check CSRF token submitted as a query parameter (used by HTML form POSTs)."""
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME, "")
    query_token = request.query_params.get("_csrf", "")
    return bool(cookie_token) and hmac.compare_digest(cookie_token, query_token)


def _hash_password(plaintext: str) -> str:
    salt = secrets.token_bytes(32)
    dk = hashlib.pbkdf2_hmac("sha256", plaintext.encode(), salt, 480_000)
    return salt.hex() + ":" + dk.hex()


def _verify_password(plaintext: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split(":")
        dk = hashlib.pbkdf2_hmac("sha256", plaintext.encode(), bytes.fromhex(salt_hex), 480_000)
        return hmac.compare_digest(dk, bytes.fromhex(dk_hex))
    except Exception:
        return False


def _make_session_token(username: str, password_hash: str) -> str:
    exp = int(time()) + SESSION_TTL_SECONDS
    nonce = secrets.token_urlsafe(16)
    payload = f"{username}:{exp}:{nonce}"
    sig = hmac.new(SECRET_KEY.encode(), f"{payload}:{password_hash}".encode(), "sha256").hexdigest()
    return f"{payload}:{sig}"


def _verify_session_token(token: str, username: str, password_hash: str) -> bool:
    try:
        token_username, exp_s, nonce, sig = token.split(":", 3)
        if token_username != username:
            return False
        exp = int(exp_s)
        if exp < int(time()):
            return False
        payload = f"{token_username}:{exp}:{nonce}"
        expected = hmac.new(
            SECRET_KEY.encode(),
            f"{payload}:{password_hash}".encode(),
            "sha256",
        ).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False


def _set_session_cookie(response, request: Request, username: str, password_hash: str):
    response.set_cookie(
        SESSION_COOKIE_NAME,
        _make_session_token(username, password_hash),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=SESSION_TTL_SECONDS,
    )
    return response


def _get_authenticated_user(request: Request):
    """Extract username from session token; look up user_profile by username. Returns Row or None."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME, "")
    if not cookie:
        return None
    parts = cookie.split(":", 3)
    if len(parts) < 4:
        return None
    token_username = parts[0]
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM user_profile WHERE username = ?", (token_username,)
        ).fetchone()
    if not row or not row["password_hash"]:
        return None
    if not _verify_session_token(cookie, token_username, row["password_hash"]):
        return None
    return row


def _has_any_patient() -> bool:
    """Return True if at least one patient account exists."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM user_profile WHERE password_hash != '' LIMIT 1"
        ).fetchone()
    return row is not None


def _get_authenticated_physician(request: Request):
    """Extract username from physician session cookie; look up in physicians table. Returns Row or None."""
    cookie = request.cookies.get(PHYSICIAN_COOKIE_NAME, "")
    if not cookie:
        return None
    parts = cookie.split(":", 3)
    if len(parts) < 4:
        return None
    token_username = parts[0]
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM physicians WHERE username = ?", (token_username,)
        ).fetchone()
    if not row:
        return None
    if not _verify_session_token(cookie, token_username, row["password_hash"]):
        return None
    return row


def _set_physician_cookie(response, request: Request, username: str, password_hash: str):
    response.set_cookie(
        PHYSICIAN_COOKIE_NAME,
        _make_session_token(username, password_hash),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=SESSION_TTL_SECONDS,
    )
    return response


def _send_reset_email(to_email: str, reset_url: str) -> bool:
    """Send a password-reset email via SMTP (preferred), fallback to Mailgun API."""
    subject = "Reset your Symptom Tracker password"
    text_body = (
        f"Click the link below to reset your Symptom Tracker password (expires in 1 hour):\n\n"
        f"{reset_url}\n\n"
        "If you did not request a password reset, you can ignore this email."
    )

    # SMTP preferred path
    smtp_host = os.environ.get("SMTP_HOST", "")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASSWORD", "")
    smtp_from = os.environ.get("SMTP_FROM", smtp_user)
    if smtp_host and smtp_user and smtp_pass:
        msg = MIMEText(text_body)
        msg["Subject"] = subject
        msg["From"] = smtp_from
        msg["To"] = to_email
        try:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=20) as s:
                s.starttls()
                s.login(smtp_user, smtp_pass)
                s.sendmail(smtp_from, [to_email], msg.as_string())
            return True
        except Exception:
            logger.exception("SMTP password reset email send failed")

    # Mailgun API fallback
    mailgun_api_key = os.environ.get("MAILGUN_API_KEY", "")
    mailgun_domain = os.environ.get("MAILGUN_DOMAIN", "")
    mailgun_from = os.environ.get("MAILGUN_FROM", "")
    if mailgun_api_key and mailgun_domain:
        sender = mailgun_from or f"no-reply@{mailgun_domain}"
        try:
            resp = requests.post(
                f"https://api.mailgun.net/v3/{mailgun_domain}/messages",
                auth=("api", mailgun_api_key),
                data={
                    "from": sender,
                    "to": [to_email],
                    "subject": subject,
                    "text": text_body,
                },
                timeout=15,
            )
            if 200 <= resp.status_code < 300:
                return True
            logger.warning(
                "Mailgun reset email send failed with status %s: %s",
                resp.status_code,
                (resp.text or "")[:200],
            )
        except Exception:
            logger.exception("Mailgun API password reset email send failed")
    else:
        logger.warning(
            "Password reset email not attempted: missing SMTP and Mailgun configuration"
        )
    return False


def _physician_owns_patient(physician_id: int, patient_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM physician_patients WHERE physician_id = ? AND patient_id = ?",
            (physician_id, patient_id),
        ).fetchone()
    return row is not None
