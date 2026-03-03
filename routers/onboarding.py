import html
from datetime import date

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from config import _current_user_id, FREQ_LABELS
from db import get_db
from ui import PAGE_STYLE

router = APIRouter()

_TOTAL_STEPS = 3


def _progress_dots(current_step: int) -> str:
    dots = ""
    for i in range(1, _TOTAL_STEPS + 1):
        if i < current_step:
            color = "#7c3aed"
            symbol = "&#9679;"  # filled circle
        elif i == current_step:
            color = "#7c3aed"
            symbol = "&#9679;"
        else:
            color = "#d1d5db"
            symbol = "&#9675;"  # empty circle
        dots += f'<span style="font-size:18px; color:{color}; margin:0 3px;">{symbol}</span>'
    return dots


def _shell(step: int, title: str, body: str) -> str:
    dots = _progress_dots(step)
    skip_link = (
        '<p style="text-align:center; margin-top:24px; font-size:13px;">'
        '<a href="/onboarding/skip" style="color:#9ca3af; text-decoration:none;">Skip setup</a>'
        "</p>"
    )
    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}<title>{html.escape(title)}</title></head>
<body>
  <div class="container" style="max-width:480px;">
    <div style="text-align:center; margin-bottom:24px;">
      <p style="font-size:13px; color:#6b7280; margin:0 0 8px;">Step {step} of {_TOTAL_STEPS}</p>
      <div style="margin-bottom:4px;">{dots}</div>
      <h1 style="margin:12px 0 0; font-size:22px;">{html.escape(title)}</h1>
    </div>
    {body}
    {skip_link}
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Step 1: About You
# ---------------------------------------------------------------------------

@router.get("/onboarding/1", response_class=HTMLResponse)
def onboarding_step1_get():
    uid = _current_user_id.get()
    with get_db() as conn:
        row = conn.execute("SELECT name, dob FROM user_profile WHERE id = ?", (uid,)).fetchone()
    name_val = html.escape(row["name"] if row else "")
    dob_val = html.escape(row["dob"] if row else "")
    body = f"""
    <p style="color:#555; font-size:14px; margin-bottom:20px; text-align:center;">
      Let's start with some basic information about you.
    </p>
    <form method="post" action="/onboarding/1" data-ajax>
      <div class="form-error" style="display:none"></div>
      <div class="form-group">
        <label for="name">Your name</label>
        <input type="text" id="name" name="name" value="{name_val}"
          placeholder="e.g. Alex Smith" autocomplete="name">
      </div>
      <div class="form-group">
        <label for="dob">Date of birth</label>
        <input type="date" id="dob" name="dob" value="{dob_val}" data-no-client-default>
      </div>
      <button type="submit" class="btn-primary" style="width:100%;">Continue &rarr;</button>
    </form>"""
    return _shell(1, "About You", body)


@router.post("/onboarding/1")
def onboarding_step1_post(name: str = Form(""), dob: str = Form("")):
    if len(name) > 120:
        return JSONResponse({"ok": False, "error": "Name must be 120 characters or fewer"}, status_code=400)
    if dob:
        from datetime import datetime
        try:
            datetime.strptime(dob, "%Y-%m-%d")
        except ValueError:
            return JSONResponse({"ok": False, "error": "Invalid date of birth"}, status_code=400)
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "UPDATE user_profile SET name=?, dob=? WHERE id=?",
            (name.strip(), dob, uid),
        )
        conn.commit()
    return JSONResponse({"ok": True, "redirect": "/onboarding/2"})


# ---------------------------------------------------------------------------
# Step 2: Your Conditions
# ---------------------------------------------------------------------------

@router.get("/onboarding/2", response_class=HTMLResponse)
def onboarding_step2_get():
    uid = _current_user_id.get()
    with get_db() as conn:
        row = conn.execute("SELECT conditions FROM user_profile WHERE id = ?", (uid,)).fetchone()
    conditions_val = html.escape(row["conditions"] if row else "")
    body = f"""
    <p style="color:#555; font-size:14px; margin-bottom:20px; text-align:center;">
      List any known medical conditions. This helps give context to your symptom data.
    </p>
    <form method="post" action="/onboarding/2" data-ajax>
      <div class="form-error" style="display:none"></div>
      <div class="form-group">
        <label for="conditions">Known conditions</label>
        <textarea id="conditions" name="conditions" rows="4"
          placeholder="e.g. Type 2 diabetes, hypertension, migraines">{conditions_val}</textarea>
      </div>
      <button type="submit" class="btn-primary" style="width:100%;">Continue &rarr;</button>
    </form>"""
    return _shell(2, "Your Conditions", body)


