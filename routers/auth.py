import html
import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import SESSION_COOKIE_NAME
from db import get_db
from security import (
    _hash_password,
    _verify_password,
    _set_session_cookie,
    _has_any_patient,
    _get_authenticated_user,
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
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    if not username.strip():
        return RedirectResponse(url="/signup?error=Username+is+required", status_code=303)
    if len(new_password) < 8:
        return RedirectResponse(url="/signup?error=Password+must+be+at+least+8+characters", status_code=303)
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
    with get_db() as conn:
        conn.execute(
            "INSERT INTO user_profile (username, password_hash, share_code) VALUES (?, ?, ?)",
            (username.strip(), pw_hash, share_code),
        )
        conn.commit()
    resp = RedirectResponse(url="/", status_code=303)
    _set_session_cookie(resp, request, username.strip(), pw_hash)
    return resp


@router.get("/login", response_class=HTMLResponse)
def login_get(request: Request, error: str = ""):
    if not _has_any_patient():
        return RedirectResponse(url="/signup", status_code=303)
    if _get_authenticated_user(request):
        return RedirectResponse(url="/", status_code=303)
    error_banner = f'<div class="alert">{html.escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}<title>Log In</title></head>
<body>
  <div class="container">
    <h1>Symptom Tracker</h1>
    <p style="color:#555; font-size:14px; margin-bottom:16px;">Enter your credentials to continue.</p>
    {error_banner}
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
    <p style="margin-top:20px; font-size:13px; color:#6b7280;">
      No account yet? <a href="/signup" style="color:#3b82f6;">Sign up</a>
    </p>
  </div>
</body>
</html>
"""


@router.post("/login")
def login_post(request: Request, username: str = Form(""), password: str = Form("")):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM user_profile WHERE username = ?", (username.strip(),)
        ).fetchone()
    if not row or not row["password_hash"]:
        return RedirectResponse(url="/login?error=Incorrect+username+or+password", status_code=303)
    if not _verify_password(password, row["password_hash"]):
        return RedirectResponse(url="/login?error=Incorrect+username+or+password", status_code=303)
    resp = RedirectResponse(url="/", status_code=303)
    _set_session_cookie(resp, request, row["username"], row["password_hash"])
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp
