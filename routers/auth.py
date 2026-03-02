import html
import re
import secrets
from time import time

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import SESSION_COOKIE_NAME, RESET_TOKEN_TTL_SECONDS, VERIFICATION_TOKEN_TTL_SECONDS
from db import get_db
from security import (
    _hash_password,
    _verify_password,
    _set_session_cookie,
    _has_any_patient,
    _get_authenticated_user,
    _send_reset_email,
    _send_verification_email,
    _is_login_allowed,
    _is_reset_allowed,
    _is_username_login_allowed,
    _record_login_failure,
    _clear_username_lockout,
    _audit_log,
    _password_meets_complexity,
)
from ui import PAGE_STYLE

router = APIRouter()


@router.get("/signup", response_class=HTMLResponse)
def signup_get(error: str = ""):
    error_banner = f'<div class="alert">{html.escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}<title>Set Up Account</title></head>
<body>
  <div class="container">
    <h1>Set Up Your Account</h1>
    <p style="color:#555; font-size:14px; margin-bottom:16px;">
      Choose a username and password to protect your symptom tracker.
    </p>
    {error_banner}
    <form method="post" action="/signup">
      <div class="form-group">
        <label for="username">Username</label>
        <input type="text" id="username" name="username"
          placeholder="Your username" required autocomplete="username">
      </div>
      <div class="form-group">
        <label for="email">Email</label>
        <input type="email" id="email" name="email"
          placeholder="you@example.com" required autocomplete="email">
      </div>
      <div class="form-group">
        <label for="new_password">Password</label>
        <input type="password" id="new_password" name="new_password"
          placeholder="At least 8 characters" required autocomplete="new-password">
      </div>
      <div class="form-group">
        <label for="confirm_password">Confirm Password</label>
        <input type="password" id="confirm_password" name="confirm_password"
          placeholder="Repeat password" required autocomplete="new-password">
      </div>
      <button type="submit" class="btn-primary">Create Account</button>
    </form>
  </div>
