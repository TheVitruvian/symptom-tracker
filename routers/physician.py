import html
import secrets
from datetime import datetime, timezone
from time import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ai import generate_physician_digest, _ai_configured
from config import PHYSICIAN_COOKIE_NAME, PHYSICIAN_CTX_COOKIE, RESET_TOKEN_TTL_SECONDS
from db import get_db
from security import (
    _hash_password,
    _hash_token,
    _verify_password,
    _password_meets_complexity,
    _set_physician_cookie,
    _get_authenticated_physician,
    _physician_owns_patient,
    _is_physician_login_allowed,
    _is_physician_signup_allowed,
    _is_reset_allowed,
    _is_share_code_allowed,
    _send_reset_email,
    _send_username_reminder_email,
)
from ui import _calc_age
from email_validation import is_semantic_email, normalize_email

router = APIRouter()

_PHYSICIAN_STYLE = """
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" type="image/png" href="/static/favicon.png">
  <style>
    body { font-family: system-ui, sans-serif; background: #f5f5f5; margin: 0; color: #222; }
    .ph-nav { background: #1e3a8a; padding: 0 24px; height: 52px; display: flex;
              align-items: center; gap: 16px; }
    .ph-nav-title { font-weight: 800; color: #fff; font-size: 15px; }
    .ph-nav-sub { color: rgba(255,255,255,0.7); font-size: 14px; flex: 1; }
    .ph-btn-logout { background: transparent; border: 1px solid rgba(255,255,255,0.4);
                     color: rgba(255,255,255,0.8); border-radius: 6px; padding: 4px 12px;
                     font-size: 13px; cursor: pointer; font-family: inherit; }
    .container { max-width: 680px; margin: 0 auto; padding: 28px 24px; }
    h1 { margin: 0 0 4px; }
    .card { background: #fff; border: 1px solid #e0e0e0; border-radius: 8px;
            padding: 16px 20px; margin: 12px 0; }
    .btn-primary { background: #1e3a8a; color: #fff; border: none; border-radius: 8px;
                   padding: 10px 22px; font-size: 15px; cursor: pointer; font-weight: 600;
                   font-family: inherit; }
    .btn-primary:hover { background: #1e40af; }
    .btn-view { background: #059669; color: #fff; border: none; border-radius: 6px;
                padding: 6px 14px; font-size: 13px; cursor: pointer; font-family: inherit; font-weight: 600; }
    .btn-remove { background: none; border: 1px solid #e0e0e0; border-radius: 6px;
                  padding: 6px 12px; font-size: 13px; color: #888; cursor: pointer; font-family: inherit; }
    .btn-remove:hover { background: #fee2e2; border-color: #ef4444; color: #ef4444; }
    .form-group { margin-bottom: 18px; }
    label { display: block; font-weight: 600; font-size: 14px; margin-bottom: 6px; }
    input[type=text], input[type=password] { width: 100%; box-sizing: border-box;
      border: 1px solid #d1d5db; border-radius: 6px; padding: 8px 10px;
      font-size: 15px; font-family: inherit; }
    .alert { background: #fee2e2; border: 1px solid #fca5a5; color: #b91c1c;
             border-radius: 6px; padding: 10px 14px; margin-bottom: 16px; font-size: 14px; }
    .form-error { color: #b91c1c; background: #fee2e2; border: 1px solid #fca5a5;
                  border-radius: 6px; padding: 10px 14px; margin-bottom: 12px; font-size: 14px; }
    .empty { color: #888; font-style: italic; margin-top: 12px; }
    .patient-row { display: flex; align-items: center; gap: 12px; }
    .patient-info { flex: 1; }
    .patient-name { font-weight: 700; font-size: 16px; }
    .patient-meta { font-size: 13px; color: #666; margin-top: 2px; }
    .btn-digest { background: none; border: 1px solid #a78bfa; border-radius: 6px;
                  padding: 6px 12px; font-size: 13px; color: #7c3aed; cursor: pointer;
                  font-family: inherit; white-space: nowrap; }
    .btn-digest:hover { background: #f5f3ff; }
    .digest-box { margin-top: 10px; padding: 10px 14px; background: #faf5ff;
                  border: 1px solid #e9d5ff; border-radius: 6px; font-size: 13px;
                  color: #374151; line-height: 1.5; }
  </style>
  <script>
    (function() {
      function getCsrfToken() {
        return document.cookie.split('; ').reduce(function(v, c) {
          var p = c.split('='); return p[0] === 'csrf_token' ? decodeURIComponent(p[1]) : v;
        }, '');
      }
      function showToast(msg, type) {
        var el = document.createElement('div');
        el.style.cssText = 'position:fixed;bottom:24px;left:50%;transform:translateX(-50%);'
          + 'padding:11px 20px;border-radius:8px;font-size:14px;font-weight:500;z-index:8000;'
          + 'box-shadow:0 4px 16px rgba(0,0,0,.18);color:#fff;font-family:system-ui,sans-serif;'
          + (type === 'error' ? 'background:#991b1b;' : 'background:#166534;');
        el.textContent = msg;
        document.body.appendChild(el);
        setTimeout(function() { if (el.parentNode) el.parentNode.removeChild(el); }, type === 'error' ? 5000 : 3500);
      }
      async function ajaxSubmit(form) {
        var errEl = form.querySelector('.form-error');
        if (errEl) { errEl.style.display = 'none'; errEl.textContent = ''; }
        var btn = form.querySelector('[type=submit]');
        if (btn) btn.disabled = true;
        try {
          var res = await fetch(form.action || window.location.pathname, {
            method: form.method || 'POST',
            headers: { 'X-CSRF-Token': getCsrfToken() },
            body: new FormData(form)
          });
          var data = await res.json();
          if (!data.ok) {
            if (errEl) { errEl.textContent = data.error || 'An error occurred'; errEl.style.display = 'block'; }
            else showToast(data.error || 'An error occurred', 'error');
          } else {
            if (data.toast) showToast(data.toast, 'success');
            if (data.reload || data.redirect) {
              var delay = data.toast ? 1200 : 0;
              setTimeout(function() {
                if (data.reload) window.location.reload();
                else window.location.href = data.redirect;
              }, delay);
            }
          }
        } catch(e) {
          if (errEl) { errEl.textContent = 'Network error. Please try again.'; errEl.style.display = 'block'; }
          else showToast('Network error. Please try again.', 'error');
        } finally {
          if (btn) btn.disabled = false;
        }
      }
      document.addEventListener('submit', function(e) {
        if (!e.target.hasAttribute('data-ajax')) return;
        e.preventDefault();
        ajaxSubmit(e.target);
      });
      document.addEventListener('keydown', function(e) {
        if (e.key !== 'Enter') return;
        var el = e.target;
        if (el.tagName !== 'INPUT' || el.type === 'submit') return;
        var form = el.closest('form[data-ajax]');
        if (!form) return;
        e.preventDefault();
        form.requestSubmit();
      });
    })();
  </script>
"""


