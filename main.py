import hashlib
import hmac
import html
import os
import secrets
import sqlite3
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from math import sqrt
from pathlib import Path
from time import time
from typing import Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

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


def _load_secret_key() -> str:
    env_key = os.environ.get("APP_SECRET_KEY", "").strip()
    if env_key:
        return env_key
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_text(encoding="utf-8").strip()
    key = secrets.token_hex(32)
    SECRET_KEY_PATH.write_text(key, encoding="utf-8")
    return key


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sx = sy = 0.0
    for x, y in zip(xs, ys):
        dx, dy = x - mx, y - my
        cov += dx * dy
        sx += dx * dx
        sy += dy * dy
    den = sqrt(sx * sy)
    return round(cov / den, 2) if den != 0 else None


def _compute_correlations(rows):
    # rows are already daily averages: (name, date, avg_severity)
    avg = {}
    dates_by_name = defaultdict(set)
    names_set = set()
    for row in rows:
        name, date, sev = row["name"], row["date"], row["avg_severity"]
        avg[(name, date)] = sev
        dates_by_name[name].add(date)
        names_set.add(name)
    names = sorted(names_set)
    n = len(names)
    matrix: list[list] = [[None] * n for _ in range(n)]
    for k in range(n):
        matrix[k][k] = 1.0
    for i in range(n):
        for j in range(i + 1, n):          # upper triangle only
            a, b = names[i], names[j]
            common = list(dates_by_name[a] & dates_by_name[b])
            r = _pearson([avg[(a, d)] for d in common], [avg[(b, d)] for d in common])
            matrix[i][j] = matrix[j][i] = r  # mirror — exploit symmetry
    return names, matrix


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS symptoms (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT    NOT NULL,
                severity  INTEGER NOT NULL CHECK (severity BETWEEN 1 AND 10),
                notes     TEXT    NOT NULL DEFAULT '',
                timestamp TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS medications (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT    NOT NULL,
                dose      TEXT    NOT NULL DEFAULT '',
                notes     TEXT    NOT NULL DEFAULT '',
                timestamp TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS user_profile (
                id          INTEGER PRIMARY KEY,
                name        TEXT    NOT NULL DEFAULT '',
                dob         TEXT    NOT NULL DEFAULT '',
                conditions  TEXT    NOT NULL DEFAULT '',
                medications TEXT    NOT NULL DEFAULT ''
            )
        """)
        # Migrate: add columns if not present
        cols = [row[1] for row in conn.execute("PRAGMA table_info(user_profile)")]
        if "password_hash" not in cols:
            conn.execute(
                "ALTER TABLE user_profile ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''"
            )
        if "username" not in cols:
            conn.execute(
                "ALTER TABLE user_profile ADD COLUMN username TEXT NOT NULL DEFAULT ''"
            )
        if "photo_ext" not in cols:
            conn.execute(
                "ALTER TABLE user_profile ADD COLUMN photo_ext TEXT NOT NULL DEFAULT ''"
            )
        if "share_code" not in cols:
            conn.execute(
                "ALTER TABLE user_profile ADD COLUMN share_code TEXT NOT NULL DEFAULT ''"
            )
        # Migrate symptoms: add user_id column
        symp_cols = [row[1] for row in conn.execute("PRAGMA table_info(symptoms)")]
        if "user_id" not in symp_cols:
            conn.execute("ALTER TABLE symptoms ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
        # Migrate medications: add user_id column
        med_cols = [row[1] for row in conn.execute("PRAGMA table_info(medications)")]
        if "user_id" not in med_cols:
            conn.execute("ALTER TABLE medications ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
        # Physicians table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS physicians (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL
            )
        """)
        # Physician–patient junction table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS physician_patients (
                physician_id INTEGER NOT NULL REFERENCES physicians(id),
                patient_id   INTEGER NOT NULL REFERENCES user_profile(id),
                PRIMARY KEY (physician_id, patient_id)
            )
        """)
        # Generate share codes for any patient rows missing one
        for row in conn.execute("SELECT id FROM user_profile WHERE share_code = ''"):
            conn.execute(
                "UPDATE user_profile SET share_code = ? WHERE id = ?",
                (secrets.token_hex(4).upper(), row[0]),
            )
        conn.commit()


UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


init_db()

# Migrate legacy single-user photo (profile.{ext}) to per-user naming (profile_1.{ext})
for _ext in ["jpg", "png", "gif", "webp"]:
    _old_photo = UPLOAD_DIR / f"profile.{_ext}"
    _new_photo = UPLOAD_DIR / f"profile_1.{_ext}"
    if _old_photo.exists() and not _new_photo.exists():
        _old_photo.rename(_new_photo)

app = FastAPI()

PUBLIC_PATHS = {"/login", "/signup", "/logout"}


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


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if not _is_same_origin(request):
            if path.startswith("/api/"):
                return JSONResponse({"error": "forbidden"}, status_code=403)
            return RedirectResponse(url="/login?error=Forbidden+request", status_code=303)
        if path.startswith("/api/") and not _csrf_header_valid(request):
            return JSONResponse({"error": "forbidden"}, status_code=403)
    # Physician-only routes
    if path.startswith("/physician"):
        if path in {"/physician/login", "/physician/signup"}:
            return _ensure_csrf_cookie(request, await call_next(request))
        physician = _get_authenticated_physician(request)
        if not physician:
            return RedirectResponse(url="/physician/login", status_code=303)
        return _ensure_csrf_cookie(request, await call_next(request))

    # Patient public paths (login / signup / logout)
    if path in PUBLIC_PATHS:
        return _ensure_csrf_cookie(request, await call_next(request))

    # Check physician-in-patient-context
    physician = _get_authenticated_physician(request)
    if physician:
        ctx_cookie = request.cookies.get(PHYSICIAN_CTX_COOKIE, "")
        if ctx_cookie:
            try:
                patient_id = int(ctx_cookie)
            except ValueError:
                patient_id = None
            if patient_id and _physician_owns_patient(physician["id"], patient_id):
                with get_db() as conn:
                    patient = conn.execute(
                        "SELECT name FROM user_profile WHERE id = ?", (patient_id,)
                    ).fetchone()
                patient_name = (patient["name"] if patient and patient["name"] else "Patient")
                _current_user_id.set(patient_id)
                _physician_ctx.set(patient_name)
                return _ensure_csrf_cookie(request, await call_next(request))
        # Physician logged in but no valid patient ctx → send to portal
        return RedirectResponse(url="/physician", status_code=303)

    # Regular patient auth
    user = _get_authenticated_user(request)
    if not user:
        if not _has_any_patient():
            return RedirectResponse(url="/signup", status_code=303)
        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse(url="/login", status_code=303)
    _current_user_id.set(user["id"])
    return _ensure_csrf_cookie(request, await call_next(request))


@app.get("/")
def root():
    return RedirectResponse(url="/symptoms/calendar", status_code=303)



def _severity_color(s):
    if s <= 3: return "#22c55e"   # green
    if s <= 6: return "#eab308"   # yellow
    if s <= 8: return "#f97316"   # orange
    return "#ef4444"              # red


def _sidebar() -> str:
    uid = _current_user_id.get()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM user_profile WHERE id = ?", (uid,)).fetchone()
    p = dict(row) if row else {"name": "", "dob": "", "conditions": "", "medications": "", "photo_ext": ""}
    photo_ext = p.get("photo_ext", "")
    if photo_ext:
        avatar = (
            '<a href="/profile" style="display:block;text-decoration:none;">'
            '<img src="/profile/photo" alt="Profile photo"'
            ' style="width:80px;height:80px;border-radius:50%;object-fit:cover;'
            'border:3px solid #e5e7eb;display:block;margin:0 auto;"></a>'
        )
    else:
        avatar = (
            '<a href="/profile" style="display:block;text-decoration:none;">'
            '<div style="width:80px;height:80px;border-radius:50%;background:#e5e7eb;'
            'display:flex;align-items:center;justify-content:center;margin:0 auto;">'
            '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 24 24"'
            ' fill="none" stroke="#9ca3af" stroke-width="1.5">'
            '<circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/>'
            '</svg></div></a>'
        )
    age = _calc_age(p.get("dob", ""))
    age_str = f"{age}y" if age else ""
    name_esc = html.escape(p.get("name") or "")
    dob_esc = html.escape(p.get("dob") or "")
    cond_esc = html.escape(p.get("conditions") or "")
    meds_esc = html.escape(p.get("medications") or "")
    lbl = 'style="font-size:11px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.06em;display:block;margin-bottom:4px;"'
    inp = 'style="width:100%;box-sizing:border-box;border:1px solid #d1d5db;border-radius:6px;padding:6px 8px;font-size:13px;font-family:inherit;"'
    ta = 'style="width:100%;box-sizing:border-box;border:1px solid #d1d5db;border-radius:6px;padding:6px 8px;font-size:13px;font-family:inherit;resize:vertical;"'
    return f"""<aside class="sidebar">
  <div style="text-align:center;margin-bottom:16px;">{avatar}</div>
  <div id="sb-view">
    <p id="sb-name-v" style="font-weight:700;font-size:15px;margin:0 0 2px;text-align:center;">{name_esc or '<em style="color:#aaa;font-style:normal;">Your name</em>'}</p>
    <p id="sb-age-v" style="font-size:12px;color:#888;margin:0 0 12px;text-align:center;">{age_str}</p>
    <p {lbl}>Conditions</p>
    <p id="sb-cond-v" style="font-size:13px;color:#444;margin:0 0 12px;line-height:1.5;">{cond_esc or '<em style="color:#d1d5db;">—</em>'}</p>
    <p {lbl}>Medications</p>
    <p id="sb-meds-v" style="font-size:13px;color:#444;margin:0 0 16px;line-height:1.5;">{meds_esc or '<em style="color:#d1d5db;">—</em>'}</p>
    <button onclick="sbToggle(true)" style="width:100%;background:none;border:1px solid #e5e7eb;border-radius:6px;padding:6px;font-size:13px;color:#6b7280;cursor:pointer;font-family:inherit;">Edit profile</button>
  </div>
  <form id="sb-edit" style="display:none;" onsubmit="sbSave(event)">
    <div style="margin-bottom:10px;"><label {lbl}>Name</label>
      <input type="text" name="name" id="sb-name-i" value="{name_esc}" {inp}></div>
    <div style="margin-bottom:10px;"><label {lbl}>Date of Birth</label>
      <input type="date" name="dob" id="sb-dob-i" value="{dob_esc}" {inp}></div>
    <div style="margin-bottom:10px;"><label {lbl}>Conditions</label>
      <textarea name="conditions" id="sb-cond-i" rows="3" {ta}>{cond_esc}</textarea></div>
    <div style="margin-bottom:14px;"><label {lbl}>Medications</label>
      <textarea name="medications" id="sb-meds-i" rows="3" {ta}>{meds_esc}</textarea></div>
    <div style="display:flex;gap:6px;">
      <button type="submit" style="flex:1;background:#3b82f6;color:#fff;border:none;border-radius:6px;padding:7px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;">Save</button>
      <button type="button" onclick="sbToggle(false)" style="flex:1;background:none;border:1px solid #e5e7eb;border-radius:6px;padding:7px;font-size:13px;color:#6b7280;cursor:pointer;font-family:inherit;">Cancel</button>
    </div>
  </form>