</body>
</html>
"""


@router.post("/signup")
def signup_post(
    request: Request,
    username: str = Form(""),
    email: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    if len(username) > 60:
        return RedirectResponse(url="/signup?error=Username+must+be+60+characters+or+fewer", status_code=303)
    if len(email) > 254:
        return RedirectResponse(url="/signup?error=Email+address+is+too+long", status_code=303)
    if len(new_password) > 1000:
        return RedirectResponse(url="/signup?error=Password+is+too+long", status_code=303)
    if not username.strip():
        return RedirectResponse(url="/signup?error=Username+is+required", status_code=303)
    if not email.strip():
        return RedirectResponse(url="/signup?error=Email+is+required", status_code=303)
    if not _EMAIL_RE.match(email.strip().lower()):
        return RedirectResponse(url="/signup?error=Invalid+email+address", status_code=303)
    pw_ok, pw_err = _password_meets_complexity(new_password)
    if not pw_ok:
        return RedirectResponse(url=f"/signup?error={pw_err.replace(' ', '+')}", status_code=303)
    if new_password != confirm_password:
        return RedirectResponse(url="/signup?error=Passwords+do+not+match", status_code=303)
    with get_db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM user_profile WHERE username = ?", (username.strip(),)
        ).fetchone()
        if existing:
            return RedirectResponse(url="/signup?error=Username+already+taken", status_code=303)
    pw_hash = _hash_password(new_password)
    share_code = secrets.token_hex(4).upper()
    ip = request.client.host if request.client else "unknown"
    email_clean = email.strip().lower()
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO user_profile (username, email, password_hash, share_code) VALUES (?, ?, ?, ?)",
            (username.strip(), email_clean, pw_hash, share_code),
        )
        new_user_id = cursor.lastrowid
        conn.commit()
    if email_clean:
        verify_token = secrets.token_urlsafe(32)
        verify_expires = int(time()) + VERIFICATION_TOKEN_TTL_SECONDS
        with get_db() as conn:
            conn.execute(
                "INSERT INTO email_verification_tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
                (verify_token, new_user_id, verify_expires),
            )
            conn.commit()
        base = str(request.base_url).rstrip("/")
        verify_url = f"{base}/verify-email?token={verify_token}"
        _send_verification_email(email_clean, verify_url)
    _audit_log("signup", username=username.strip(), ip_address=ip)
    resp = RedirectResponse(url="/onboarding/1", status_code=303)
    _set_session_cookie(resp, request, username.strip(), pw_hash)
    return resp


@router.get("/login", response_class=HTMLResponse)
def login_get(request: Request, error: str = "", success: str = ""):
    if not _has_any_patient():
        return RedirectResponse(url="/signup", status_code=303)
    if _get_authenticated_user(request):
        return RedirectResponse(url="/", status_code=303)
    error_banner = f'<div class="alert">{html.escape(error)}</div>' if error else ""
    success_banner = (
        f'<div style="background:#dcfce7; border:1px solid #86efac; color:#15803d; border-radius:6px;'
        f' padding:10px 14px; margin-bottom:16px; font-size:14px;">{html.escape(success)}</div>'
    ) if success else ""
    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}<title>Log In</title></head>
<body>
  <div class="container">
    <h1>Symptom Tracker</h1>
    <p style="color:#555; font-size:14px; margin-bottom:16px;">Enter your credentials to continue.</p>
    {error_banner}
    {success_banner}
    <form method="post" action="/login">
      <div class="form-group">
        <label for="username">Username</label>
        <input type="text" id="username" name="username" required autocomplete="username">
      </div>
      <div class="form-group">
        <label for="password">Password</label>
        <input type="password" id="password" name="password" required autocomplete="current-password">
      </div>
      <button type="submit" class="btn-primary">Log In</button>
    </form>
    <p style="margin-top:16px; font-size:13px; color:#6b7280;">
      <a href="/forgot-password" style="color:#7c3aed;">Forgot your password?</a>
    </p>
    <p style="margin-top:8px; font-size:13px; color:#6b7280;">
      No account yet? <a href="/signup" style="color:#7c3aed;">Sign up</a>
    </p>
    <p style="margin-top:8px; font-size:13px; color:#6b7280;">
      Are you a physician? <a href="/physician/login" style="color:#7c3aed;">Physician login</a>
    </p>
  </div>
</body>
</html>
"""


@router.post("/login")
def login_post(request: Request, username: str = Form(""), password: str = Form("")):
    ip = request.client.host if request.client else "unknown"
    if not _is_login_allowed(ip):
        _audit_log("login_rate_limited_ip", username=username.strip()[:60], ip_address=ip)
        return RedirectResponse(url="/login?error=Too+many+attempts.+Please+wait+before+trying+again.", status_code=303)
    if not _is_username_login_allowed(username.strip()):
        _audit_log("login_account_locked", username=username.strip()[:60], ip_address=ip)
        return RedirectResponse(url="/login?error=Too+many+attempts.+Please+wait+before+trying+again.", status_code=303)
    if len(username) > 60 or len(password) > 1000:
        return RedirectResponse(url="/login?error=Incorrect+username+or+password", status_code=303)
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM user_profile WHERE username = ?", (username.strip(),)
        ).fetchone()
    if not row or not row["password_hash"]:
        _record_login_failure(username.strip())
        _audit_log("login_failure", username=username.strip(), ip_address=ip, details="user not found")
        return RedirectResponse(url="/login?error=Incorrect+username+or+password", status_code=303)
    if not _verify_password(password, row["password_hash"]):
        _record_login_failure(username.strip())
        _audit_log("login_failure", user_id=row["id"], username=row["username"], ip_address=ip)
        return RedirectResponse(url="/login?error=Incorrect+username+or+password", status_code=303)
    _audit_log("login_success", user_id=row["id"], username=row["username"], ip_address=ip)
    resp = RedirectResponse(url="/", status_code=303)
    _set_session_cookie(resp, request, row["username"], row["password_hash"])
    return resp