@router.get("/physician/signup", response_class=HTMLResponse)
def physician_signup_get():
    return f"""<!DOCTYPE html>
<html>
<head>{_PHYSICIAN_STYLE}<title>Physician Sign Up</title></head>
<body>
  <div class="ph-nav">
    <span class="ph-nav-title">Physician Portal</span>
  </div>
  <div class="container">
    <h1>Create Physician Account</h1>
    <p style="color:#555; font-size:14px; margin-bottom:16px;">
      Set up your physician login to manage multiple patients.
    </p>
    <div class="card">
      <form method="post" action="/physician/signup" data-ajax>
        <div class="form-error" style="display:none"></div>
        <div class="form-group">
          <label for="username">Username</label>
          <input type="text" id="username" name="username" required autocomplete="username">
        </div>
        <div class="form-group">
          <label for="email">Email</label>
          <input type="email" id="email" name="email" required autocomplete="email"
            placeholder="you@example.com">
        </div>
        <div class="form-group">
          <label for="new_password">Password</label>
          <input type="password" id="new_password" name="new_password"
            placeholder="At least 8 characters" required autocomplete="new-password">
        </div>
        <div class="form-group">
          <label for="confirm_password">Confirm Password</label>
          <input type="password" id="confirm_password" name="confirm_password"
            required autocomplete="new-password">
        </div>
        <button type="submit" class="btn-primary">Create Account</button>
      </form>
    </div>
    <p style="margin-top:16px; font-size:13px; color:#6b7280;">
      Already have an account? <a href="/physician/login" style="color:#1e3a8a;">Log in</a>
    </p>
  </div>
</body>
</html>
"""


