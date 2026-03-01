import html
import io
import re
import secrets
from datetime import datetime
from typing import Optional

from PIL import Image

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_MAX_IMAGE_DIMENSION = 8000  # pixels per side

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from config import _current_user_id, _from_utc_storage, UPLOAD_DIR, MAX_PHOTO_SIZE, FREQ_LABELS
from db import get_db
from security import _hash_password, _verify_password, _set_session_cookie
from ui import PAGE_STYLE, _nav_bar, _calc_age

router = APIRouter()

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


@router.get("/api/profile")
def api_profile_get():
    uid = _current_user_id.get()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM user_profile WHERE id = ?", (uid,)).fetchone()
    if row is None:
        return JSONResponse({"name": "", "dob": "", "conditions": "", "medications": ""})
    data = dict(row)
    data.pop("password_hash", None)
    return JSONResponse(data)


@router.post("/api/profile")
def api_profile_update(
    name: str = Form(""),
    dob: str = Form(""),
    conditions: str = Form(""),
    medications: str = Form(""),
    email: str = Form(""),
):
    if len(name) > 120:
        return JSONResponse({"status": "error", "error": "Name must be 120 characters or fewer"}, status_code=400)
    if len(conditions) > 2000:
        return JSONResponse({"status": "error", "error": "Conditions must be 2000 characters or fewer"}, status_code=400)
    email_clean = email.strip().lower()
    if email_clean and not _EMAIL_RE.match(email_clean):
        return JSONResponse({"status": "error", "error": "Invalid email address"}, status_code=400)
    if len(email_clean) > 254:
        return JSONResponse({"status": "error", "error": "Email address is too long"}, status_code=400)
    if dob:
        try:
            datetime.strptime(dob, "%Y-%m-%d")
        except ValueError:
            return JSONResponse({"status": "error", "error": "Invalid date of birth"}, status_code=400)
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "UPDATE user_profile SET name=?, dob=?, conditions=?, medications=?, email=? WHERE id=?",
            (name.strip(), dob, conditions.strip(), medications.strip(), email_clean, uid),
        )
        conn.commit()
    return JSONResponse({"status": "ok"})


def _medications_from_schedules(conn, uid: int) -> str:
    schedules = conn.execute(
        "SELECT DISTINCT ms.name, ms.dose, ms.frequency FROM medication_schedules ms"
        " JOIN medication_doses md ON md.schedule_id = ms.id AND md.status = 'taken'"
        " WHERE ms.user_id=? AND ms.active=1 ORDER BY ms.name",
        (uid,),
    ).fetchall()
    lines = []
    seen = set()
    for r in schedules:
        freq = FREQ_LABELS.get(r["frequency"], r["frequency"]).lower()
        parts = [r["name"]]
        if r["dose"]:
            parts.append(r["dose"])
        parts.append(f"– {freq}")
        lines.append(" ".join(parts))
        seen.add(r["name"].lower())
    # Also include distinct entries from the ad-hoc log not already covered
    log_rows = conn.execute(
        "SELECT name, dose FROM medications WHERE user_id=?"
        " GROUP BY name ORDER BY MAX(timestamp) DESC",
        (uid,),
    ).fetchall()
    for r in log_rows:
        if r["name"].lower() not in seen:
            parts = [r["name"]]
            if r["dose"]:
                parts.append(r["dose"])
            lines.append(" ".join(parts))
            seen.add(r["name"].lower())
    return "\n".join(lines)


