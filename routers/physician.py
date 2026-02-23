import html

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from config import PHYSICIAN_COOKIE_NAME, PHYSICIAN_CTX_COOKIE
from db import get_db
from security import (
    _hash_password,
    _verify_password,
    _set_physician_cookie,
    _get_authenticated_physician,
    _physician_owns_patient,
)
from ui import _calc_age

router = APIRouter()

_PHYSICIAN_STYLE = """
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
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
    .empty { color: #888; font-style: italic; margin-top: 12px; }
    .patient-row { display: flex; align-items: center; gap: 12px; }
    .patient-info { flex: 1; }
    .patient-name { font-weight: 700; font-size: 16px; }
    .patient-meta { font-size: 13px; color: #666; margin-top: 2px; }
  </style>
"""


@router.get("/physician/signup", response_class=HTMLResponse)
def physician_signup_get(error: str = ""):
    error_banner = f'<div class="alert">{html.escape(error)}</div>' if error else ""
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
    {error_banner}
    <div class="card">
      <form method="post" action="/physician/signup">
        <div class="form-group">
          <label for="username">Username</label>
          <input type="text" id="username" name="username" required autocomplete="username">
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
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    if not username.strip():
        return RedirectResponse(url="/physician/signup?error=Username+is+required", status_code=303)
    if len(new_password) < 8:
        return RedirectResponse(
            url="/physician/signup?error=Password+must+be+at+least+8+characters", status_code=303
        )
    if new_password != confirm_password:
        return RedirectResponse(url="/physician/signup?error=Passwords+do+not+match", status_code=303)
    with get_db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM physicians WHERE username = ?", (username.strip(),)
        ).fetchone()
        if existing:
            return RedirectResponse(url="/physician/signup?error=Username+already+taken", status_code=303)
    pw_hash = _hash_password(new_password)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO physicians (username, password_hash) VALUES (?, ?)",
            (username.strip(), pw_hash),
        )
        conn.commit()
    resp = RedirectResponse(url="/physician", status_code=303)
    _set_physician_cookie(resp, request, username.strip(), pw_hash)
    return resp


@router.get("/physician/login", response_class=HTMLResponse)
def physician_login_get(request: Request, error: str = ""):
    if _get_authenticated_physician(request):
        return RedirectResponse(url="/physician", status_code=303)
    error_banner = f'<div class="alert">{html.escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html>
<head>{_PHYSICIAN_STYLE}<title>Physician Login</title></head>
<body>
  <div class="ph-nav">
    <span class="ph-nav-title">Physician Portal</span>
  </div>
  <div class="container">
    <h1>Physician Login</h1>
    {error_banner}
    <div class="card">
      <form method="post" action="/physician/login">
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
      No account? <a href="/physician/signup" style="color:#1e3a8a;">Sign up</a>
    </p>
  </div>
</body>
</html>
"""


@router.post("/physician/login")
def physician_login_post(
    request: Request, username: str = Form(""), password: str = Form("")
):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM physicians WHERE username = ?", (username.strip(),)
        ).fetchone()
    if not row or not _verify_password(password, row["password_hash"]):
        return RedirectResponse(
            url="/physician/login?error=Incorrect+username+or+password", status_code=303
        )
    resp = RedirectResponse(url="/physician", status_code=303)
    _set_physician_cookie(resp, request, row["username"], row["password_hash"])
    return resp


@router.post("/physician/logout")
def physician_logout():
    resp = RedirectResponse(url="/physician/login", status_code=303)
    resp.delete_cookie(PHYSICIAN_COOKIE_NAME)
    resp.delete_cookie(PHYSICIAN_CTX_COOKIE)
    return resp


@router.get("/physician", response_class=HTMLResponse)
def physician_dashboard(request: Request, error: str = "", success: str = ""):
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

    patient_cards = ""
    for pt in patients:
        age = _calc_age(pt["dob"])
        age_str = f", {age}y" if age else ""
        cond_str = html.escape(pt["conditions"] or "—")
        patient_cards += f"""
        <div class="card">
          <div class="patient-row">
            <div class="patient-info">
              <div class="patient-name">{html.escape(pt["name"] or "Unnamed patient")}</div>
              <div class="patient-meta">{cond_str}{age_str}</div>
            </div>
            <form method="post" action="/physician/switch/{pt['id']}" style="margin:0;">
              <button class="btn-view" type="submit">View &rarr;</button>
            </form>
            <form method="post" action="/physician/patients/remove" style="margin:0;">
              <input type="hidden" name="patient_id" value="{pt['id']}">
              <button class="btn-remove" type="submit"
                onclick="return confirm('Remove this patient from your list?')">Remove</button>
            </form>
          </div>
        </div>"""

    if not patient_cards:
        patient_cards = "<p class='empty'>No patients yet. Add one using their share code below.</p>"

    error_banner = f'<div class="alert">{html.escape(error)}</div>' if error else ""
    success_banner = (
        f'<div style="background:#dcfce7; border:1px solid #86efac; color:#15803d; border-radius:6px;'
        f' padding:10px 14px; margin-bottom:16px; font-size:14px;">{html.escape(success)}</div>'
    ) if success else ""

    return f"""<!DOCTYPE html>