@router.post("/physician/signup")
def physician_signup_post(
    request: Request,
    username: str = Form(""),
    email: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    ip = request.client.host if request.client else "unknown"
    if not _is_physician_signup_allowed(ip):
        return JSONResponse({"ok": False, "error": "Too many attempts. Please wait."}, status_code=429)
    if len(username) > 60:
        return JSONResponse({"ok": False, "error": "Username must be 60 characters or fewer"}, status_code=400)
    if len(email) > 254:
        return JSONResponse({"ok": False, "error": "Email address is too long"}, status_code=400)
    if len(new_password) > 1000:
        return JSONResponse({"ok": False, "error": "Password is too long"}, status_code=400)
    if not username.strip():
        return JSONResponse({"ok": False, "error": "Username is required"}, status_code=400)
    email_clean = normalize_email(email)
    if not email_clean or not is_semantic_email(email_clean):
        return JSONResponse({"ok": False, "error": "A valid email address is required"}, status_code=400)
    pw_ok, pw_err = _password_meets_complexity(new_password)
    if not pw_ok:
        return JSONResponse({"ok": False, "error": pw_err}, status_code=400)
    if new_password != confirm_password:
        return JSONResponse({"ok": False, "error": "Passwords do not match"}, status_code=400)
    with get_db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM physicians WHERE username = ? COLLATE NOCASE", (username.strip(),)
        ).fetchone()
        if existing:
            return JSONResponse({"ok": False, "error": "Username already taken"}, status_code=400)
    pw_hash = _hash_password(new_password)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO physicians (username, email, password_hash) VALUES (?, ?, ?)",
            (username.strip(), email_clean, pw_hash),
        )
        conn.commit()
    resp = JSONResponse({"ok": True, "redirect": "/physician"})
    _set_physician_cookie(resp, request, username.strip(), pw_hash)
    return resp


@router.get("/physician/login", response_class=HTMLResponse)
def physician_login_get(request: Request):
    if _get_authenticated_physician(request):
        return RedirectResponse(url="/physician", status_code=303)
    return f"""<!DOCTYPE html>
<html>
<head>{_PHYSICIAN_STYLE}<title>Physician Login</title></head>
<body>
  <div class="ph-nav">
    <span class="ph-nav-title">Physician Portal</span>
  </div>
  <div class="container">
    <h1>Physician Login</h1>
    <div class="card">
      <form method="post" action="/physician/login" data-ajax>
        <div class="form-error" style="display:none"></div>
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
    </div>
    <p style="margin-top:16px; font-size:13px; color:#6b7280;">
      <a href="/physician/forgot-password" style="color:#1e3a8a;">Forgot your password?</a>
      &nbsp;&middot;&nbsp;
      <a href="/physician/forgot-username" style="color:#1e3a8a;">Forgot your username?</a>
    </p>
    <p style="margin-top:8px; font-size:13px; color:#6b7280;">
      No account? <a href="/physician/signup" style="color:#1e3a8a;">Sign up</a>
    </p>
    <p style="margin-top:8px; font-size:13px; color:#6b7280;">
      Not a physician? <a href="/login" style="color:#1e3a8a;">Patient login</a>
    </p>
  </div>
</body>
</html>
"""


