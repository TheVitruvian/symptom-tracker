import html
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from config import _current_user_id, UPLOAD_DIR, MAX_PHOTO_SIZE
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
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "UPDATE user_profile SET name=?, dob=?, conditions=?, medications=?, email=? WHERE id=?",
            (name.strip(), dob, conditions.strip(), medications.strip(), email.strip().lower(), uid),
        )
        conn.commit()
    return JSONResponse({"status": "ok"})


@router.get("/profile", response_class=HTMLResponse)
def profile_get(saved: int = 0, error: str = ""):
    uid = _current_user_id.get()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM user_profile WHERE id = ?", (uid,)).fetchone()
    p = dict(row) if row else {"name": "", "dob": "", "conditions": "", "medications": "", "photo_ext": "", "share_code": "", "email": ""}
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


@router.post("/profile")
def profile_update(
    name: str = Form(""),
    dob: str = Form(""),
    conditions: str = Form(""),
    medications: str = Form(""),
    email: str = Form(""),
):
    if dob:
        try:
            datetime.strptime(dob, "%Y-%m-%d")
        except ValueError:
            return RedirectResponse(url="/profile?error=Invalid+date+of+birth", status_code=303)
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "UPDATE user_profile SET name=?, dob=?, conditions=?, medications=?, email=? WHERE id=?",
            (name.strip(), dob, conditions.strip(), medications.strip(), email.strip().lower(), uid),
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