<html>
<head>{_PHYSICIAN_STYLE}<title>Physician Portal</title></head>
<body>
  <div class="ph-nav">
    <span class="ph-nav-title">Physician Portal</span>
    <span class="ph-nav-sub">Logged in as <strong>{html.escape(physician["username"])}</strong></span>
    <form method="post" action="/physician/logout" style="margin:0;">
      <button class="ph-btn-logout" type="submit">Log Out</button>
    </form>
  </div>
  <div class="container">
    <h1>My Patients</h1>
    {error_banner}
    {success_banner}
    {patient_cards}
    <div class="card" style="margin-top:28px;">
      <h2 style="font-size:16px; margin:0 0 12px;">Add a Patient</h2>
      <p style="font-size:13px; color:#555; margin:0 0 14px;">
        Ask your patient for their share code (shown on their Profile page).
      </p>
      <form method="post" action="/physician/patients/add" style="display:flex; gap:10px; align-items:flex-end;">
        <div style="flex:1;">
          <label for="share_code" style="font-size:13px; font-weight:600; display:block; margin-bottom:6px;">Share Code</label>
          <input type="text" id="share_code" name="share_code"
            placeholder="e.g. A3F2C1D0"
            style="text-transform:uppercase; letter-spacing:2px; font-size:15px; width:100%; box-sizing:border-box;
                   border:1px solid #d1d5db; border-radius:6px; padding:8px 10px; font-family:inherit;"
            required>
        </div>
        <button type="submit" class="btn-primary" style="padding:10px 20px; white-space:nowrap;">Add Patient</button>
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
    code = share_code.strip().upper()
    if not code:
        return RedirectResponse(url="/physician?error=Share+code+is+required", status_code=303)
    with get_db() as conn:
        patient = conn.execute(
            "SELECT id FROM user_profile WHERE share_code = ?", (code,)
        ).fetchone()
    if not patient:
        return RedirectResponse(url="/physician?error=No+patient+found+with+that+share+code", status_code=303)
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO physician_patients (physician_id, patient_id) VALUES (?, ?)",
            (physician["id"], patient["id"]),
        )
        conn.commit()
    return RedirectResponse(url="/physician?success=Patient+added+successfully", status_code=303)


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
    return RedirectResponse(url="/physician", status_code=303)


@router.post("/physician/switch/{patient_id}")
def physician_switch(request: Request, patient_id: int):
    physician = _get_authenticated_physician(request)
    if not physician:
        return RedirectResponse(url="/physician/login", status_code=303)
    if not _physician_owns_patient(physician["id"], patient_id):
        return RedirectResponse(url="/physician", status_code=303)
    resp = RedirectResponse(url="/symptoms/calendar", status_code=303)
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
    resp = RedirectResponse(url="/physician", status_code=303)
    resp.delete_cookie(PHYSICIAN_CTX_COOKIE)
    return resp