@router.post("/physician/login")
def physician_login_post(
    request: Request, username: str = Form(""), password: str = Form("")
):
    ip = request.client.host if request.client else "unknown"
    if not _is_physician_login_allowed(ip):
        return JSONResponse({"ok": False, "error": "Too many attempts. Please wait."}, status_code=429)
    if len(username) > 60 or len(password) > 1000:
        return JSONResponse({"ok": False, "error": "Incorrect username or password"}, status_code=400)
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM physicians WHERE username = ? COLLATE NOCASE", (username.strip(),)
        ).fetchone()
    if not row or not _verify_password(password, row["password_hash"]):
        return JSONResponse({"ok": False, "error": "Incorrect username or password"}, status_code=400)
    resp = JSONResponse({"ok": True, "redirect": "/physician"})
    _set_physician_cookie(resp, request, row["username"], row["password_hash"])
    return resp


@router.post("/physician/logout")
def physician_logout():
    resp = JSONResponse({"ok": True, "redirect": "/physician/login"})
    resp.delete_cookie(PHYSICIAN_COOKIE_NAME)
    resp.delete_cookie(PHYSICIAN_CTX_COOKIE)
    return resp


@router.get("/physician/forgot-password", response_class=HTMLResponse)
def physician_forgot_password_get():
    return f"""<!DOCTYPE html>
<html>
<head>{_PHYSICIAN_STYLE}<title>Forgot Password</title></head>
<body>
  <div class="ph-nav">
    <span class="ph-nav-title">Physician Portal</span>
  </div>
  <div class="container">
    <h1>Forgot Password</h1>
    <p style="color:#555; font-size:14px; margin-bottom:16px;">
      Enter the email address associated with your physician account and we'll send you a reset link.
    </p>
    <div class="card">
      <form method="post" action="/physician/forgot-password" data-ajax>
        <div class="form-error" style="display:none"></div>
        <div class="form-group">
          <label for="email">Email address</label>
          <input type="email" id="email" name="email" required autocomplete="email"
            placeholder="you@example.com">
        </div>
        <button type="submit" class="btn-primary">Send Reset Link</button>
      </form>
    </div>
    <p style="margin-top:16px; font-size:13px; color:#6b7280;">
      <a href="/physician/login" style="color:#1e3a8a;">&larr; Back to login</a>
    </p>
  </div>
</body>
</html>
"""


@router.post("/physician/forgot-password")
def physician_forgot_password_post(request: Request, email: str = Form("")):
    ip = request.client.host if request.client else "unknown"
    _TOAST = "If that email address is registered, a password reset link has been sent."
    if not _is_reset_allowed(ip):
        return JSONResponse({"ok": True, "toast": _TOAST})
    if len(email) > 254:
        return JSONResponse({"ok": True, "toast": _TOAST})
    email_clean = normalize_email(email)
    if not email_clean or not is_semantic_email(email_clean):
        return JSONResponse({"ok": True, "toast": _TOAST})
    with get_db() as conn:
        physician = conn.execute(
            "SELECT id FROM physicians WHERE LOWER(email) = ?", (email_clean,)
        ).fetchone()
    if physician:
        token = secrets.token_urlsafe(32)
        expires_at = int(time()) + RESET_TOKEN_TTL_SECONDS
        with get_db() as conn:
            conn.execute("DELETE FROM physician_reset_tokens WHERE physician_id = ?", (physician["id"],))
            conn.execute(
                "INSERT INTO physician_reset_tokens (token, physician_id, expires_at) VALUES (?, ?, ?)",
                (_hash_token(token), physician["id"], expires_at),
            )
            conn.commit()
        base = str(request.base_url).rstrip("/")
        reset_url = f"{base}/physician/reset-password?token={token}"
        _send_reset_email(email_clean, reset_url)
    return JSONResponse({"ok": True, "toast": _TOAST})


