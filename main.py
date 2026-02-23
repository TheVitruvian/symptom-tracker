from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from config import PUBLIC_PATHS, PHYSICIAN_CTX_COOKIE, _current_user_id, _physician_ctx, UPLOAD_DIR
from db import init_db, get_db
from security import (
    _get_authenticated_user,
    _get_authenticated_physician,
    _physician_owns_patient,
    _has_any_patient,
    _ensure_csrf_cookie,
    _csrf_header_valid,
    _is_same_origin,
)
from routers import auth, symptoms, symptoms_analytics, medications, profile, physician

# Startup
init_db()

# Migrate legacy single-user photo (profile.{ext}) to per-user naming (profile_1.{ext})
for _ext in ["jpg", "png", "gif", "webp"]:
    _old_photo = UPLOAD_DIR / f"profile.{_ext}"
    _new_photo = UPLOAD_DIR / f"profile_1.{_ext}"
    if _old_photo.exists() and not _new_photo.exists():
        _old_photo.rename(_new_photo)

app = FastAPI()


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


app.include_router(auth.router)
app.include_router(symptoms.router)
app.include_router(symptoms_analytics.router)
app.include_router(medications.router)
app.include_router(profile.router)
app.include_router(physician.router)