@router.post("/logout")
def logout(request: Request):
    user = _get_authenticated_user(request)
    if user:
        ip = request.client.host if request.client else "unknown"
        _audit_log("logout", user_id=user["id"], username=user["username"], ip_address=ip)
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp


@router.get("/forgot-password", response_class=HTMLResponse)
def forgot_password_get(sent: int = 0, error: str = ""):
    if sent:
        banner = (
            '<div style="background:#dcfce7; border:1px solid #86efac; color:#15803d; border-radius:6px;'
            ' padding:10px 14px; margin-bottom:16px; font-size:14px;">'
            "If that email address is registered, a password reset link has been sent.</div>"
        )
    elif error:
        banner = f'<div class="alert">{html.escape(error)}</div>'
    else:
        banner = ""
    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}<title>Forgot Password</title></head>
<body>
  <div class="container">
    <h1>Forgot Password</h1>
    <p style="color:#555; font-size:14px; margin-bottom:16px;">
      Enter the email address associated with your account and we'll send you a reset link.
    </p>
    {banner}
    <form method="post" action="/forgot-password">
      <div class="form-group">
        <label for="email">Email address</label>
        <input type="email" id="email" name="email" required autocomplete="email"
          placeholder="you@example.com">
      </div>
      <button type="submit" class="btn-primary">Send Reset Link</button>
    </form>
    <p style="margin-top:16px; font-size:13px; color:#6b7280;">
      <a href="/login" style="color:#7c3aed;">&larr; Back to login</a>
    </p>
  </div>
</body>
</html>
"""


@router.post("/forgot-password")
def forgot_password_post(request: Request, email: str = Form("")):
    ip = request.client.host if request.client else "unknown"
    if not _is_reset_allowed(ip):
        return RedirectResponse(url="/forgot-password?sent=1", status_code=303)
    if len(email) > 254:
        return RedirectResponse(url="/forgot-password?sent=1", status_code=303)
    email = email.strip().lower()
    if email and not _EMAIL_RE.match(email):
        return RedirectResponse(url="/forgot-password?sent=1", status_code=303)
    if email:
        with get_db() as conn:
            patient = conn.execute(
                "SELECT id FROM user_profile WHERE LOWER(email) = ?", (email,)
            ).fetchone()
        if patient:
            token = secrets.token_urlsafe(32)
            expires_at = int(time()) + RESET_TOKEN_TTL_SECONDS
            with get_db() as conn:
                conn.execute(
                    "DELETE FROM password_reset_tokens WHERE user_id = ?", (patient["id"],)
                )
                conn.execute(
                    "INSERT INTO password_reset_tokens (token, user_id, expires_at) VALUES (?, ?, ?)",
                    (token, patient["id"], expires_at),
                )
                conn.commit()
            base = str(request.base_url).rstrip("/")
            reset_url = f"{base}/reset-password?token={token}"
            _send_reset_email(email, reset_url)
    # Always show the same message regardless of outcome to prevent email enumeration
    return RedirectResponse(url="/forgot-password?sent=1", status_code=303)


@router.get("/reset-password", response_class=HTMLResponse)
def reset_password_get(token: str = "", error: str = ""):
    if not token:
        return RedirectResponse(url="/forgot-password?error=Missing+reset+token", status_code=303)
    with get_db() as conn:
        row = conn.execute(
            "SELECT user_id, expires_at FROM password_reset_tokens WHERE token = ?", (token,)
        ).fetchone()
    if not row or row["expires_at"] < int(time()):
        return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}<title>Reset Password</title></head>
<body>
  <div class="container">
    <h1>Reset Password</h1>
    <div class="alert">This reset link has expired or is invalid. Please
      <a href="/forgot-password" style="color:#b91c1c;">request a new one</a>.
    </div>
  </div>
</body>
</html>
"""
    error_banner = f'<div class="alert">{html.escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}<title>Reset Password</title></head>