@router.get("/physician/reset-password", response_class=HTMLResponse)
def physician_reset_password_get(token: str = ""):
    if not token:
        return RedirectResponse(url="/physician/forgot-password", status_code=303)
    with get_db() as conn:
        row = conn.execute(
            "SELECT physician_id, expires_at FROM physician_reset_tokens WHERE token = ?",
            (_hash_token(token),),
        ).fetchone()
    if not row or row["expires_at"] < int(time()):
        return f"""<!DOCTYPE html>
<html>
<head>{_PHYSICIAN_STYLE}<title>Reset Password</title></head>
<body>
  <div class="ph-nav"><span class="ph-nav-title">Physician Portal</span></div>
  <div class="container">
    <h1>Reset Password</h1>
    <div class="alert">This reset link has expired or is invalid. Please
      <a href="/physician/forgot-password" style="color:#b91c1c;">request a new one</a>.
    </div>
  </div>
</body>
</html>
"""
    return f"""<!DOCTYPE html>
<html>
<head>{_PHYSICIAN_STYLE}<title>Reset Password</title></head>
<body>
  <div class="ph-nav"><span class="ph-nav-title">Physician Portal</span></div>
  <div class="container">
    <h1>Reset Password</h1>
    <div class="card">
      <form method="post" action="/physician/reset-password" data-ajax>
        <div class="form-error" style="display:none"></div>
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
  </div>
</body>
</html>
"""