def _physician_access_card(share_code: str, linked_physicians, access_log) -> str:
    if linked_physicians:
        rows = ""
        for ph in linked_physicians:
            ph_name = html.escape(ph["username"])
            ph_name_js = html.escape(ph["username"], quote=True)
            ph_id = ph["id"]
            rows += f"""
        <div style="display:flex; align-items:center; justify-content:space-between;
                    padding:8px 0; border-bottom:1px solid #e0f2fe;">
          <span style="font-size:14px; color:#0c4a6e; font-weight:500;">{ph_name}</span>
          <form method="post" action="/profile/physicians/{ph_id}/revoke" style="margin:0;">
            <button type="submit"
              style="background:none; border:1px solid #ef4444; border-radius:6px; color:#ef4444;
                     font-size:12px; padding:4px 10px; cursor:pointer; font-family:inherit;"
              onclick="return confirm('Revoke access for {ph_name_js}? They will no longer be able to view your data.')">
              Revoke
            </button>
          </form>
        </div>"""
        physician_list = f"""
      <div style="margin-top:14px;">
        <p style="font-size:13px; color:#0c4a6e; margin:0 0 6px; font-weight:600;">Physicians with access</p>
        {rows}
      </div>"""
    else:
        physician_list = """
      <p style="font-size:13px; color:#6b7280; margin:12px 0 0; font-style:italic;">
        No physicians currently have access to your data.
      </p>"""

    if access_log:
        log_rows = ""
        for entry in access_log:
            try:
                dt = _from_utc_storage(entry["accessed_at"])
                ts = dt.strftime("%-d %b %Y at %-I:%M %p")
            except Exception:
                ts = entry["accessed_at"]
            log_rows += f"""
          <div style="display:flex; justify-content:space-between; padding:5px 0;
                      border-bottom:1px solid #e0f2fe; font-size:13px;">
            <span style="color:#0c4a6e; font-weight:500;">{html.escape(entry["username"])}</span>
            <span style="color:#6b7280;">{html.escape(ts)}</span>
          </div>"""
        log_section = f"""
      <details style="margin-top:14px;">
        <summary style="cursor:pointer; font-size:13px; font-weight:600; color:#0c4a6e;
                        list-style:none; display:flex; align-items:center; gap:6px;">
          <span>&#9654;</span> Access History (last {len(access_log)})
        </summary>
        <div style="margin-top:8px;">{log_rows}
        </div>
      </details>"""
    else:
        log_section = """
      <p style="font-size:13px; color:#6b7280; margin:10px 0 0; font-style:italic;">
        No physician access recorded yet.
      </p>"""

    return f"""
    <div style="margin-top:24px; background:#f0f9ff; border:1px solid #bae6fd; border-radius:8px; padding:14px 18px;">
      <p style="font-size:13px; color:#0c4a6e; margin:0 0 4px; font-weight:600;">Physician Access</p>
      <p style="font-size:13px; color:#0369a1; margin:0 0 10px;">
        Share this code with your physician to give them access to your data.
        Regenerate it at any time to invalidate the old code.
      </p>
      <div style="display:flex; align-items:center; gap:14px; flex-wrap:wrap;">
        <code style="font-size:20px; font-weight:700; letter-spacing:3px; color:#1e3a8a;">{html.escape(share_code)}</code>
        <form method="post" action="/profile/share-code/regenerate" style="margin:0;">
          <button type="submit"
            style="background:none; border:1px solid #0369a1; border-radius:6px; color:#0369a1;
                   font-size:12px; padding:5px 12px; cursor:pointer; font-family:inherit;"
            onclick="return confirm('Generate a new share code? Your current code will stop working.')">
            Regenerate
          </button>
        </form>
      </div>
      {physician_list}
      {log_section}
    </div>"""


@router.get("/profile", response_class=HTMLResponse)
def profile_get(saved: int = 0, error: str = ""):
    uid = _current_user_id.get()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM user_profile WHERE id = ?", (uid,)).fetchone()
        sched_text = _medications_from_schedules(conn, uid)
        linked_physicians = conn.execute(
            "SELECT p.id, p.username FROM physicians p"
            " JOIN physician_patients pp ON pp.physician_id = p.id"
            " WHERE pp.patient_id = ? ORDER BY p.username",
            (uid,),
        ).fetchall()
        access_log = conn.execute(
            "SELECT p.username, al.accessed_at"
            " FROM physician_access_log al"
            " JOIN physicians p ON p.id = al.physician_id"
            " WHERE al.patient_id = ?"
            " ORDER BY al.accessed_at DESC LIMIT 50",
            (uid,),
        ).fetchall()
    p = dict(row) if row else {"name": "", "dob": "", "conditions": "", "medications": "", "photo_ext": "", "share_code": "", "email": ""}
    if sched_text:
        p["medications"] = sched_text
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
        <label for="email">Email <span style="color:#aaa;font-weight:400">(used for password reset)</span></label>
        <input type="email" id="email" name="email" value="{html.escape(p.get('email', ''))}"
          placeholder="you@example.com" autocomplete="email">
      </div>
      <div class="form-group">
        <label for="conditions">Known Conditions</label>
        <textarea id="conditions" name="conditions" rows="3"
          placeholder="e.g. migraines, asthma">{html.escape(p['conditions'])}</textarea>
      </div>
      <div class="form-group">
        <label for="medications">Medications <span style="font-size:12px; font-weight:400; color:#6b7280;">(auto-populated from your schedules &amp; log)</span></label>
        <textarea id="medications" name="medications" rows="3" readonly
          style="background:#f9fafb; color:#374151; cursor:default;">{html.escape(p['medications'])}</textarea>
      </div>
      <button type="submit" class="btn-primary">Save Profile</button>
    </form>
    {_physician_access_card(p.get("share_code", ""), linked_physicians, access_log)}
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