<body>
  <div class="container">
    <h1>Reset Password</h1>
    {error_banner}
    <form method="post" action="/reset-password">
      <input type="hidden" name="token" value="{html.escape(token)}">
      <div class="form-group">
        <label for="new_password">New Password</label>
        <input type="password" id="new_password" name="new_password"
          placeholder="At least 8 characters" required autocomplete="new-password">
      </div>
      <div class="form-group">
        <label for="confirm_password">Confirm New Password</label>
        <input type="password" id="confirm_password" name="confirm_password"
          required autocomplete="new-password">
      </div>
      <button type="submit" class="btn-primary">Reset Password</button>
    </form>
  </div>
</body>
</html>
"""


@router.post("/reset-password")
def reset_password_post(
    token: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    if not token:
        return RedirectResponse(url="/forgot-password?error=Missing+reset+token", status_code=303)
    if len(token) > 200:
        return RedirectResponse(url="/forgot-password?error=Reset+link+expired+or+invalid", status_code=303)
    if len(new_password) > 1000:
        return RedirectResponse(
            url=f"/reset-password?token={token}&error=Password+is+too+long", status_code=303
        )
    with get_db() as conn:
        row = conn.execute(
            "SELECT user_id, expires_at FROM password_reset_tokens WHERE token = ?", (token,)
        ).fetchone()
    if not row or row["expires_at"] < int(time()):
        return RedirectResponse(
            url="/forgot-password?error=Reset+link+expired+or+invalid", status_code=303
        )
    pw_ok, pw_err = _password_meets_complexity(new_password)
    if not pw_ok:
        return RedirectResponse(
            url=f"/reset-password?token={token}&error={pw_err.replace(' ', '+')}",
            status_code=303,
        )
    if new_password != confirm_password:
        return RedirectResponse(
            url=f"/reset-password?token={token}&error=Passwords+do+not+match",
            status_code=303,
        )
    new_hash = _hash_password(new_password)
    with get_db() as conn:
        profile = conn.execute(
            "SELECT username FROM user_profile WHERE id = ?", (row["user_id"],)
        ).fetchone()
        conn.execute(
            "UPDATE user_profile SET password_hash = ? WHERE id = ?",
            (new_hash, row["user_id"]),
        )
        conn.execute("DELETE FROM password_reset_tokens WHERE token = ?", (token,))
        conn.commit()
    if profile:
        _clear_username_lockout(profile["username"])
    _audit_log("password_reset", user_id=row["user_id"])
    return RedirectResponse(url="/login?success=Password+updated.+Please+log+in.", status_code=303)


@router.get("/verify-email", response_class=HTMLResponse)
def verify_email_get(request: Request, token: str = ""):
    if not token or len(token) > 200:
        return RedirectResponse(url="/login?error=Invalid+verification+link", status_code=303)
    with get_db() as conn:
        row = conn.execute(
            "SELECT user_id, expires_at FROM email_verification_tokens WHERE token = ?", (token,)
        ).fetchone()
    if not row or row["expires_at"] < int(time()):
        return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}<title>Verify Email</title></head>
<body>
  <div class="container">
    <h1>Email Verification</h1>
    <div class="alert">This verification link has expired or is invalid. You can request a new one
      from your <a href="/profile" style="color:#b91c1c;">profile page</a>.
    </div>
  </div>
</body>
</html>
"""
    with get_db() as conn:
        conn.execute("UPDATE user_profile SET email_verified = 1 WHERE id = ?", (row["user_id"],))
        conn.execute("DELETE FROM email_verification_tokens WHERE token = ?", (token,))
        conn.commit()
    _audit_log("email_verified", user_id=row["user_id"])
    return RedirectResponse(url="/login?success=Email+verified.+You+can+now+log+in.", status_code=303)