@router.post("/physician/reset-password")
def physician_reset_password_post(
    token: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    if not token:
        return JSONResponse({"ok": False, "error": "Missing reset token"}, status_code=400)
    if len(token) > 200:
        return JSONResponse({"ok": False, "error": "Reset link expired or invalid"}, status_code=400)
    if len(new_password) > 1000:
        return JSONResponse({"ok": False, "error": "Password is too long"}, status_code=400)
    with get_db() as conn:
        row = conn.execute(
            "SELECT physician_id, expires_at FROM physician_reset_tokens WHERE token = ?",
            (_hash_token(token),),
        ).fetchone()
    if not row or row["expires_at"] < int(time()):
        return JSONResponse({"ok": False, "error": "Reset link expired or invalid"}, status_code=400)
    pw_ok, pw_err = _password_meets_complexity(new_password)
    if not pw_ok:
        return JSONResponse({"ok": False, "error": pw_err}, status_code=400)
    if new_password != confirm_password:
        return JSONResponse({"ok": False, "error": "Passwords do not match"}, status_code=400)
    new_hash = _hash_password(new_password)
    with get_db() as conn:
        conn.execute(
            "UPDATE physicians SET password_hash = ? WHERE id = ?",
            (new_hash, row["physician_id"]),
        )
        conn.execute("DELETE FROM physician_reset_tokens WHERE token = ?", (_hash_token(token),))
        conn.commit()
    return JSONResponse({"ok": True, "toast": "Password updated. Please log in.", "redirect": "/physician/login"})


@router.get("/physician/forgot-username", response_class=HTMLResponse)
def physician_forgot_username_get():
    return f"""<!DOCTYPE html>
<html>
<head>{_PHYSICIAN_STYLE}<title>Forgot Username</title></head>
<body>
  <div class="ph-nav">
    <span class="ph-nav-title">Physician Portal</span>
  </div>
  <div class="container">
    <h1>Forgot Username</h1>
    <p style="color:#555; font-size:14px; margin-bottom:16px;">
      Enter the email address you signed up with and we'll send your username to your inbox.
    </p>
    <div class="card">
      <form method="post" action="/physician/forgot-username" data-ajax>
        <div class="form-error" style="display:none"></div>
        <div class="form-group">
          <label for="email">Email address</label>
          <input type="email" id="email" name="email" required autocomplete="email"
            placeholder="you@example.com">
        </div>
        <button type="submit" class="btn-primary">Send Username</button>
      </form>
    </div>
    <p style="margin-top:16px; font-size:13px; color:#6b7280;">
      <a href="/physician/login" style="color:#1e3a8a;">&larr; Back to login</a>
    </p>
  </div>
</body>
</html>
"""


@router.post("/physician/forgot-username")
def physician_forgot_username_post(email: str = Form("")):
    _TOAST = "If that email address is registered, your username has been sent to your inbox."
    if len(email) > 254:
        return JSONResponse({"ok": True, "toast": _TOAST})
    email_clean = normalize_email(email)
    if email_clean and is_semantic_email(email_clean):
        with get_db() as conn:
            row = conn.execute(
                "SELECT username FROM physicians WHERE LOWER(email) = ?", (email_clean,)
            ).fetchone()
        if row and row["username"]:
            _send_username_reminder_email(email_clean, row["username"])
    return JSONResponse({"ok": True, "toast": _TOAST})


@router.get("/physician", response_class=HTMLResponse)
def physician_dashboard(request: Request):
    physician = _get_authenticated_physician(request)
    if not physician:
        return RedirectResponse(url="/physician/login", status_code=303)
    with get_db() as conn:
        patients = conn.execute(
            """SELECT up.id, up.name, up.dob, up.conditions
               FROM user_profile up
               JOIN physician_patients pp ON pp.patient_id = up.id
               WHERE pp.physician_id = ?
               ORDER BY up.name""",
            (physician["id"],),
        ).fetchall()

    ai_on = _ai_configured()
    patient_cards = ""
    for pt in patients:
        age = _calc_age(pt["dob"])
        age_str = f", {age}y" if age else ""
        cond_str = html.escape(pt["conditions"] or "—")
        digest_btn = (
            f'<button class="btn-digest" onclick="loadDigest({pt["id"]}, this)">'
            f'&#129504; Digest</button>'
        ) if ai_on else ""
        patient_cards += f"""
        <div class="card">
          <div class="patient-row">
            <div class="patient-info">
              <div class="patient-name">{html.escape(pt["name"] or "Unnamed patient")}</div>
              <div class="patient-meta">{cond_str}{age_str}</div>
            </div>
            {digest_btn}
            <form method="post" action="/physician/switch/{pt['id']}" style="margin:0;" data-ajax>
              <button class="btn-view" type="submit">View &rarr;</button>
            </form>
            <form method="post" action="/physician/patients/remove" style="margin:0;" data-ajax>
              <input type="hidden" name="patient_id" value="{pt['id']}">
              <button class="btn-remove" type="submit"
                onclick="return confirm('Remove this patient from your list?')">Remove</button>
            </form>
          </div>
          <div class="digest-box" id="digest-{pt['id']}" style="display:none;"></div>
        </div>"""

    if not patient_cards:
        patient_cards = "<p class='empty'>No patients yet. Add one using their share code below.</p>"

    return f"""<!DOCTYPE html>
<html>
<head>{_PHYSICIAN_STYLE}<title>Physician Portal</title></head>
<body>
  <div class="ph-nav">
    <span class="ph-nav-title">Physician Portal</span>
    <span class="ph-nav-sub">Logged in as <strong>{html.escape(physician["username"])}</strong></span>
    <form method="post" action="/physician/logout" style="margin:0;" data-ajax>
      <button class="ph-btn-logout" type="submit">Log Out</button>
    </form>
  </div>
  <script>
    function loadDigest(patientId, btn) {{
      var box = document.getElementById('digest-' + patientId);
      if (box.style.display !== 'none') {{ box.style.display = 'none'; return; }}
      box.style.display = 'block';
      box.textContent = 'Generating\u2026';
      btn.disabled = true;
      fetch('/physician/patients/' + patientId + '/digest')
        .then(function(r) {{ return r.json(); }})
        .then(function(data) {{
          box.textContent = data.ok ? data.digest : ('Error: ' + (data.error || 'Unknown error'));
        }})
        .catch(function() {{ box.textContent = 'Network error. Please try again.'; }})
        .finally(function() {{ btn.disabled = false; }});
    }}
  </script>
  <div class="container">
    <h1>My Patients</h1>
    {patient_cards}
    <div class="card" style="margin-top:28px;">
      <h2 style="font-size:16px; margin:0 0 12px;">Add a Patient</h2>
      <p style="font-size:13px; color:#555; margin:0 0 14px;">
        Ask your patient for their share code (shown on their Profile page).
      </p>
      <form method="post" action="/physician/patients/add" data-ajax>
        <div class="form-error" style="display:none"></div>
        <div style="display:flex; gap:10px; align-items:flex-end;">
          <div style="flex:1;">
            <label for="share_code" style="font-size:13px; font-weight:600; display:block; margin-bottom:6px;">Share Code</label>
            <input type="text" id="share_code" name="share_code"
              placeholder="e.g. A3F2C1D0"
              style="text-transform:uppercase; letter-spacing:2px; font-size:15px; width:100%; box-sizing:border-box;
                     border:1px solid #d1d5db; border-radius:6px; padding:8px 10px; font-family:inherit;"
              required>
          </div>
          <button type="submit" class="btn-primary" style="padding:10px 20px; white-space:nowrap;">Add Patient</button>
        </div>
      </form>
    </div>
  </div>
</body>
</html>
"""


@router.post("/physician/patients/add")
def physician_patients_add(request: Request, share_code: str = Form("")):
    physician = _get_authenticated_physician(request)
    if not physician:
        return RedirectResponse(url="/physician/login", status_code=303)
    ip = request.client.host if request.client else "unknown"
    if not _is_share_code_allowed(ip):
        return JSONResponse({"ok": False, "error": "Too many attempts. Please wait before trying again."}, status_code=429)
    code = share_code.strip().upper()
    if not code:
        return JSONResponse({"ok": False, "error": "Share code is required"}, status_code=400)
    if len(code) != 8 or not all(c in "0123456789ABCDEF" for c in code):
        return JSONResponse({"ok": False, "error": "Invalid share code format"}, status_code=400)
    with get_db() as conn:
        patient = conn.execute(
            "SELECT id FROM user_profile WHERE share_code = ?", (code,)
        ).fetchone()
    if not patient:
        return JSONResponse({"ok": False, "error": "No patient found with that share code"}, status_code=400)
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO physician_patients (physician_id, patient_id) VALUES (?, ?)",
            (physician["id"], patient["id"]),
        )
        conn.commit()
    return JSONResponse({"ok": True, "toast": "Patient added successfully", "reload": True})