</aside>
<script>
document.body.classList.add('has-sidebar');
(function(){{
  var nav = document.querySelector('nav');
  var sb  = document.querySelector('.sidebar');
  if (nav && sb) sb.style.top = nav.getBoundingClientRect().bottom + 'px';
}})();
function sbToggle(e){{document.getElementById('sb-view').style.display=e?'none':'';document.getElementById('sb-edit').style.display=e?'':'none';}}
function sbCookie(name){{
  const prefix = name + "=";
  return document.cookie.split(";").map(v=>v.trim()).find(v=>v.startsWith(prefix))?.slice(prefix.length) || "";
}}
function sbSetTextOrPlaceholder(elId, text, placeholderStyle, placeholderText){{
  const el = document.getElementById(elId);
  el.textContent = "";
  if (text) {{
    el.textContent = text;
    return;
  }}
  const ph = document.createElement("em");
  ph.setAttribute("style", placeholderStyle);
  ph.textContent = placeholderText;
  el.appendChild(ph);
}}
async function sbSave(e){{
  e.preventDefault();
  const fd=new FormData(document.getElementById('sb-edit'));
  await fetch('/api/profile',{{method:'POST',headers:{{'X-CSRF-Token':sbCookie('csrf_token')}},body:fd}});
  const name=(document.getElementById('sb-name-i').value||'').trim();
  const cond=(document.getElementById('sb-cond-i').value||'').trim();
  const meds=(document.getElementById('sb-meds-i').value||'').trim();
  const dob=document.getElementById('sb-dob-i').value;
  sbSetTextOrPlaceholder('sb-name-v', name, 'color:#aaa;font-style:normal;', 'Your name');
  sbSetTextOrPlaceholder('sb-cond-v', cond, 'color:#d1d5db;', '—');
  sbSetTextOrPlaceholder('sb-meds-v', meds, 'color:#d1d5db;', '—');
  if(dob){{const t=new Date(),d=new Date(dob+'T00:00:00');let a=t.getFullYear()-d.getFullYear();if(t<new Date(t.getFullYear(),d.getMonth(),d.getDate()))a--;document.getElementById('sb-age-v').textContent=a+'y';}}
  else{{document.getElementById('sb-age-v').textContent='';}}
  sbToggle(false);
}}
</script>"""


def _nav_bar(active: str = "") -> str:
    physician_banner = ""
    patient_name = _physician_ctx.get()
    if patient_name is not None:
        physician_banner = (
            '<div style="background:#fef9c3; border-bottom:1px solid #fde047; padding:6px 24px;'
            ' display:flex; align-items:center; justify-content:space-between; font-size:13px;">'
            f'<span>&#128104;&#8205;&#9877;&#65039; Physician view &mdash; <strong>{html.escape(patient_name)}</strong></span>'
            '<form method="post" action="/physician/exit" style="margin:0;">'
            '<button type="submit" style="background:#854d0e; color:#fff; border:none; border-radius:6px;'
            ' padding:4px 12px; font-size:13px; cursor:pointer; font-family:inherit;">'
            '&#8592; Exit to portal</button>'
            '</form>'
            '</div>'
        )
    def dlnk(href, label, key):
        """Desktop nav link with active underline indicator."""
        if active == key:
            s = "color:#fff; font-weight:600; border-bottom:2px solid rgba(255,255,255,0.8); padding-bottom:2px;"
        else:
            s = "color:rgba(255,255,255,0.7); font-weight:500;"
        return f'<a href="{href}" style="text-decoration:none; font-size:14px; {s}">{label}</a>'
    def mlnk(href, label, key):
        """Mobile dropdown link."""
        s = "color:#fff; font-weight:600;" if active == key else "color:rgba(255,255,255,0.85); font-weight:400;"
        return (
            f'<a href="{href}" style="text-decoration:none; font-size:15px;'
            f' padding:12px 0; border-bottom:1px solid rgba(255,255,255,0.1); {s}">{label}</a>'
        )
    return (
        physician_banner
        + '<nav style="background:#1e3a8a;">'
        # ── Desktop row ───────────────────────────────────────────────────
        '<div style="padding:0 24px; height:52px; display:flex; align-items:center; gap:20px;">'
        '<span style="font-weight:800; color:#fff; font-size:15px; flex-shrink:0; margin-right:8px;">'
        'Symptom Tracker</span>'
        '<div class="nav-desktop-links">'
        + dlnk("/symptoms/calendar", "Calendar", "calendar")
        + dlnk("/symptoms", "List", "list")
        + dlnk("/symptoms/chart", "Chart", "chart")
        + dlnk("/medications", "Meds", "meds")
        + '</div>'
        '<div class="nav-desktop-actions">'
        '<a href="/symptoms/new" style="background:#fff; color:#1e3a8a; text-decoration:none;'
        ' font-size:13px; font-weight:700; padding:6px 14px; border-radius:20px; white-space:nowrap;">'
        '+ Log Symptom</a>'
        '<a href="/medications/new" style="background:#a855f7; color:#fff; text-decoration:none;'
        ' font-size:13px; font-weight:700; padding:6px 14px; border-radius:20px; white-space:nowrap;">'
        '+ Log Medication</a>'
        + dlnk("/profile", "Profile", "profile")
        + '<form method="post" action="/logout" style="margin:0;">'
        '<button type="submit" style="background:transparent; border:1px solid rgba(255,255,255,0.4);'
        ' color:rgba(255,255,255,0.7); border-radius:6px; padding:4px 12px;'
        ' font-size:13px; cursor:pointer; font-family:inherit;">Log Out</button>'
        '</form>'
        '</div>'
        # Hamburger (hidden on desktop, shown on mobile via CSS)
        '<button class="nav-hamburger" id="nav-toggle" aria-label="Open menu"'
        ' onclick="_navToggle()">&#9776;</button>'
        '</div>'
        # ── Mobile dropdown ───────────────────────────────────────────────
        '<div id="nav-menu">'
        + mlnk("/symptoms/calendar", "Calendar", "calendar")
        + mlnk("/symptoms", "List", "list")
        + mlnk("/symptoms/chart", "Chart", "chart")
        + mlnk("/medications", "Meds", "meds")
        + mlnk("/profile", "Profile", "profile")
        + '<div style="display:flex; gap:8px; flex-wrap:wrap; padding:12px 0 4px;">'
        '<a href="/symptoms/new" style="background:#fff; color:#1e3a8a; text-decoration:none;'
        ' font-size:13px; font-weight:700; padding:7px 14px; border-radius:20px; white-space:nowrap;">'
        '+ Log Symptom</a>'
        '<a href="/medications/new" style="background:#a855f7; color:#fff; text-decoration:none;'
        ' font-size:13px; font-weight:700; padding:7px 14px; border-radius:20px; white-space:nowrap;">'
        '+ Log Medication</a>'
        '<form method="post" action="/logout" style="margin:0;">'
        '<button type="submit" style="background:transparent; border:1px solid rgba(255,255,255,0.4);'
        ' color:rgba(255,255,255,0.7); border-radius:6px; padding:6px 12px;'
        ' font-size:13px; cursor:pointer; font-family:inherit;">Log Out</button>'
        '</form>'
        '</div>'
        '</div>'
        '</nav>'
        '<script>function _navToggle(){'
        'var m=document.getElementById(\'nav-menu\');'
        'var b=document.getElementById(\'nav-toggle\');'
        'm.classList.toggle(\'open\');'
        'b.innerHTML=m.classList.contains(\'open\')?\'&#10005;\':\'&#9776;\';'
        '}</script>'
        + _sidebar()
    )


PAGE_STYLE = """
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body { font-family: system-ui, sans-serif; background: #f5f5f5; margin: 0; padding: 0; color: #222; }
    .container { max-width: 560px; margin: 0 auto; padding: 24px; }
    h1 { margin-bottom: 4px; }
    .card { background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px; margin: 12px 0; }
    .card-header { display: flex; align-items: center; gap: 10px; }
    .badge { display: inline-block; width: 36px; height: 36px; border-radius: 50%;
             color: #fff; font-weight: 700; font-size: 15px;
             display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
    .card-name { font-size: 17px; font-weight: 600; }
    .card-ts { font-size: 12px; color: #888; margin-top: 2px; }
    .card-notes { margin: 10px 0 0; font-size: 14px; color: #444; }
    .btn-delete { background: none; border: 1px solid #e0e0e0;
                  border-radius: 6px; padding: 4px 10px; font-size: 13px; color: #888;
                  cursor: pointer; }
    .btn-delete:hover { background: #fee2e2; border-color: #ef4444; color: #ef4444; }
    .btn-edit { font-size: 13px; color: #3b82f6; border: 1px solid #d1d5db;
                border-radius: 6px; padding: 4px 10px; text-decoration: none; display: inline-block; }
    .btn-edit:hover { background: #eff6ff; border-color: #3b82f6; }
    .btn-primary { background: #3b82f6; color: #fff; border: none; border-radius: 8px;
                   padding: 10px 22px; font-size: 15px; cursor: pointer; font-weight: 600; }
    .btn-primary:hover { background: #2563eb; }
    .btn-log { display: inline-block; background: #3b82f6; color: #fff; text-decoration: none;
               border-radius: 8px; padding: 8px 16px; font-size: 14px; font-weight: 600;
               margin-bottom: 8px; }
    .btn-log:hover { background: #2563eb; }
    .back { font-size: 14px; color: #3b82f6; text-decoration: none; }
    .back:hover { text-decoration: underline; }
    .form-group { margin-bottom: 20px; }
    label { display: block; font-weight: 600; font-size: 14px; margin-bottom: 6px; }
    input[type=text], input[type=password], input[type=date], input[type=datetime-local], textarea { width: 100%; box-sizing: border-box; border: 1px solid #d1d5db;
      border-radius: 6px; padding: 8px 10px; font-size: 15px; font-family: inherit; }
    input[type=text]:focus, input[type=password]:focus, input[type=date]:focus, input[type=datetime-local]:focus, textarea:focus { outline: 2px solid #3b82f6; border-color: transparent; }
    .slider-row { display: flex; align-items: center; gap: 14px; }
    input[type=range] { flex: 1; accent-color: #3b82f6; height: 6px; cursor: pointer; }
    .sev-badge { width: 42px; height: 42px; border-radius: 50%; color: #fff; font-weight: 700;
                 font-size: 18px; display: flex; align-items: center; justify-content: center;
                 flex-shrink: 0; transition: background 0.2s; }
    .sev-labels { display: flex; justify-content: space-between; font-size: 11px; color: #888;
                  margin-top: 4px; }
    .alert { background: #fee2e2; border: 1px solid #fca5a5; color: #b91c1c;
             border-radius: 6px; padding: 10px 14px; margin-bottom: 16px; font-size: 14px; }
    .empty { color: #888; font-style: italic; margin-top: 16px; }
    .btn-primary.med-submit { background: #7c3aed; }
    .btn-primary.med-submit:hover { background: #6d28d9; }
    /* ── Nav responsive ────────────────────────────────────────────────── */
    .nav-desktop-links { flex: 1; display: flex; gap: 20px; }
    .nav-desktop-actions { display: flex; align-items: center; gap: 16px; flex-shrink: 0; }
    .nav-hamburger { display: none; background: none; border: none; color: #fff;
                     font-size: 22px; cursor: pointer; padding: 4px 8px; line-height: 1;
                     margin-left: auto; }
    #nav-menu { display: none; flex-direction: column;
                padding: 4px 24px 16px; background: #1e3a8a;
                border-top: 1px solid rgba(255,255,255,0.15); }
    #nav-menu.open { display: flex; }
    @media (max-width: 640px) {
      .nav-desktop-links  { display: none; }
      .nav-desktop-actions { display: none; }
      .nav-hamburger { display: block; }
      .container { padding: 16px; }
    }
    /* ── Profile sidebar ────────────────────────────────────────────── */
    .sidebar {
      position: fixed; left: 0; top: 52px; bottom: 0; width: 220px;
      background: #fff; border-right: 1px solid #e5e7eb;
      overflow-y: auto; padding: 20px 16px; box-sizing: border-box; z-index: 5;
    }
    body.has-sidebar .container { margin-left: 236px; margin-right: 0; }
    @media (max-width: 900px) {
      .sidebar { display: none; }
      body.has-sidebar .container { margin-left: auto; margin-right: auto; }
    }
  </style>
"""


@app.get("/signup", response_class=HTMLResponse)
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


@app.post("/signup")
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


@app.get("/login", response_class=HTMLResponse)
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


@app.post("/login")
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


@app.post("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp


@app.get("/symptoms", response_class=HTMLResponse)
def symptoms_list():
    uid = _current_user_id.get()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, severity, notes, timestamp FROM symptoms"
            " WHERE user_id = ? ORDER BY timestamp DESC",
            (uid,),
        ).fetchall()

    # Group by symptom name, preserving order of most recently logged per group
    groups: dict[str, list] = {}
    for row in rows:
        n = row["name"]
        if n not in groups:
            groups[n] = []
        groups[n].append(row)

    if groups:
        sections = ""
        for name, entries in groups.items():
            cards = "".join(
                f"""
            <div class="card">
              <div class="card-header">
                <div class="badge" style="background:{_severity_color(e['severity'])}">{e['severity']}</div>
                <div>
                  <div class="card-name">{html.escape(e['name'])}</div>
                  <div class="card-ts">{html.escape(e['timestamp'])}</div>
                </div>
              </div>
              {"<p class='card-notes'>" + html.escape(e['notes']) + "</p>" if e['notes'] else ""}
              <div style="display:flex; gap:8px; align-items:center; margin-top:10px;">
                <a href="/symptoms/{e['id']}/edit" class="btn-edit">Edit</a>
                <form method="post" action="/symptoms/delete" style="margin:0;">
                  <input type="hidden" name="id" value="{e['id']}">
                  <button class="btn-delete" type="submit"
                    onclick="return confirm('Delete this symptom entry?')">Delete</button>
                </form>
              </div>
            </div>
            """
                for e in entries
            )
            count = len(entries)
            label = "entry" if count == 1 else "entries"
            sections += f"""
        <div class="sym-group">
          <div class="sym-group-header">
            <span class="sym-group-name">{html.escape(name)}</span>
            <span class="sym-count">{count} {label}</span>
          </div>
          {cards}
        </div>
        """
    else:
        sections = "<p class='empty'>No symptoms logged yet.</p>"

    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}
  <style>
    .sym-group {{ margin-bottom: 28px; }}
    .sym-group-header {{ display: flex; align-items: baseline; gap: 10px;
                         border-bottom: 2px solid #e5e7eb; padding-bottom: 8px; margin-top: 20px; }}
    .sym-group-name {{ font-size: 18px; font-weight: 700; color: #111; }}
    .sym-count {{ font-size: 13px; color: #9ca3af; }}
  </style>
</head>
<body>
  {_nav_bar('list')}
  <div class="container">
    <h1>Symptom Log</h1>
    {sections}
  </div>
</body>
</html>
"""


@app.get("/symptoms/new", response_class=HTMLResponse)
def symptoms_new(error: str = ""):
    error_html = f'<div class="alert">{html.escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}</head>
<body>
  {_nav_bar('new')}
  <div class="container">
    <h1>Log a Symptom</h1>
    {error_html}
    <div class="card">
      <form method="post" action="/symptoms">

        <div class="form-group">
          <label for="name">Symptom name <span style="color:#ef4444">*</span></label>
          <input type="text" id="name" name="name" placeholder="e.g. Headache" required
                 list="symptom-suggestions" autocomplete="off">
          <datalist id="symptom-suggestions">
            <option value="Headache"><option value="Migraine"><option value="Nausea">
            <option value="Fatigue"><option value="Dizziness"><option value="Chest pain">
            <option value="Shortness of breath"><option value="Fever"><option value="Chills">
            <option value="Cough"><option value="Sore throat"><option value="Back pain">
            <option value="Joint pain"><option value="Stomach ache"><option value="Anxiety">
          </datalist>
        </div>

        <div class="form-group">
          <label for="severity">Severity <span style="color:#ef4444">*</span></label>
          <div class="slider-row">
            <input type="range" id="severity" name="severity" min="1" max="10" value="5"
                   oninput="updateSeverity(this.value)">
            <div class="sev-badge" id="sev-badge" style="background:#eab308">5</div>
          </div>
          <div class="sev-labels"><span>1 — Mild</span><span>10 — Severe</span></div>
        </div>

        <div class="form-group">
          <label for="notes">Notes <span style="color:#aaa;font-weight:400">(optional)</span></label>
          <textarea id="notes" name="notes" rows="3" placeholder="Any additional details..."></textarea>
        </div>

        <div class="form-group">
          <label for="symptom_date">Date &amp; time <span style="color:#aaa;font-weight:400">(defaults to now)</span></label>
          <input type="datetime-local" id="symptom_date" name="symptom_date" required
                 style="width:auto;">
        </div>

        <button class="btn-primary" type="submit">Save Symptom</button>
      </form>
    </div>
  </div>

  <script>
    const colors = {{1:"#22c55e",2:"#22c55e",3:"#22c55e",
                     4:"#eab308",5:"#eab308",6:"#eab308",
                     7:"#f97316",8:"#f97316",
                     9:"#ef4444",10:"#ef4444"}};
    function updateSeverity(v) {{
      const badge = document.getElementById("sev-badge");
      badge.textContent = v;
      badge.style.background = colors[+v];
      document.getElementById("severity").style.accentColor = colors[+v];
    }}
    updateSeverity(5);
    // Default the date picker to local "now" and cap max at now
    const now = new Date();
    const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
    const localStr = local.toISOString().slice(0, 16);
    document.getElementById("symptom_date").value = localStr;
    document.getElementById("symptom_date").max = localStr;
  </script>
</body>
</html>
"""


@app.post("/symptoms")
def symptoms_create(
    name: str = Form(...),
    severity: int = Form(...),
    notes: str = Form(""),
    symptom_date: str = Form(...),
):
    if not name.strip():
        return RedirectResponse(url="/symptoms/new?error=Symptom+name+is+required", status_code=303)
    if not (1 <= severity <= 10):
        return RedirectResponse(url="/symptoms/new?error=Severity+must+be+between+1+and+10", status_code=303)
    try:
        ts_dt = datetime.strptime(symptom_date, "%Y-%m-%dT%H:%M")
    except ValueError:
        return RedirectResponse(url="/symptoms/new?error=Invalid+date+format", status_code=303)
    if ts_dt > datetime.now():
        return RedirectResponse(url="/symptoms/new?error=Date+cannot+be+in+the+future", status_code=303)
    timestamp = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO symptoms (name, severity, notes, timestamp, user_id) VALUES (?, ?, ?, ?, ?)",
            (name.strip(), severity, notes.strip(), timestamp, uid),
        )
        conn.commit()
    return RedirectResponse(url="/symptoms", status_code=303)


@app.post("/symptoms/delete")
def symptoms_delete(id: int = Form(...)):
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute("DELETE FROM symptoms WHERE id = ? AND user_id = ?", (id, uid))
        conn.commit()
    return RedirectResponse(url="/symptoms", status_code=303)


@app.get("/symptoms/{sym_id}/edit", response_class=HTMLResponse)
def symptoms_edit_get(sym_id: int, error: str = ""):
    uid = _current_user_id.get()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM symptoms WHERE id = ? AND user_id = ?", (sym_id, uid)
        ).fetchone()
    if row is None:
        return RedirectResponse(url="/symptoms", status_code=303)
    e = dict(row)
    dt_local = e["timestamp"].replace(" ", "T")[:16]
    sev = e["severity"]
    error_html = f'<div class="alert">{html.escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}</head>
<body>
  {_nav_bar('list')}
  <div class="container">
    <h1>Edit Symptom</h1>
    {error_html}
    <div class="card">
      <form method="post" action="/symptoms/{sym_id}/edit">
        <div class="form-group">
          <label for="name">Symptom name <span style="color:#ef4444">*</span></label>
          <input type="text" id="name" name="name" value="{html.escape(e['name'])}"
                 required list="symptom-suggestions" autocomplete="off">
          <datalist id="symptom-suggestions">
            <option value="Headache"><option value="Migraine"><option value="Nausea">
            <option value="Fatigue"><option value="Dizziness"><option value="Chest pain">
            <option value="Shortness of breath"><option value="Fever"><option value="Chills">
            <option value="Cough"><option value="Sore throat"><option value="Back pain">
            <option value="Joint pain"><option value="Stomach ache"><option value="Anxiety">
          </datalist>
        </div>
        <div class="form-group">
          <label for="severity">Severity <span style="color:#ef4444">*</span></label>
          <div class="slider-row">
            <input type="range" id="severity" name="severity" min="1" max="10"
                   value="{sev}" oninput="updateSeverity(this.value)">
            <div class="sev-badge" id="sev-badge" style="background:{_severity_color(sev)}">{sev}</div>
          </div>
          <div class="sev-labels"><span>1 — Mild</span><span>10 — Severe</span></div>
        </div>
        <div class="form-group">
          <label for="notes">Notes <span style="color:#aaa;font-weight:400">(optional)</span></label>
          <textarea id="notes" name="notes" rows="3">{html.escape(e['notes'])}</textarea>
        </div>
        <div class="form-group">
          <label for="symptom_date">Date &amp; time</label>
          <input type="datetime-local" id="symptom_date" name="symptom_date"
                 value="{dt_local}" required style="width:auto;">
        </div>
        <div style="display:flex; gap:12px; align-items:center;">
          <button class="btn-primary" type="submit">Save Changes</button>
          <a href="/symptoms" class="back">Cancel</a>
        </div>
      </form>
    </div>
  </div>
  <script>
    const colors = {{1:"#22c55e",2:"#22c55e",3:"#22c55e",
                     4:"#eab308",5:"#eab308",6:"#eab308",
                     7:"#f97316",8:"#f97316",
                     9:"#ef4444",10:"#ef4444"}};
    function updateSeverity(v) {{
      const badge = document.getElementById("sev-badge");
      badge.textContent = v;
      badge.style.background = colors[+v];
      document.getElementById("severity").style.accentColor = colors[+v];
    }}
    updateSeverity({sev});
    // Cap max at current local time so future dates can't be selected
    const _now = new Date();
    const _local = new Date(_now.getTime() - _now.getTimezoneOffset() * 60000);
    document.getElementById("symptom_date").max = _local.toISOString().slice(0, 16);
  </script>
</body>
</html>"""


@app.post("/symptoms/{sym_id}/edit")
def symptoms_edit_post(
    sym_id: int,
    name: str = Form(...),
    severity: int = Form(...),
    notes: str = Form(""),
    symptom_date: str = Form(...),
):
    if not name.strip():
        return RedirectResponse(url=f"/symptoms/{sym_id}/edit?error=Symptom+name+is+required", status_code=303)
    if not (1 <= severity <= 10):
        return RedirectResponse(url=f"/symptoms/{sym_id}/edit?error=Severity+must+be+between+1+and+10", status_code=303)
    try:
        ts_dt = datetime.strptime(symptom_date, "%Y-%m-%dT%H:%M")
    except ValueError:
        return RedirectResponse(url=f"/symptoms/{sym_id}/edit?error=Invalid+date+format", status_code=303)
    if ts_dt > datetime.now():
        return RedirectResponse(url=f"/symptoms/{sym_id}/edit?error=Date+cannot+be+in+the+future", status_code=303)
    timestamp = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "UPDATE symptoms SET name = ?, severity = ?, notes = ?, timestamp = ?"
            " WHERE id = ? AND user_id = ?",
            (name.strip(), severity, notes.strip(), timestamp, sym_id, uid),
        )
        conn.commit()
    return RedirectResponse(url="/symptoms", status_code=303)


@app.get("/api/symptoms")
def api_symptoms():
    uid = _current_user_id.get()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, severity, notes, timestamp FROM symptoms"
            " WHERE user_id = ? ORDER BY timestamp ASC",
            (uid,),
        ).fetchall()
    return JSONResponse({"symptoms": [dict(r) for r in rows]})


@app.get("/api/symptoms/correlations")
def api_symptoms_correlations(from_date: str = "", to_date: str = ""):
    uid = _current_user_id.get()
    clauses: list[str] = ["user_id = ?"]
    params: list = [uid]
    if from_date:
        clauses.append("substr(timestamp, 1, 10) >= ?")
        params.append(from_date)
    if to_date:
        clauses.append("substr(timestamp, 1, 10) <= ?")
        params.append(to_date)
    where = "WHERE " + " AND ".join(clauses)
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT name, substr(timestamp, 1, 10) AS date, AVG(severity) AS avg_severity
            FROM symptoms {where}
            GROUP BY name, date
        """, params).fetchall()
    names, matrix = _compute_correlations(rows)
    return JSONResponse({"names": names, "matrix": matrix})


@app.get("/api/correlations/med-symptom")
def api_med_symptom_correlations(from_date: str = "", to_date: str = ""):
    uid = _current_user_id.get()
    clauses: list[str] = ["user_id = ?"]
    params: list = [uid]
    if from_date:
        clauses.append("substr(timestamp, 1, 10) >= ?")
        params.append(from_date)
    if to_date:
        clauses.append("substr(timestamp, 1, 10) <= ?")
        params.append(to_date)
    where = "WHERE " + " AND ".join(clauses)
    with get_db() as conn:
        symp_rows = conn.execute(f"""
            SELECT name, substr(timestamp, 1, 10) AS date, AVG(severity) AS avg_severity
            FROM symptoms {where} GROUP BY name, date
        """, params).fetchall()
        med_rows = conn.execute(f"""
            SELECT name, substr(timestamp, 1, 10) AS date, COUNT(*) AS cnt
            FROM medications {where} GROUP BY name, date
        """, params).fetchall()
    symp_avg = {(r["name"], r["date"]): r["avg_severity"] for r in symp_rows}
    dates_by_symp = defaultdict(set)
    for name, date in symp_avg:
        dates_by_symp[name].add(date)
    med_cnt = {(r["name"], r["date"]): r["cnt"] for r in med_rows}
    symp_names = sorted(dates_by_symp)
    med_names = sorted({r["name"] for r in med_rows})
    if not symp_names or not med_names:
        return JSONResponse({"med_names": [], "symp_names": [], "matrix": []})
    matrix = []
    for med in med_names:
        row = []
        for symp in symp_names:
            dates = sorted(dates_by_symp[symp])
            xs = [med_cnt.get((med, d), 0) for d in dates]
            ys = [symp_avg[(symp, d)] for d in dates]
            row.append(_pearson(xs, ys) if sum(xs) > 0 else None)
        matrix.append(row)
    return JSONResponse({"med_names": med_names, "symp_names": symp_names, "matrix": matrix})


@app.get("/api/medications")
def api_medications():
    uid = _current_user_id.get()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, dose, notes, timestamp FROM medications"
            " WHERE user_id = ? ORDER BY timestamp ASC",
            (uid,),
        ).fetchall()
    return JSONResponse({"medications": [dict(r) for r in rows]})


@app.get("/api/profile")
def api_profile_get():
    uid = _current_user_id.get()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM user_profile WHERE id = ?", (uid,)).fetchone()
    if row is None:
        return JSONResponse({"name": "", "dob": "", "conditions": "", "medications": ""})
    data = dict(row)
    data.pop("password_hash", None)
    return JSONResponse(data)


@app.post("/api/profile")
def api_profile_update(
    name: str = Form(""),
    dob: str = Form(""),
    conditions: str = Form(""),
    medications: str = Form(""),
):
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "UPDATE user_profile SET name=?, dob=?, conditions=?, medications=? WHERE id=?",
            (name.strip(), dob, conditions.strip(), medications.strip(), uid),
        )
        conn.commit()
    return JSONResponse({"status": "ok"})


@app.get("/symptoms/chart", response_class=HTMLResponse)
def symptoms_chart():
    return f"""<!DOCTYPE html>
<html>
<head>
  {PAGE_STYLE}
  <title>Symptom Chart</title>
</head>
<body>
  {_nav_bar('chart')}
  <div class="container" style="max-width:860px;">
    <h1>Symptom Chart</h1>

    <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-top:12px;">
      <div style="display:flex; align-items:center; gap:6px;">
        <label for="range-from" style="font-size:13px; font-weight:600; color:#555;">From</label>
        <input type="date" id="range-from" onchange="render()"
          style="border:1px solid #d1d5db; border-radius:6px; padding:5px 8px; font-size:13px; font-family:inherit;">
      </div>
      <div style="display:flex; align-items:center; gap:6px;">
        <label for="range-to" style="font-size:13px; font-weight:600; color:#555;">To</label>
        <input type="date" id="range-to" onchange="render()"
          style="border:1px solid #d1d5db; border-radius:6px; padding:5px 8px; font-size:13px; font-family:inherit;">
      </div>
      <div style="display:flex; gap:4px;">
        <button onclick="setPreset(7)"  style="border:1px solid #d1d5db; background:#fff; border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; font-family:inherit;">7d</button>
        <button onclick="setPreset(30)" style="border:1px solid #d1d5db; background:#fff; border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; font-family:inherit;">30d</button>
        <button onclick="setPreset(90)" style="border:1px solid #d1d5db; background:#fff; border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; font-family:inherit;">90d</button>
        <button onclick="setPresetAll()" style="border:1px solid #d1d5db; background:#fff; border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; font-family:inherit;">All</button>
      </div>
    </div>

    <div id="no-data" class="empty" style="display:none; margin-top:24px;">
      Not enough data yet &mdash; log at least 2 symptoms first.
    </div>

    <div id="chart-wrapper" class="card" style="display:none; margin-top:16px; padding:24px;">
      <div id="toggle-bar" style="display:flex; flex-wrap:wrap; gap:6px; margin-bottom:16px;"></div>
      <canvas id="symptomChart"></canvas>
    </div>

    <div id="corr-wrapper" style="display:none; margin-top:28px;">
      <h2 style="font-size:18px; margin-bottom:4px;">Symptom Correlations</h2>
      <p style="font-size:13px; color:#666; margin:0 0 12px;">
        Pearson r between symptom severities, averaged by day.
        Requires &ge;3 shared days per pair. Red&nbsp;=&nbsp;positive, blue&nbsp;=&nbsp;negative.
      </p>
      <div id="corr-table" style="overflow-x:auto;"></div>
    </div>

    <div id="med-corr-wrapper" style="display:none; margin-top:28px;">
      <h2 style="font-size:18px; margin-bottom:4px;">Medication &ndash; Symptom Correlations</h2>
      <p style="font-size:13px; color:#666; margin:0 0 12px;">
        Pearson r between daily medication doses and symptom severity.
        Positive (red) = medication often taken on worse days; negative (blue) = associated with lower severity.
        Requires &ge;3 symptom days with at least one dose.
      </p>
      <div id="med-corr-table" style="overflow-x:auto;"></div>
    </div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
  <script>
    const PALETTE = [
      "#3b82f6","#ef4444","#22c55e","#f97316","#a855f7",
      "#06b6d4","#eab308","#ec4899","#14b8a6","#f43f5e","#8b5cf6","#84cc16"
    ];
    const MED_PALETTE = ["#7c3aed","#9333ea","#a855f7","#6d28d9","#c026d3","#0ea5e9","#0f766e","#b45309"];
    const MED_SHAPES = ["triangle","rectRot","star","crossRot","rect","circle"];
    const MED_SYMBOLS = {{
      triangle: "&#9650;",   // ▲
      rectRot: "&#9670;",    // ◆
      star: "&#9733;",       // ★
      crossRot: "&#10006;",  // ✖
      rect: "&#9632;",       // ■
      circle: "&#9679;",     // ●
    }};
    function escHtml(v) {{
      return String(v)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }}

    function fmtDate(dateStr) {{
      const d = new Date(dateStr + "T00:00:00Z");
      return d.toLocaleDateString("en-US", {{ month: "short", day: "numeric", timeZone: "UTC" }});
    }}

    function stableIndex(name, size) {{
      let h = 2166136261;
      for (let i = 0; i < name.length; i++) {{
        h ^= name.charCodeAt(i);
        h = Math.imul(h, 16777619);
      }}
      return ((h >>> 0) % size);
    }}

    function corrColor(r) {{
      if (r === null) return {{ bg: "#e5e7eb", text: "#9ca3af" }};
      const t = Math.abs(r);
      const light = Math.round(255 * (1 - t));
      const bg = r >= 0
        ? `rgb(255,${{light}},${{light}})`
        : `rgb(${{light}},${{light}},255)`;
      return {{ bg, text: t > 0.55 ? "#fff" : "#333" }};
    }}

    let _allSymp = [], _allMeds = [], _chart = null;

    async function init() {{
      const [sr, mr] = await Promise.all([fetch("/api/symptoms"), fetch("/api/medications")]);
      const [sd, md] = await Promise.all([sr.json(), mr.json()]);
      _allSymp = sd.symptoms;
      _allMeds = md.medications;

      if (_allSymp.length < 2 && _allMeds.length === 0) {{
        document.getElementById("no-data").style.display = "block";
        return;
      }}

      // Default range: last 30 days of data
      const dates = [
        ..._allSymp.map(s => s.timestamp.slice(0, 10)),
        ..._allMeds.map(m => m.timestamp.slice(0, 10)),
      ].sort();
      if (dates.length) {{
        const latest = new Date(dates[dates.length - 1] + "T00:00:00");
        const from30 = new Date(+latest - 29 * 86400000);
        document.getElementById("range-from").value = from30.toISOString().slice(0, 10);
        document.getElementById("range-to").value = dates[dates.length - 1];
      }}

      render();
    }}

    function setPreset(days) {{
      const to = new Date();
      const from = new Date(+to - days * 86400000);
      document.getElementById("range-from").value = from.toISOString().slice(0, 10);
      document.getElementById("range-to").value = to.toISOString().slice(0, 10);
      render();
    }}

    function setPresetAll() {{
      const dates = [
        ..._allSymp.map(s => s.timestamp.slice(0, 10)),
        ..._allMeds.map(m => m.timestamp.slice(0, 10)),
      ].sort();
      if (dates.length) {{
        document.getElementById("range-from").value = dates[0];
        document.getElementById("range-to").value = dates[dates.length - 1];
      }}
      render();
    }}

    function render() {{
      const from = document.getElementById("range-from").value;
      const to   = document.getElementById("range-to").value;
      const syms = _allSymp.filter(s => {{
        const d = s.timestamp.slice(0, 10);
        return (!from || d >= from) && (!to || d <= to);
      }});
      const meds = _allMeds.filter(m => {{
        const d = m.timestamp.slice(0, 10);
        return (!from || d >= from) && (!to || d <= to);
      }});
      renderChart(syms, meds);
      renderCorrelations(from, to);
      renderMedCorrelations(from, to);
    }}

    function renderChart(symptoms, medications) {{
      document.getElementById("toggle-bar").innerHTML = "";
      if (_chart) {{ _chart.destroy(); _chart = null; }}

      const hasData = symptoms.length > 0 || medications.length > 0;
      document.getElementById("chart-wrapper").style.display = hasData ? "block" : "none";
      document.getElementById("no-data").style.display = hasData ? "none" : "block";
      if (!hasData) return;

      const allDates = new Set();
      symptoms.forEach(s => allDates.add(s.timestamp.slice(0, 10)));
      medications.forEach(m => allDates.add(m.timestamp.slice(0, 10)));
      const labels = [...allDates].sort().map(d => fmtDate(d));

      const groups = new Map();
      symptoms.forEach(s => {{
        const date = s.timestamp.slice(0, 10);
        if (!groups.has(s.name)) groups.set(s.name, new Map());
        const byDate = groups.get(s.name);
        if (!byDate.has(date)) byDate.set(date, []);
        byDate.get(date).push(s.severity);
      }});

      let i = 0;
      const datasets = [];
      for (const [name, byDate] of groups) {{
        const color = PALETTE[i % PALETTE.length]; i++;
        datasets.push({{
          label: name,
          data: [...byDate.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([date, sevs]) => ({{
            x: fmtDate(date),
            y: Math.round(sevs.reduce((a, b) => a + b, 0) / sevs.length * 10) / 10,
          }})),
          borderColor: color, backgroundColor: color + "33",
          tension: 0.4, pointRadius: 4, pointHoverRadius: 7,
        }});
      }}

      const medGroups = new Map();
      medications.forEach(m => {{
        if (!medGroups.has(m.name)) medGroups.set(m.name, []);
        medGroups.get(m.name).push(m);
      }});
      let lane = 0;
      for (const [name, meds] of [...medGroups.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {{
        const color = MED_PALETTE[stableIndex(name, MED_PALETTE.length)];
        const shape = MED_SHAPES[stableIndex(name + "::shape", MED_SHAPES.length)];
        const yLane = 0.12 + lane * 0.14;
        lane += 1;
        datasets.push({{
          type: "scatter", label: name, _isMed: true, _medSymbol: MED_SYMBOLS[shape],
          data: meds.map(m => ({{
            x: fmtDate(m.timestamp.slice(0, 10)), y: yLane,
            _dose: m.dose, _time: m.timestamp.slice(11, 16),
          }})),
          // Use high-contrast fill + dark outline for crisper medication markers.
          backgroundColor: "#ffffff",
          borderColor: color,
          pointStyle: shape,
          pointRadius: 9,
          pointHoverRadius: 11,
          pointBorderWidth: 2.6,
          pointHoverBorderWidth: 3.2,
          pointHoverBackgroundColor: "#ffffff",
          pointHoverBorderColor: "#4c1d95",
        }});
      }}

      _chart = new Chart(document.getElementById("symptomChart"), {{
        type: "line",
        data: {{ labels, datasets }},
        options: {{
          responsive: true,
          scales: {{
            x: {{ type: "category", title: {{ display: true, text: "Date (UTC)" }} }},
            y: {{
              min: 0, max: 10,
              ticks: {{ stepSize: 1, callback: (val) => val === 0 ? "Rx" : val }},
              title: {{ display: true, text: "Avg Severity" }},
            }},
          }},
          plugins: {{
            tooltip: {{
              callbacks: {{
                title: (items) => items[0].dataset.label,
                label: (item) => {{
                  if (item.dataset._isMed) {{
                    const d = item.raw;
                    return d._dose ? `${{d._time}} — ${{d._dose}}` : `Taken at ${{d._time}}`;
                  }}
                  return `Avg severity: ${{item.parsed.y}} on ${{item.label}}`;
                }},
              }},
            }},
            legend: {{ display: false }},
          }},
        }},
      }});

      buildToggles(_chart, datasets);
    }}

    function buildToggles(chart, datasets) {{
      const bar = document.getElementById("toggle-bar");
      datasets.forEach((ds, i) => {{
        const color = ds.borderColor || ds.backgroundColor;
        const isMed = !!ds._isMed;
        const btn = document.createElement("button");
        if (isMed) {{
          const icon = document.createElement("span");
          icon.style.cssText = `font-size:10px;color:${{color}};line-height:1;`;
          icon.innerHTML = ds._medSymbol || "&#9650;";
          btn.appendChild(icon);
        }} else {{
          const dot = document.createElement("span");
          dot.style.cssText = `width:10px;height:10px;border-radius:50%;background:${{color}};flex-shrink:0;display:inline-block;`;
          btn.appendChild(dot);
        }}
        btn.appendChild(document.createTextNode(` ${{ds.label}}`));
        btn.style.cssText = `display:inline-flex;align-items:center;gap:5px;padding:4px 12px;`
          + `border-radius:20px;border:1.5px solid ${{color}};background:${{color}}22;`
          + `font-size:13px;cursor:pointer;font-family:inherit;color:#111;transition:opacity .15s;`;
        btn.onclick = () => {{
          const meta = chart.getDatasetMeta(i);
          meta.hidden = !meta.hidden;
          chart.update();
          const hidden = meta.hidden;
          btn.style.opacity = hidden ? "0.35" : "1";
          btn.style.background = hidden ? "transparent" : `${{color}}22`;
          btn.style.borderColor = hidden ? "#d1d5db" : color;
          btn.style.color = hidden ? "#9ca3af" : "#111";
        }};
        bar.appendChild(btn);
      }});
    }}

    function describeR(r, isMed) {{
      if (r === null) return "&mdash;";
      const a = Math.abs(r);
      if (a < 0.1) return isMed ? "no clear pattern" : "no clear link";
      if (isMed) {{
        if (r >=  0.5) return "mostly on bad days";
        if (r >=  0.3) return "more on bad days";
        if (r >=  0.1) return "slightly on bad days";
        if (r <= -0.5) return "mostly on good days";
        if (r <= -0.3) return "more on good days";
        return "slightly on good days";
      }} else {{
        if (r >=  0.7) return "very often together";
        if (r >=  0.5) return "often together";
        if (r >=  0.3) return "sometimes together";
        if (r >=  0.1) return "weakly linked";
        if (r <= -0.7) return "almost never together";
        if (r <= -0.5) return "rarely together";
        if (r <= -0.3) return "tend to alternate";
        return "weakly opposite";
      }}
    }}

    async function renderCorrelations(from, to) {{
      const params = new URLSearchParams();
      if (from) params.set("from_date", from);
      if (to)   params.set("to_date", to);
      const resp = await fetch(`/api/symptoms/correlations?${{params}}`);
      const data = await resp.json();

      const corrWrapper = document.getElementById("corr-wrapper");
      if (data.names.length < 2) {{ corrWrapper.style.display = "none"; return; }}
      corrWrapper.style.display = "block";

      const names = data.names, matrix = data.matrix;
      const thStyle = `style="padding:8px 10px; font-size:13px; font-weight:600;
        text-align:center; white-space:nowrap; background:#f5f5f5;"`;
      const rowHeadStyle = `style="padding:8px 12px; font-size:13px; font-weight:600;
        text-align:right; white-space:nowrap; background:#f5f5f5;"`;

      let html = `<table style="border-collapse:collapse; width:100%;">`;
      html += `<thead><tr><th ${{thStyle}}></th>`;
      for (const name of names) html += `<th ${{thStyle}}>${{escHtml(name)}}</th>`;
      html += `</tr></thead><tbody>`;
      for (let r = 0; r < names.length; r++) {{
        html += `<tr><th ${{rowHeadStyle}}>${{escHtml(names[r])}}</th>`;
        for (let c = 0; c < names.length; c++) {{
          const val = matrix[r][c];
          const {{ bg, text }} = corrColor(val);
          const isDiag = r === c;
          const label = isDiag ? "&mdash;" : describeR(val, false);
          const title = isDiag || val === null ? "" : ` title="r = ${{val >= 0 ? "+" : ""}}${{val.toFixed(2)}}"`;
          const cellBg = isDiag ? "#f3f4f6" : bg;
          const cellText = isDiag ? "#9ca3af" : text;
          html += `<td${{title}} style="min-width:110px; padding:9px 8px; text-align:center;
            font-size:12px; font-weight:600; white-space:nowrap; background:${{cellBg}}; color:${{cellText}}">${{label}}</td>`;
        }}
        html += `</tr>`;
      }}
      html += `</tbody></table>`;
      document.getElementById("corr-table").innerHTML = html;
    }}

    async function renderMedCorrelations(from, to) {{
      const params = new URLSearchParams();
      if (from) params.set("from_date", from);
      if (to)   params.set("to_date", to);
      const resp = await fetch(`/api/correlations/med-symptom?${{params}}`);
      const data = await resp.json();

      const wrapper = document.getElementById("med-corr-wrapper");
      if (!data.med_names.length || !data.symp_names.length) {{ wrapper.style.display = "none"; return; }}
      wrapper.style.display = "block";

      const {{ med_names, symp_names, matrix }} = data;
      const thStyle = `style="padding:8px 10px; font-size:13px; font-weight:600;
        text-align:center; white-space:nowrap; background:#f5f5f5;"`;
      const rowHeadStyle = `style="padding:8px 12px; font-size:13px; font-weight:600;
        text-align:right; white-space:nowrap; background:#f5f5f5;"`;

      let html = `<table style="border-collapse:collapse; width:100%;">`;
      html += `<thead><tr><th ${{thStyle}}></th>`;
      for (const s of symp_names) html += `<th ${{thStyle}}>${{escHtml(s)}}</th>`;
      html += `</tr></thead><tbody>`;
      for (let r = 0; r < med_names.length; r++) {{
        html += `<tr><th ${{rowHeadStyle}}>${{escHtml(med_names[r])}}</th>`;
        for (let c = 0; c < symp_names.length; c++) {{
          const val = matrix[r][c];
          const {{ bg, text }} = corrColor(val);
          const label = describeR(val, true);
          const title = val === null ? "" : ` title="r = ${{val >= 0 ? "+" : ""}}${{val.toFixed(2)}}"`;
          html += `<td${{title}} style="min-width:110px; padding:9px 8px; text-align:center;
            font-size:12px; font-weight:600; white-space:nowrap; background:${{bg}}; color:${{text}}">${{label}}</td>`;
        }}
        html += `</tr>`;
      }}
      html += `</tbody></table>`;
      document.getElementById("med-corr-table").innerHTML = html;
    }}

    init();
  </script>
</body>
</html>
"""


@app.get("/symptoms/calendar", response_class=HTMLResponse)
def symptoms_calendar():
    return """<!DOCTYPE html>
<html>
<head>""" + PAGE_STYLE + """
  <title>Symptom Calendar</title>
  <style>
    .cal-nav { display: flex; align-items: center; justify-content: space-between; margin: 16px 0 8px; }
    .cal-nav button { background: #fff; border: 1px solid #d1d5db; border-radius: 6px;
      padding: 6px 14px; font-size: 18px; cursor: pointer; color: #374151; }
    .cal-nav button:hover { background: #f3f4f6; }
    .cal-month { font-size: 18px; font-weight: 700; color: #111; }
    .cal-grid { width: 100%; border-collapse: collapse; table-layout: fixed; }
    .cal-grid th { padding: 6px 0; text-align: center; font-size: 12px; font-weight: 600;
      color: #6b7280; border-bottom: 2px solid #e5e7eb; }
    .cal-grid td { width: 14.28%; min-height: 72px; height: 72px; vertical-align: top;
      padding: 5px 6px; border: 1px solid #e5e7eb; background: #fff; }
    .cal-grid td.other-month { background: #f9fafb; }
    .cal-grid td.other-month .day-num { color: #d1d5db; }
    .cal-grid td.today { outline: 2px solid #3b82f6; outline-offset: -2px; }
    .cal-grid td.has-data { cursor: pointer; }
    .cal-grid td.has-data:hover { background: #f0f9ff; }
    .cal-grid td.selected { background: #eff6ff; }
    .day-num { font-size: 12px; font-weight: 600; color: #374151; }
    .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-left: 3px;
      vertical-align: middle; }
    .count { font-size: 11px; color: #6b7280; margin-left: 2px; vertical-align: middle; }
    #day-detail { display: none; margin-top: 20px; }
    #day-detail h3 { font-size: 16px; margin: 0 0 12px; color: #111; }
    .detail-card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
      padding: 12px 14px; margin-bottom: 10px; }
    .detail-header { display: flex; align-items: center; gap: 10px; }
    .detail-time { font-size: 12px; color: #6b7280; margin-top: 2px; }
    .detail-notes { font-size: 13px; color: #555; margin: 6px 0 0; }
    @media (max-width: 640px) {
      .cal-grid td { height: 52px; min-height: 52px; padding: 3px 4px; }
      .count { display: none; }
    }
  </style>
</head>
<body>
""" + _nav_bar('calendar') + """
  <div class="container" style="max-width:700px;">
    <h1>Symptom Calendar</h1>
    <div class="cal-nav">
      <button id="prev-btn" onclick="shiftMonth(-1)">&#8592;</button>
      <span class="cal-month" id="month-label"></span>
      <button id="next-btn" onclick="shiftMonth(1)">&#8594;</button>
    </div>
    <table class="cal-grid">
      <thead>
        <tr>
          <th>Sun</th><th>Mon</th><th>Tue</th><th>Wed</th><th>Thu</th><th>Fri</th><th>Sat</th>
        </tr>
      </thead>
      <tbody id="cal-body"></tbody>
    </table>
    <div id="day-detail">
      <h3 id="detail-title"></h3>
      <div id="detail-cards"></div>
    </div>
  </div>
  <script>
    const MONTHS = ["January","February","March","April","May","June",
                    "July","August","September","October","November","December"];
    function escHtml(v) {
      return String(v)
        .replace(/&/g,"&amp;")
        .replace(/</g,"&lt;")
        .replace(/>/g,"&gt;")
        .replace(/"/g,"&quot;")
        .replace(/'/g,"&#39;");
    }

    function sevColor(s) {
      if (s <= 3) return "#22c55e";
      if (s <= 6) return "#eab308";
      if (s <= 8) return "#f97316";
      return "#ef4444";
    }

    function pad(n) { return String(n).padStart(2, "0"); }

    let byDate = {};     // "YYYY-MM-DD" -> [{id,name,severity,notes,timestamp}]
    let medsByDate = {}; // "YYYY-MM-DD" -> [{id,name,dose,notes,timestamp}]
    let curYear, curMonth, selectedDate = null;

    async function loadData() {
      const [sympResp, medResp] = await Promise.all([fetch("/api/symptoms"), fetch("/api/medications")]);
      const [sympData, medData] = await Promise.all([sympResp.json(), medResp.json()]);
      byDate = {};
      for (const s of sympData.symptoms) {
        const date = s.timestamp.slice(0, 10);
        if (!byDate[date]) byDate[date] = [];
        byDate[date].push(s);
      }
      medsByDate = {};
      for (const m of medData.medications) {
        const date = m.timestamp.slice(0, 10);
        if (!medsByDate[date]) medsByDate[date] = [];
        medsByDate[date].push(m);
      }
      const now = new Date();
      curYear = now.getFullYear();
      curMonth = now.getMonth();  // 0-indexed
      renderCalendar();
    }

    function shiftMonth(delta) {
      curMonth += delta;
      if (curMonth > 11) { curMonth = 0; curYear++; }
      if (curMonth < 0)  { curMonth = 11; curYear--; }
      selectedDate = null;
      document.getElementById("day-detail").style.display = "none";
      renderCalendar();
    }

    function renderCalendar() {
      document.getElementById("month-label").textContent = MONTHS[curMonth] + " " + curYear;

      const today = new Date();
      const todayStr = today.getFullYear() + "-" + pad(today.getMonth()+1) + "-" + pad(today.getDate());

      // First day of month (0=Sun), days in month
      const firstDay = new Date(curYear, curMonth, 1).getDay();
      const daysInMonth = new Date(curYear, curMonth + 1, 0).getDate();
      // Days from previous month to fill first row
      const prevMonthDays = new Date(curYear, curMonth, 0).getDate();

      const tbody = document.getElementById("cal-body");
      tbody.innerHTML = "";

      let dayCount = 1;
      let nextCount = 1;

      for (let row = 0; row < 6; row++) {
        if (row > 0 && dayCount > daysInMonth) break;
        const tr = document.createElement("tr");
        for (let col = 0; col < 7; col++) {
          const td = document.createElement("td");
          const cellIndex = row * 7 + col;

          if (cellIndex < firstDay) {
            // Previous month filler
            const d = prevMonthDays - firstDay + cellIndex + 1;
            td.className = "other-month";
            td.innerHTML = `<span class="day-num">${d}</span>`;
          } else if (dayCount > daysInMonth) {
            // Next month filler
            td.className = "other-month";
            td.innerHTML = `<span class="day-num">${nextCount++}</span>`;
          } else {
            const dateStr = curYear + "-" + pad(curMonth + 1) + "-" + pad(dayCount);
            const entries = byDate[dateStr];
            const medEntries = medsByDate[dateStr];
            let classes = "";
            if (dateStr === todayStr) classes += " today";
            if (entries || medEntries) classes += " has-data";
            if (dateStr === selectedDate) classes += " selected";
            td.className = classes.trim();

            let inner = `<span class="day-num">${dayCount}</span>`;
            if (entries) {
              const maxSev = Math.max(...entries.map(e => e.severity));
              const color = sevColor(maxSev);
              inner += `<span class="dot" style="background:${color}"></span>`;
              if (entries.length > 1) {
                inner += `<span class="count">×${entries.length}</span>`;
              }
            }
            if (medEntries) {
              inner += `<span class="dot" style="background:#a855f7"></span>`;
              if (medEntries.length > 1) {
                inner += `<span class="count">×${medEntries.length}</span>`;
              }
            }
            if (entries || medEntries) {
              td.setAttribute("data-date", dateStr);
              td.addEventListener("click", () => onDayClick(dateStr));
            }
            td.innerHTML = inner;
            dayCount++;
          }
          tr.appendChild(td);
        }
        tbody.appendChild(tr);
      }
    }

    function onDayClick(dateStr) {
      const detail = document.getElementById("day-detail");
      if (selectedDate === dateStr) {
        // Toggle off
        selectedDate = null;
        detail.style.display = "none";
        renderCalendar();
        return;
      }
      selectedDate = dateStr;
      renderCalendar();

      const entries = byDate[dateStr] || [];
      const [year, month, day] = dateStr.split("-");
      document.getElementById("detail-title").textContent =
        MONTHS[parseInt(month) - 1] + " " + parseInt(day) + ", " + year;

      const cards = document.getElementById("detail-cards");
      cards.innerHTML = "";
      const medEntries = medsByDate[dateStr] || [];
      for (const m of medEntries) {
        const time = m.timestamp.slice(11, 16);
        const doseHtml = m.dose
          ? `<span style="font-size:12px;color:#7c3aed;margin-top:2px;display:block;">${escHtml(m.dose)}</span>`
          : "";
        const notesHtml = m.notes
          ? `<p class="detail-notes">${escHtml(m.notes)}</p>`
          : "";
        const div = document.createElement("div");
        div.className = "detail-card";
        div.innerHTML = `
          <div class="detail-header">
            <div class="badge" style="background:#a855f7;width:32px;height:32px;font-size:11px;flex-shrink:0;">Rx</div>
            <div>
              <div class="card-name">${escHtml(m.name)}</div>
              ${doseHtml}
              <div class="detail-time">${time}</div>
            </div>
          </div>
          ${notesHtml}
        `;
        cards.appendChild(div);
      }
      for (const e of entries) {
        const time = e.timestamp.slice(11, 16);  // HH:MM
        const notesHtml = e.notes
          ? `<p class="detail-notes">${escHtml(e.notes)}</p>`
          : "";
        const div = document.createElement("div");
        div.className = "detail-card";
        div.innerHTML = `
          <div class="detail-header">
            <div class="badge" style="background:${sevColor(e.severity)};width:32px;height:32px;font-size:14px;">${e.severity}</div>
            <div>
              <div class="card-name">${escHtml(e.name)}</div>
              <div class="detail-time">${time}</div>
            </div>
          </div>
          ${notesHtml}
        `;
        cards.appendChild(div);
      }
      detail.style.display = "block";
    }

    loadData();
  </script>
</body>
</html>
"""


MEDICATION_SUGGESTIONS = [
    "Acetaminophen (Tylenol)", "Ibuprofen (Advil/Motrin)", "Aspirin",
    "Naproxen (Aleve)", "Metformin", "Lisinopril", "Atorvastatin",
    "Levothyroxine", "Amlodipine", "Omeprazole", "Metoprolol",
    "Losartan", "Albuterol", "Gabapentin", "Sertraline (Zoloft)",
    "Escitalopram (Lexapro)", "Fluoxetine (Prozac)", "Amoxicillin",
    "Azithromycin", "Prednisone", "Cetirizine (Zyrtec)",
    "Loratadine (Claritin)", "Montelukast (Singulair)",
    "Bupropion (Wellbutrin)", "Duloxetine (Cymbalta)",
    "Pantoprazole", "Furosemide", "Hydrochlorothiazide",
    "Clonazepam", "Alprazolam (Xanax)", "Zolpidem (Ambien)",
    "Melatonin", "Vitamin D", "Fish Oil", "Magnesium",
]
_MED_DATALIST = "".join(f'<option value="{html.escape(med)}">' for med in MEDICATION_SUGGESTIONS)


@app.get("/medications", response_class=HTMLResponse)
def medications_list():
    uid = _current_user_id.get()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, dose, notes, timestamp FROM medications"
            " WHERE user_id = ? ORDER BY timestamp DESC",
            (uid,),
        ).fetchall()

    groups: dict[str, list] = {}
    for row in rows:
        n = row["name"]
        if n not in groups:
            groups[n] = []
        groups[n].append(row)

    if groups:
        sections = ""
        for name, entries in groups.items():
            cards = "".join(
                f"""
            <div class="card med-card">
              <div class="card-header">
                <div class="badge med-badge">Rx</div>
                <div>
                  <div class="card-name med-name">{html.escape(e['name'])}</div>
                  {"<div class='med-dose'>" + html.escape(e['dose']) + "</div>" if e['dose'] else ""}
                  <div class="card-ts med-ts">{html.escape(e['timestamp'])}</div>
                </div>
              </div>
              {"<p class='card-notes med-notes'>" + html.escape(e['notes']) + "</p>" if e['notes'] else ""}
              <div class="med-actions">
                <a href="/medications/{e['id']}/edit" class="btn-edit">Edit</a>
                <form method="post" action="/medications/delete" style="margin:0;">
                  <input type="hidden" name="id" value="{e['id']}">
                  <button class="btn-delete" type="submit"
                    onclick="return confirm('Delete this medication entry?')">Delete</button>
                </form>
              </div>
            </div>
            """
                for e in entries
            )
            count = len(entries)
            label = "entry" if count == 1 else "entries"
            sections += f"""
        <div class="med-group">
          <div class="med-group-header">
            <span class="med-group-name">{html.escape(name)}</span>
            <span class="med-count">{count} {label}</span>
          </div>
          {cards}
        </div>
        """
    else:
        sections = "<p class='empty'>No medications logged yet.</p>"

    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}
  <style>
    .med-group {{ margin-bottom: 34px; }}
    .med-group-header {{ display: flex; align-items: center; gap: 10px;
                         border-bottom: 2px solid #dbe1ea; padding-bottom: 10px; margin-top: 22px; }}
    .med-group-name {{ font-size: 20px; font-weight: 750; color: #111827; line-height: 1.2; }}
    .med-count {{ font-size: 14px; color: #374151; background: #eef2ff; border: 1px solid #e0e7ff;
                  border-radius: 999px; padding: 2px 10px; }}
    .med-card {{ border-color: #dbe1ea; padding: 18px; }}
    .med-badge {{ background: #7c3aed; font-size: 13px; width: 40px; height: 40px; }}
    .med-name {{ font-size: 18px; font-weight: 700; color: #111827; }}
    .med-dose {{ font-size: 14px; color: #5b21b6; margin-top: 4px; font-weight: 600; line-height: 1.35; }}
    .med-ts {{ font-size: 13px; color: #4b5563; margin-top: 5px; }}
    .med-notes {{ font-size: 15px; color: #1f2937; line-height: 1.55; }}
    .med-actions {{ display: flex; gap: 10px; align-items: center; margin-top: 14px; }}
    .med-actions .btn-edit, .med-actions .btn-delete {{ font-size: 14px; padding: 6px 12px; }}
    .med-cta {{ background: #7c3aed; margin-bottom: 18px; padding: 10px 18px; font-size: 15px; }}
    .med-cta:hover {{ background: #6d28d9; }}
    @media (max-width: 640px) {{
      .med-group-name {{ font-size: 18px; }}
      .med-count {{ font-size: 13px; }}
      .med-card {{ padding: 16px; }}
      .med-badge {{ width: 36px; height: 36px; font-size: 12px; }}
      .med-dose, .med-ts {{ font-size: 13px; }}
      .med-notes {{ font-size: 14px; }}
      .med-actions .btn-edit, .med-actions .btn-delete {{ min-height: 36px; }}
    }}
  </style>
</head>
<body>
  {_nav_bar('meds')}
  <div class="container">
    <h1>Medications</h1>
    <a href="/medications/new" class="btn-log med-cta">+ Log Medication</a>
    {sections}
  </div>
</body>
</html>
"""


@app.get("/medications/new", response_class=HTMLResponse)
def medications_new(error: str = ""):
    error_html = f'<div class="alert">{html.escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}</head>
<body>
  {_nav_bar('meds')}
  <div class="container">
    <h1>Log a Medication</h1>
    {error_html}
    <div class="card">
      <form method="post" action="/medications">
        <div class="form-group">
          <label for="med_name">Medication name <span style="color:#ef4444">*</span></label>
          <input type="text" id="med_name" name="name"
            placeholder="e.g. Ibuprofen (Advil/Motrin)" required
            list="med-suggestions" autocomplete="off">
          <datalist id="med-suggestions">{_MED_DATALIST}</datalist>
        </div>
        <div class="form-group">
          <label for="dose">Dose <span style="color:#aaa;font-weight:400">(optional)</span></label>
          <input type="text" id="dose" name="dose" placeholder="e.g. 400mg, 10mg twice daily">
        </div>
        <div class="form-group">
          <label for="notes">Notes <span style="color:#aaa;font-weight:400">(optional)</span></label>
          <textarea id="notes" name="notes" rows="2" placeholder="Any additional details..."></textarea>
        </div>
        <div class="form-group">
          <label for="med_date">Date &amp; time <span style="color:#aaa;font-weight:400">(defaults to now)</span></label>
          <input type="datetime-local" id="med_date" name="med_date" required>
        </div>
        <button class="btn-primary med-submit" type="submit">Save Medication</button>
      </form>
    </div>
  </div>
  <script>
    const now = new Date();
    const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
    const localStr = local.toISOString().slice(0, 16);
    document.getElementById("med_date").value = localStr;
    document.getElementById("med_date").max = localStr;
  </script>
</body>
</html>
"""


@app.post("/medications")
def medications_create(
    name: str = Form(...),
    dose: str = Form(""),
    notes: str = Form(""),
    med_date: str = Form(...),
):
    if not name.strip():
        return RedirectResponse(url="/medications/new?error=Medication+name+is+required", status_code=303)
    try:
        ts_dt = datetime.strptime(med_date, "%Y-%m-%dT%H:%M")
    except ValueError:
        return RedirectResponse(url="/medications/new?error=Invalid+date+format", status_code=303)
    if ts_dt > datetime.now():
        return RedirectResponse(url="/medications/new?error=Date+cannot+be+in+the+future", status_code=303)
    timestamp = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO medications (name, dose, notes, timestamp, user_id) VALUES (?, ?, ?, ?, ?)",
            (name.strip(), dose.strip(), notes.strip(), timestamp, uid),
        )
        conn.commit()
    return RedirectResponse(url="/medications", status_code=303)


@app.post("/medications/delete")
def medications_delete(id: int = Form(...)):
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute("DELETE FROM medications WHERE id = ? AND user_id = ?", (id, uid))
        conn.commit()
    return RedirectResponse(url="/medications", status_code=303)


@app.get("/medications/{med_id}/edit", response_class=HTMLResponse)
def medications_edit_get(med_id: int, error: str = ""):
    uid = _current_user_id.get()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM medications WHERE id = ? AND user_id = ?", (med_id, uid)
        ).fetchone()
    if row is None:
        return RedirectResponse(url="/medications", status_code=303)
    m = dict(row)
    dt_local = m["timestamp"].replace(" ", "T")[:16]
    error_html = f'<div class="alert">{html.escape(error)}</div>' if error else ""
    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}</head>
<body>
  {_nav_bar('meds')}
  <div class="container">
    <h1>Edit Medication</h1>
    {error_html}
    <div class="card">
      <form method="post" action="/medications/{med_id}/edit">
        <div class="form-group">
          <label for="med_name">Medication name <span style="color:#ef4444">*</span></label>
          <input type="text" id="med_name" name="name" value="{html.escape(m['name'])}"
            required list="med-suggestions" autocomplete="off">
          <datalist id="med-suggestions">{_MED_DATALIST}</datalist>
        </div>
        <div class="form-group">
          <label for="dose">Dose <span style="color:#aaa;font-weight:400">(optional)</span></label>
          <input type="text" id="dose" name="dose" value="{html.escape(m['dose'])}">
        </div>
        <div class="form-group">
          <label for="notes">Notes <span style="color:#aaa;font-weight:400">(optional)</span></label>
          <textarea id="notes" name="notes" rows="2">{html.escape(m['notes'])}</textarea>
        </div>
        <div class="form-group">
          <label for="med_date">Date &amp; time</label>
          <input type="datetime-local" id="med_date" name="med_date"
            value="{dt_local}" required>
        </div>
        <div style="display:flex; gap:12px; align-items:center;">
          <button class="btn-primary med-submit" type="submit">Save Changes</button>
          <a href="/medications" class="back">Cancel</a>
        </div>
      </form>
    </div>
  </div>
  <script>
    const _now = new Date();
    const _local = new Date(_now.getTime() - _now.getTimezoneOffset() * 60000);
    document.getElementById("med_date").max = _local.toISOString().slice(0, 16);
  </script>
</body>
</html>"""


@app.post("/medications/{med_id}/edit")
def medications_edit_post(
    med_id: int,
    name: str = Form(...),
    dose: str = Form(""),
    notes: str = Form(""),
    med_date: str = Form(...),
):
    if not name.strip():
        return RedirectResponse(
            url=f"/medications/{med_id}/edit?error=Medication+name+is+required", status_code=303
        )
    try:
        ts_dt = datetime.strptime(med_date, "%Y-%m-%dT%H:%M")
    except ValueError:
        return RedirectResponse(
            url=f"/medications/{med_id}/edit?error=Invalid+date+format", status_code=303
        )
    if ts_dt > datetime.now():
        return RedirectResponse(
            url=f"/medications/{med_id}/edit?error=Date+cannot+be+in+the+future", status_code=303
        )
    timestamp = ts_dt.strftime("%Y-%m-%d %H:%M:%S")
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "UPDATE medications SET name = ?, dose = ?, notes = ?, timestamp = ?"
            " WHERE id = ? AND user_id = ?",
            (name.strip(), dose.strip(), notes.strip(), timestamp, med_id, uid),
        )
        conn.commit()
    return RedirectResponse(url="/medications", status_code=303)
SECRET_KEY = _load_secret_key()


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


def _physician_owns_patient(physician_id: int, patient_id: int) -> bool:
    with get_db() as conn:
        row = conn.execute(
            "SELECT 1 FROM physician_patients WHERE physician_id = ? AND patient_id = ?",
            (physician_id, patient_id),
        ).fetchone()
    return row is not None



def _calc_age(dob_str: str):
    if not dob_str:
        return None
    try:
        dob = datetime.strptime(dob_str, "%Y-%m-%d")
        today = datetime.today()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except ValueError:
        return None


@app.get("/profile", response_class=HTMLResponse)
def profile_get(saved: int = 0, error: str = ""):
    uid = _current_user_id.get()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM user_profile WHERE id = ?", (uid,)).fetchone()
    p = dict(row) if row else {"name": "", "dob": "", "conditions": "", "medications": "", "photo_ext": "", "share_code": ""}
    age = _calc_age(p["dob"])
    age_str = f" &nbsp;<span style='color:#666;font-size:13px;'>({age} years old)</span>" if age is not None else ""
    if saved:
        top_banner = """<div style="background:#dcfce7; border:1px solid #86efac; color:#15803d;
          border-radius:6px; padding:10px 14px; margin-bottom:16px; font-size:14px;">
          Profile saved.</div>"""
    elif error:
        top_banner = f"""<div class="alert">{html.escape(error)}</div>"""
    else:
        top_banner = ""
    photo_ext = p.get("photo_ext", "")
    if photo_ext:
        photo_html = f"""
    <div style="text-align:center; margin-bottom:24px;">
      <img src="/profile/photo" alt="Profile photo"
        style="width:120px; height:120px; border-radius:50%; object-fit:cover; border:3px solid #e5e7eb;">
      <div style="margin-top:8px;">
        <form method="post" action="/profile/photo" enctype="multipart/form-data" style="display:inline;">
          <label style="cursor:pointer; font-size:13px; color:#1e3a8a; font-weight:500;">
            Change photo
            <input type="file" name="photo" accept="image/*" style="display:none;"
              onchange="this.form.submit()">
          </label>
        </form>
        &nbsp;&middot;&nbsp;
        <form method="post" action="/profile/photo/delete" style="display:inline;">
          <button type="submit" style="background:none; border:none; cursor:pointer;
            font-size:13px; color:#dc2626; font-weight:500; padding:0;"
            onclick="return confirm('Remove your profile photo?')">Remove</button>
        </form>
      </div>
    </div>"""
    else:
        photo_html = """
    <div style="text-align:center; margin-bottom:24px;">
      <div style="width:120px; height:120px; border-radius:50%; background:#e5e7eb;
        display:inline-flex; align-items:center; justify-content:center; border:3px solid #e5e7eb;">
        <svg xmlns="http://www.w3.org/2000/svg" width="60" height="60" viewBox="0 0 24 24"
          fill="none" stroke="#9ca3af" stroke-width="1.5">
          <circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/>
        </svg>
      </div>
      <div style="margin-top:8px;">
        <form method="post" action="/profile/photo" enctype="multipart/form-data" style="display:inline;">
          <label style="cursor:pointer; font-size:13px; color:#1e3a8a; font-weight:500;">
            Upload photo
            <input type="file" name="photo" accept="image/*" style="display:none;"
              onchange="this.form.submit()">
          </label>
        </form>
      </div>
    </div>"""
    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}
  <title>My Profile</title>
</head>
<body>
  {_nav_bar('profile')}
  <div class="container">
    <h1>My Profile</h1>
    {top_banner}
    {photo_html}
    <form method="post" action="/profile" style="margin-top:16px;">
      <div class="form-group">
        <label for="name">Name</label>
        <input type="text" id="name" name="name" value="{html.escape(p['name'])}" placeholder="Your name">
      </div>
      <div class="form-group">
        <label for="dob">Date of Birth{age_str}</label>
        <input type="date" id="dob" name="dob" value="{html.escape(p['dob'])}">
      </div>
      <div class="form-group">
        <label for="conditions">Known Conditions</label>
        <textarea id="conditions" name="conditions" rows="3"
          placeholder="e.g. migraines, asthma">{html.escape(p['conditions'])}</textarea>
      </div>
      <div class="form-group">
        <label for="medications">Medications</label>
        <textarea id="medications" name="medications" rows="3"
          placeholder="e.g. ibuprofen 400mg as needed">{html.escape(p['medications'])}</textarea>
      </div>
      <button type="submit" class="btn-primary">Save Profile</button>
    </form>
    <div style="margin-top:24px; background:#f0f9ff; border:1px solid #bae6fd; border-radius:8px; padding:14px 18px;">
      <p style="font-size:13px; color:#0c4a6e; margin:0 0 6px; font-weight:600;">Physician Share Code</p>
      <p style="font-size:13px; color:#0369a1; margin:0 0 8px;">Give this code to your physician so they can view your data.</p>
      <code style="font-size:20px; font-weight:700; letter-spacing:3px; color:#1e3a8a;">{html.escape(p.get("share_code", ""))}</code>
    </div>
    <details style="margin-top:28px;">
      <summary style="cursor:pointer; font-size:14px; font-weight:600; color:#374151;">
        Change Password
      </summary>
      <form method="post" action="/profile/password" style="margin-top:12px;">
        <div class="form-group">
          <label for="current_password">Current Password</label>
          <input type="password" id="current_password" name="current_password"
            required autocomplete="current-password">
        </div>
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
        <button type="submit" class="btn-primary">Change Password</button>
      </form>
    </details>
  </div>
</body>
</html>
"""


@app.post("/profile")
def profile_update(
    name: str = Form(""),
    dob: str = Form(""),
    conditions: str = Form(""),
    medications: str = Form(""),
):
    if dob:
        try:
            datetime.strptime(dob, "%Y-%m-%d")
        except ValueError:
            return RedirectResponse(url="/profile?error=Invalid+date+of+birth", status_code=303)
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "UPDATE user_profile SET name=?, dob=?, conditions=?, medications=? WHERE id=?",
            (name.strip(), dob, conditions.strip(), medications.strip(), uid),
        )
        conn.commit()
    return RedirectResponse(url="/profile?saved=1", status_code=303)


@app.post("/profile/password")
def profile_change_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    uid = _current_user_id.get()
    with get_db() as conn:
        row = conn.execute(
            "SELECT username, password_hash FROM user_profile WHERE id = ?", (uid,)
        ).fetchone()
    if not row:
        return RedirectResponse(url="/profile?error=User+not+found", status_code=303)
    username, pw_hash = row["username"], row["password_hash"]
    if not _verify_password(current_password, pw_hash):
        return RedirectResponse(url="/profile?error=Current+password+is+incorrect", status_code=303)
    if len(new_password) < 8:
        return RedirectResponse(url="/profile?error=New+password+must+be+at+least+8+characters", status_code=303)
    if new_password != confirm_password:
        return RedirectResponse(url="/profile?error=New+passwords+do+not+match", status_code=303)
    new_hash = _hash_password(new_password)
    with get_db() as conn:
        conn.execute(
            "UPDATE user_profile SET password_hash = ? WHERE id = ?", (new_hash, uid)
        )
        conn.commit()
    resp = RedirectResponse(url="/profile?saved=1", status_code=303)
    _set_session_cookie(resp, request, username, new_hash)
    return resp


_ALLOWED_PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}


def _detect_image_ext(data: bytes) -> Optional[str]:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


async def _read_limited_upload(upload: UploadFile, max_bytes: int) -> Optional[bytes]:
    chunks = []
    total = 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@app.get("/profile/photo")
def profile_photo_get():
    uid = _current_user_id.get()
    with get_db() as conn:
        row = conn.execute("SELECT photo_ext FROM user_profile WHERE id = ?", (uid,)).fetchone()
    if not row or not row["photo_ext"]:
        return JSONResponse({"error": "no photo"}, status_code=404)
    path = UPLOAD_DIR / f"profile_{uid}.{row['photo_ext']}"
    if not path.exists():
        return JSONResponse({"error": "no photo"}, status_code=404)
    media_type = f"image/{'jpeg' if row['photo_ext'] == 'jpg' else row['photo_ext']}"
    return FileResponse(path, media_type=media_type)


@app.post("/profile/photo")
async def profile_photo_upload(photo: UploadFile = File(...)):
    uid = _current_user_id.get()
    data = await _read_limited_upload(photo, MAX_PHOTO_SIZE)
    if data is None:
        return RedirectResponse(url="/profile?saved=0", status_code=303)
    ext = _detect_image_ext(data)
    if not ext:
        return RedirectResponse(url="/profile?saved=0", status_code=303)
    # Remove old photos for this user with different extensions
    for old_ext in _ALLOWED_PHOTO_TYPES.values():
        old_path = UPLOAD_DIR / f"profile_{uid}.{old_ext}"
        if old_path.exists():
            old_path.unlink()
    (UPLOAD_DIR / f"profile_{uid}.{ext}").write_bytes(data)
    with get_db() as conn:
        conn.execute(
            "UPDATE user_profile SET photo_ext=? WHERE id=?",
            (ext, uid),
        )
        conn.commit()
    return RedirectResponse(url="/profile?saved=1", status_code=303)


@app.post("/profile/photo/delete")
def profile_photo_delete():
    uid = _current_user_id.get()
    with get_db() as conn:
        row = conn.execute("SELECT photo_ext FROM user_profile WHERE id = ?", (uid,)).fetchone()
        if row and row["photo_ext"]:
            path = UPLOAD_DIR / f"profile_{uid}.{row['photo_ext']}"
            if path.exists():
                path.unlink()
        conn.execute("UPDATE user_profile SET photo_ext='' WHERE id=?", (uid,))
        conn.commit()
    return RedirectResponse(url="/profile", status_code=303)


# ── Physician portal ──────────────────────────────────────────────────────────

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


@app.get("/physician/signup", response_class=HTMLResponse)
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


@app.post("/physician/signup")
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


@app.get("/physician/login", response_class=HTMLResponse)
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


@app.post("/physician/login")
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


@app.post("/physician/logout")
def physician_logout():
    resp = RedirectResponse(url="/physician/login", status_code=303)
    resp.delete_cookie(PHYSICIAN_COOKIE_NAME)
    resp.delete_cookie(PHYSICIAN_CTX_COOKIE)
    return resp


@app.get("/physician", response_class=HTMLResponse)
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


@app.post("/physician/patients/add")
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


@app.post("/physician/patients/remove")
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


@app.post("/physician/switch/{patient_id}")
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


@app.post("/physician/exit")
def physician_exit():
    resp = RedirectResponse(url="/physician", status_code=303)
    resp.delete_cookie(PHYSICIAN_CTX_COOKIE)
    return resp