@router.post("/profile")
def profile_update(
    name: str = Form(""),
    dob: str = Form(""),
    conditions: str = Form(""),
    medications: str = Form(""),
    email: str = Form(""),
):
    if len(name) > 120:
        return RedirectResponse(url="/profile?error=Name+must+be+120+characters+or+fewer", status_code=303)
    if len(conditions) > 2000:
        return RedirectResponse(url="/profile?error=Conditions+must+be+2000+characters+or+fewer", status_code=303)
    email_clean = email.strip().lower()
    if email_clean and not _EMAIL_RE.match(email_clean):
        return RedirectResponse(url="/profile?error=Invalid+email+address", status_code=303)
    if len(email_clean) > 254:
        return RedirectResponse(url="/profile?error=Email+address+is+too+long", status_code=303)
    if dob:
        try:
            datetime.strptime(dob, "%Y-%m-%d")
        except ValueError:
            return RedirectResponse(url="/profile?error=Invalid+date+of+birth", status_code=303)
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "UPDATE user_profile SET name=?, dob=?, conditions=?, medications=?, email=? WHERE id=?",
            (name.strip(), dob, conditions.strip(), medications.strip(), email_clean, uid),
        )
        conn.commit()
    return RedirectResponse(url="/profile?saved=1", status_code=303)


@router.post("/profile/password")
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
    if len(new_password) > 1000:
        return RedirectResponse(url="/profile?error=New+password+is+too+long", status_code=303)
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


@router.get("/profile/photo")
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


@router.post("/profile/photo")
async def profile_photo_upload(photo: UploadFile = File(...)):
    uid = _current_user_id.get()
    data = await _read_limited_upload(photo, MAX_PHOTO_SIZE)
    if data is None:
        return RedirectResponse(url="/profile?error=Photo+must+be+under+5+MB", status_code=303)
    ext = _detect_image_ext(data)
    if not ext:
        return RedirectResponse(url="/profile?error=Unsupported+image+format", status_code=303)
    # Validate dimensions and strip EXIF/metadata via Pillow
    try:
        img = Image.open(io.BytesIO(data))
        if img.width > _MAX_IMAGE_DIMENSION or img.height > _MAX_IMAGE_DIMENSION:
            return RedirectResponse(
                url=f"/profile?error=Image+must+be+{_MAX_IMAGE_DIMENSION}px+or+smaller+in+each+dimension",
                status_code=303,
            )
        buf = io.BytesIO()
        fmt = {"jpg": "JPEG", "png": "PNG", "gif": "GIF", "webp": "WEBP"}[ext]
        img.save(buf, format=fmt)
        data = buf.getvalue()
    except Exception:
        return RedirectResponse(url="/profile?error=Could+not+process+image", status_code=303)
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


@router.post("/profile/photo/delete")
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


@router.post("/profile/sync-medications")
def profile_sync_medications():
    uid = _current_user_id.get()
    with get_db() as conn:
        meds_text = _medications_from_schedules(conn, uid)
        conn.execute(
            "UPDATE user_profile SET medications=? WHERE id=?",
            (meds_text, uid),
        )
        conn.commit()
    return RedirectResponse(url="/profile?saved=1", status_code=303)


@router.post("/profile/physicians/{physician_id}/revoke")
def profile_revoke_physician(physician_id: int):
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "DELETE FROM physician_patients WHERE physician_id = ? AND patient_id = ?",
            (physician_id, uid),
        )
        conn.commit()
    return RedirectResponse(url="/profile?saved=1", status_code=303)


@router.post("/profile/share-code/regenerate")
def profile_regenerate_share_code():
    uid = _current_user_id.get()
    new_code = secrets.token_hex(4).upper()
    with get_db() as conn:
        conn.execute(
            "UPDATE user_profile SET share_code = ? WHERE id = ?",
            (new_code, uid),
        )
        conn.commit()
    return RedirectResponse(url="/profile?saved=1", status_code=303)