@router.post("/physician/patients/remove")
def physician_patients_remove(request: Request, patient_id: int = Form(...)):
    physician = _get_authenticated_physician(request)
    if not physician:
        return RedirectResponse(url="/physician/login", status_code=303)
    with get_db() as conn:
        conn.execute(
            "DELETE FROM physician_patients WHERE physician_id = ? AND patient_id = ?",
            (physician["id"], patient_id),
        )
        conn.commit()
    return JSONResponse({"ok": True, "reload": True})


@router.post("/physician/switch/{patient_id}")
def physician_switch(request: Request, patient_id: int):
    physician = _get_authenticated_physician(request)
    if not physician:
        return RedirectResponse(url="/physician/login", status_code=303)
    if not _physician_owns_patient(physician["id"], patient_id):
        return RedirectResponse(url="/physician", status_code=303)
    accessed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO physician_access_log (physician_id, patient_id, accessed_at) VALUES (?, ?, ?)",
            (physician["id"], patient_id, accessed_at),
        )
        conn.commit()
    resp = JSONResponse({"ok": True, "redirect": "/symptoms/chart"})
    resp.set_cookie(
        PHYSICIAN_CTX_COOKIE,
        str(patient_id),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


@router.post("/physician/exit")
def physician_exit():
    resp = JSONResponse({"ok": True, "redirect": "/physician"})
    resp.delete_cookie(PHYSICIAN_CTX_COOKIE)
    return resp


@router.get("/physician/patients/{patient_id}/digest")
def physician_patient_digest(request: Request, patient_id: int):
    """Return a one-sentence AI digest for a patient (physician-only)."""
    physician = _get_authenticated_physician(request)
    if not physician:
        return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    if not _physician_owns_patient(physician["id"], patient_id):
        return JSONResponse({"ok": False, "error": "Forbidden"}, status_code=403)
    digest = generate_physician_digest(patient_id)
    if digest is None:
        return JSONResponse({"ok": False, "error": "AI not configured or no data"}, status_code=503)
    return JSONResponse({"ok": True, "digest": digest})