@router.post("/onboarding/2")
def onboarding_step2_post(conditions: str = Form("")):
    if len(conditions) > 2000:
        return JSONResponse({"ok": False, "error": "Conditions must be 2000 characters or fewer"}, status_code=400)
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "UPDATE user_profile SET conditions=? WHERE id=?",
            (conditions.strip(), uid),
        )
        conn.commit()
    return JSONResponse({"ok": True, "redirect": "/onboarding/3"})


# ---------------------------------------------------------------------------
# Step 3: First Medication
# ---------------------------------------------------------------------------

@router.get("/onboarding/3", response_class=HTMLResponse)
def onboarding_step3_get():
    freq_options = "".join(
        f'<option value="{k}"{"selected" if k == "once_daily" else ""}>{v}</option>'
        for k, v in FREQ_LABELS.items()
    )
    body = f"""
    <p style="color:#555; font-size:14px; margin-bottom:20px; text-align:center;">
      Add your first medication to start tracking adherence.
    </p>
    <form method="post" action="/onboarding/3" data-ajax>
      <div class="form-error" style="display:none"></div>
      <div class="form-group">
        <label for="med_name">Medication name</label>
        <input type="text" id="med_name" name="med_name"
          placeholder="e.g. Metformin" autocomplete="off">
      </div>
      <div class="form-group">
        <label for="med_dose">Dose <span style="color:#aaa; font-weight:400">(optional)</span></label>
        <input type="text" id="med_dose" name="med_dose"
          placeholder="e.g. 500mg" autocomplete="off">
      </div>
      <div class="form-group">
        <label for="med_frequency">Frequency</label>
        <select id="med_frequency" name="med_frequency">{freq_options}</select>
      </div>
      <button type="submit" class="btn-primary" style="width:100%;">Add &amp; Finish</button>
    </form>
    <p style="text-align:center; margin-top:14px; font-size:13px;">
      <a href="/onboarding/skip" style="color:#6b7280;">Skip this step &rarr;</a>
    </p>"""
    return _shell(3, "First Medication", body)


@router.post("/onboarding/3")
def onboarding_step3_post(
    med_name: str = Form(""),
    med_dose: str = Form(""),
    med_frequency: str = Form("once_daily"),
):
    if len(med_name) > 120:
        return JSONResponse({"ok": False, "error": "Medication name must be 120 characters or fewer"}, status_code=400)
    if len(med_dose) > 80:
        return JSONResponse({"ok": False, "error": "Dose must be 80 characters or fewer"}, status_code=400)
    if med_frequency not in FREQ_LABELS:
        med_frequency = "once_daily"
    uid = _current_user_id.get()
    with get_db() as conn:
        if med_name.strip():
            today = str(date.today())
            conn.execute(
                "INSERT INTO medication_schedules"
                " (user_id, name, dose, frequency, start_date, created_at, active)"
                " VALUES (?, ?, ?, ?, ?, ?, 1)",
                (uid, med_name.strip(), med_dose.strip(), med_frequency, today, today + " 00:00:00"),
            )
        conn.execute(
            "UPDATE user_profile SET onboarding_complete=1 WHERE id=?", (uid,)
        )
        conn.commit()
    return JSONResponse({"ok": True, "redirect": "/onboarding/done"})


# ---------------------------------------------------------------------------
# Skip & Done
# ---------------------------------------------------------------------------

@router.get("/onboarding/skip")
def onboarding_skip():
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "UPDATE user_profile SET onboarding_complete=1 WHERE id=?", (uid,)
        )
        conn.commit()
    return RedirectResponse(url="/", status_code=303)


@router.get("/onboarding/done", response_class=HTMLResponse)
def onboarding_done():
    body = """
    <div style="text-align:center;">
      <div style="font-size:48px; margin-bottom:12px;">&#10003;</div>
      <p style="color:#555; font-size:15px; margin-bottom:24px; line-height:1.5;">
        Your account is set up. You can now track symptoms, log medications,
        and view your health report — all in one place.
      </p>
      <a href="/" style="display:inline-block; background:#7c3aed; color:#fff; border-radius:8px;
        padding:12px 28px; font-size:15px; font-weight:600; text-decoration:none;">
        Go to Dashboard &rarr;
      </a>
    </div>"""
    # Done screen has no progress dots or skip link — use a minimal wrapper
    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}<title>You're all set!</title></head>
<body>
  <div class="container" style="max-width:480px;">
    <div style="text-align:center; margin-bottom:28px;">
      <h1 style="font-size:24px; color:#15803d;">You're all set!</h1>
    </div>
    {body}
  </div>
</body>
</html>
"""
