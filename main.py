from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from config import (
    PUBLIC_PATHS,
    PHYSICIAN_CTX_COOKIE,
    _current_user_id,
    _physician_ctx,
    _set_client_clock,
    UPLOAD_DIR,
)
from db import init_db, get_db
from security import (
    _get_authenticated_user,
    _get_authenticated_physician,
    _physician_owns_patient,
    _has_any_patient,
    _ensure_csrf_cookie,
    _csrf_header_valid,
    _csrf_query_valid,
    _is_same_origin,
)
from routers import auth, symptoms, symptoms_analytics, medications, medications_adherence, profile, physician

# Startup
init_db()

# Migrate legacy single-user photo (profile.{ext}) to per-user naming (profile_1.{ext})
for _ext in ["jpg", "png", "gif", "webp"]:
    _old_photo = UPLOAD_DIR / f"profile.{_ext}"
    _new_photo = UPLOAD_DIR / f"profile_1.{_ext}"
    if _old_photo.exists() and not _new_photo.exists():
        _old_photo.rename(_new_photo)

app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    _set_client_clock(request.cookies.get("tz_offset", ""))
    path = request.url.path
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if not _is_same_origin(request):
            if path.startswith("/api/"):
                return JSONResponse({"error": "forbidden"}, status_code=403)
            return RedirectResponse(url="/login?error=Forbidden+request", status_code=303)
        if path.startswith("/api/"):
            if not _csrf_header_valid(request):
                return JSONResponse({"error": "forbidden"}, status_code=403)
        elif path not in PUBLIC_PATHS and not path.startswith("/physician"):
            # Require CSRF query param for authenticated patient form POSTs.
            # Excludes public routes (login/signup) and physician routes which
            # have their own auth and don't use the JS form injection.
            if not _csrf_query_valid(request):
                return RedirectResponse(url="/login?error=Forbidden+request", status_code=303)
    # Physician-only routes
    if path.startswith("/physician"):
        if path in {"/physician/login", "/physician/signup"}:
            return _ensure_csrf_cookie(request, await call_next(request))
        physician_user = _get_authenticated_physician(request)
        if not physician_user:
            return RedirectResponse(url="/physician/login", status_code=303)
        return _ensure_csrf_cookie(request, await call_next(request))

    # Patient public paths (login / signup / logout)
    if path in PUBLIC_PATHS:
        return _ensure_csrf_cookie(request, await call_next(request))

    # Check physician-in-patient-context
    physician_user = _get_authenticated_physician(request)
    if physician_user:
        ctx_cookie = request.cookies.get(PHYSICIAN_CTX_COOKIE, "")
        if ctx_cookie:
            try:
                patient_id = int(ctx_cookie)
            except ValueError:
                patient_id = None
            if patient_id and _physician_owns_patient(physician_user["id"], patient_id):
                if request.method not in ("GET", "HEAD", "OPTIONS"):
                    if path.startswith("/api/"):
                        return JSONResponse({"error": "Physicians cannot modify patient data"}, status_code=403)
                    return RedirectResponse(url="/physician", status_code=303)
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


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if _get_authenticated_physician(request):
        return RedirectResponse(url="/physician", status_code=303)
    if _get_authenticated_user(request):
        return RedirectResponse(url="/symptoms/chart", status_code=303)
    return """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Symptom Tracker</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f0f4ff; min-height: 100vh;
           display: flex; align-items: center; justify-content: center; padding: 24px; }
    .hero { text-align: center; max-width: 480px; width: 100%; }
    .logo { font-size: 48px; margin-bottom: 16px; }
    h1 { font-size: 32px; font-weight: 800; color: #1e3a8a; margin-bottom: 8px; }
    .subtitle { font-size: 16px; color: #6b7280; margin-bottom: 40px; line-height: 1.5; }
    .cards { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
    .card { background: #fff; border: 1px solid #e5e7eb; border-radius: 12px;
            padding: 28px 20px; text-decoration: none; color: inherit;
            transition: box-shadow .15s, transform .15s; display: block; }
    .card:hover { box-shadow: 0 8px 24px rgba(0,0,0,.10); transform: translateY(-2px); }
    .card-icon { font-size: 32px; margin-bottom: 12px; }
    .card-title { font-size: 17px; font-weight: 700; color: #111; margin-bottom: 6px; }
    .card-desc { font-size: 13px; color: #6b7280; line-height: 1.5; }
    .card.patient { border-top: 4px solid #3b82f6; }
    .card.physician { border-top: 4px solid #7c3aed; }
    @media (max-width: 420px) { .cards { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <div class="hero">
    <div class="logo">&#128203;</div>
    <h1>Symptom Tracker</h1>
    <p class="subtitle">Log symptoms and medications, spot patterns over time,
      and share data with your care team.</p>
    <div class="cards">
      <a href="/login" class="card patient">
        <div class="card-icon">&#129730;</div>
        <div class="card-title">I'm a Patient</div>
        <div class="card-desc">Log in to track your symptoms and medications.</div>
      </a>
      <a href="/physician/login" class="card physician">
        <div class="card-icon">&#128104;&#8205;&#9877;&#65039;</div>
        <div class="card-title">I'm a Physician</div>
        <div class="card-desc">Log in to view and manage your patients' data.</div>
      </a>
    </div>
  </div>
</body>
</html>
"""


app.include_router(auth.router)
app.include_router(symptoms.router)
app.include_router(symptoms_analytics.router)
app.include_router(medications.router)
app.include_router(medications_adherence.router)
app.include_router(profile.router)
app.include_router(physician.router)
