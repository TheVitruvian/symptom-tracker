import html
from datetime import datetime, timedelta

from fastapi import APIRouter, Body, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from config import _current_user_id, _from_utc_storage, _now_local, _to_utc_storage
from db import get_db
from ui import PAGE_STYLE, _nav_bar, _sidebar, _severity_color

router = APIRouter()
SOFT_DELETE_RECOVERY_SECONDS = 20


def _client_now_or_server() -> datetime:
    return _now_local()


def _validate_symptom_payload(
    name: str,
    severity: int,
    notes: str,
    symptom_date: str,
    end_date: str,
):
    if not name.strip():
        return ("Symptom name is required", None, None)
    if not (1 <= severity <= 10):
        return ("Severity must be between 1 and 10", None, None)
    try:
        ts_dt = datetime.strptime(symptom_date, "%Y-%m-%dT%H:%M")
    except ValueError:
        return ("Invalid date format", None, None)
    now_ref = _client_now_or_server()
    if ts_dt > now_ref:
        return ("Date cannot be in the future", None, None)
    end_dt = None
    if end_date.strip():
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%dT%H:%M")
        except ValueError:
            return ("Invalid end date format", None, None)
        if end_dt > now_ref:
            return ("End date cannot be in the future", None, None)
        if end_dt <= ts_dt:
            return ("End date must be after start date", None, None)
    return ("", ts_dt, end_dt)


def _fmt_duration(start_str: str, end_str: str) -> str:
    """Return a human-readable duration string for a symptom entry."""
    if not end_str:
        return _from_utc_storage(start_str).strftime("%Y-%m-%d %H:%M:%S")
    start_dt = _from_utc_storage(start_str)
    end_dt = _from_utc_storage(end_str)
    start_time = start_dt.strftime("%H:%M")
    end_time_str = end_dt.strftime("%H:%M")
    if start_dt.date() == end_dt.date():
        return f"{start_dt.strftime('%b')} {start_dt.day} \u2014 {start_time}\u2013{end_time_str}"
    else:
        return (
            f"{start_dt.strftime('%b')} {start_dt.day} {start_time}"
            f" \u2013 {end_dt.strftime('%b')} {end_dt.day} {end_time_str}"
        )


@router.get("/symptoms")
def symptoms_list():
    return RedirectResponse(url="/symptoms/chart", status_code=303)


@router.get("/symptoms/new", response_class=HTMLResponse)
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
          <label for="symptom_date">Start date &amp; time <span style="color:#aaa;font-weight:400">(defaults to now)</span></label>
          <input type="datetime-local" id="symptom_date" name="symptom_date" required
                 style="width:auto;">
        </div>

        <div class="form-group">
          <label for="end_date">End date &amp; time
            <span style="color:#aaa;font-weight:400">(optional — leave blank for a single moment)</span>
          </label>
          <input type="datetime-local" id="end_date" name="end_date" style="width:auto;">
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
    const startEl = document.getElementById("symptom_date");
    const endEl   = document.getElementById("end_date");
    startEl.value = localStr;
    startEl.max   = localStr;
    endEl.max     = localStr;
    startEl.addEventListener("change", () => {{
      endEl.min = startEl.value;
      if (endEl.value && endEl.value <= startEl.value) endEl.value = "";
    }});
  </script>
</body>
</html>
"""


@router.post("/symptoms")
def symptoms_create(
    name: str = Form(...),
    severity: int = Form(...),
    notes: str = Form(""),
    symptom_date: str = Form(...),
    end_date: str = Form(""),
):
    error, ts_dt, end_dt = _validate_symptom_payload(name, severity, notes, symptom_date, end_date)
    if error:
        return RedirectResponse(url="/symptoms/new?error=" + error.replace(" ", "+"), status_code=303)
    timestamp = _to_utc_storage(ts_dt)
    end_time = ""
    if end_dt:
        end_time = _to_utc_storage(end_dt)
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO symptoms (name, severity, notes, timestamp, end_time, user_id, deleted_at)"
            " VALUES (?, ?, ?, ?, ?, ?, '')",
            (name.strip(), severity, notes.strip(), timestamp, end_time, uid),
        )
        conn.commit()
    return RedirectResponse(url="/symptoms", status_code=303)


@router.post("/symptoms/delete")
def symptoms_delete(id: int = Form(...)):
    uid = _current_user_id.get()
    deleted_at = _to_utc_storage(_client_now_or_server())
    with get_db() as conn:
        conn.execute(
            "UPDATE symptoms SET deleted_at = ? WHERE id = ? AND user_id = ? AND deleted_at = ''",
            (deleted_at, id, uid),
        )
        conn.commit()
    return RedirectResponse(url="/symptoms", status_code=303)


@router.get("/symptoms/{sym_id}/edit", response_class=HTMLResponse)
def symptoms_edit_get(sym_id: int, error: str = ""):
    uid = _current_user_id.get()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM symptoms WHERE id = ? AND user_id = ? AND deleted_at = ''", (sym_id, uid)
        ).fetchone()
    if row is None:
        return RedirectResponse(url="/symptoms", status_code=303)
    e = dict(row)
    dt_local = _from_utc_storage(e["timestamp"]).strftime("%Y-%m-%dT%H:%M")
    end_local = _from_utc_storage(e["end_time"]).strftime("%Y-%m-%dT%H:%M") if e.get("end_time") else ""
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
          <label for="symptom_date">Start date &amp; time</label>
          <input type="datetime-local" id="symptom_date" name="symptom_date"
                 value="{dt_local}" required style="width:auto;">
        </div>
        <div class="form-group">
          <label for="end_date">End date &amp; time
            <span style="color:#aaa;font-weight:400">(optional)</span>
          </label>
          <input type="datetime-local" id="end_date" name="end_date"
                 value="{end_local}" style="width:auto;">
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
    const _nowStr = _local.toISOString().slice(0, 16);
    const _startEl = document.getElementById("symptom_date");
    const _endEl   = document.getElementById("end_date");
    _startEl.max = _nowStr;
    _endEl.max   = _nowStr;
    _endEl.min   = _startEl.value;
    _startEl.addEventListener("change", () => {{
      _endEl.min = _startEl.value;
      if (_endEl.value && _endEl.value <= _startEl.value) _endEl.value = "";
    }});
  </script>
</body>
</html>"""


@router.post("/symptoms/{sym_id}/edit")
def symptoms_edit_post(
    sym_id: int,
    name: str = Form(...),
    severity: int = Form(...),
    notes: str = Form(""),
    symptom_date: str = Form(...),
    end_date: str = Form(""),
):
    error, ts_dt, end_dt = _validate_symptom_payload(name, severity, notes, symptom_date, end_date)
    if error:
        return RedirectResponse(
            url=f"/symptoms/{sym_id}/edit?error=" + error.replace(" ", "+"), status_code=303
        )
    timestamp = _to_utc_storage(ts_dt)
    end_time = ""
    if end_dt:
        end_time = _to_utc_storage(end_dt)
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "UPDATE symptoms SET name = ?, severity = ?, notes = ?, timestamp = ?, end_time = ?"
            " WHERE id = ? AND user_id = ? AND deleted_at = ''",
            (name.strip(), severity, notes.strip(), timestamp, end_time, sym_id, uid),
        )
        conn.commit()
    return RedirectResponse(url="/symptoms", status_code=303)


@router.get("/api/symptoms")
def api_symptoms():
    uid = _current_user_id.get()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, severity, notes, timestamp, end_time FROM symptoms"
            " WHERE user_id = ? AND deleted_at = '' ORDER BY timestamp ASC",
            (uid,),
        ).fetchall()
    items = []
    for r in rows:
        item = dict(r)
        item["timestamp"] = _from_utc_storage(item["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
        if item["end_time"]:
            item["end_time"] = _from_utc_storage(item["end_time"]).strftime("%Y-%m-%d %H:%M:%S")
        items.append(item)
    return JSONResponse({"symptoms": items})


@router.post("/api/symptoms")
def api_symptoms_create(payload: dict = Body(...)):
    uid = _current_user_id.get()
    name = str(payload.get("name", ""))
    severity = int(payload.get("severity", 0))
    notes = str(payload.get("notes", ""))
    symptom_date = str(payload.get("symptom_date", ""))
    end_date = str(payload.get("end_date", ""))
    error, ts_dt, end_dt = _validate_symptom_payload(name, severity, notes, symptom_date, end_date)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    timestamp = _to_utc_storage(ts_dt)
    end_time = _to_utc_storage(end_dt) if end_dt else ""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO symptoms (name, severity, notes, timestamp, end_time, user_id, deleted_at)"
            " VALUES (?, ?, ?, ?, ?, ?, '')",
            (name.strip(), severity, notes.strip(), timestamp, end_time, uid),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, name, severity, notes, timestamp, end_time FROM symptoms WHERE id = ? AND user_id = ?",
            (cur.lastrowid, uid),
        ).fetchone()
    item = dict(row)
    item["timestamp"] = _from_utc_storage(item["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
    if item["end_time"]:
        item["end_time"] = _from_utc_storage(item["end_time"]).strftime("%Y-%m-%d %H:%M:%S")
    return JSONResponse({"ok": True, "symptom": item})


@router.post("/api/symptoms/{sym_id}/edit")
def api_symptoms_edit(sym_id: int, payload: dict = Body(...)):
    uid = _current_user_id.get()
    name = str(payload.get("name", ""))
    severity = int(payload.get("severity", 0))
    notes = str(payload.get("notes", ""))
    symptom_date = str(payload.get("symptom_date", ""))
    end_date = str(payload.get("end_date", ""))
    error, ts_dt, end_dt = _validate_symptom_payload(name, severity, notes, symptom_date, end_date)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    timestamp = _to_utc_storage(ts_dt)
    end_time = _to_utc_storage(end_dt) if end_dt else ""
    with get_db() as conn:
        exists = conn.execute(
            "SELECT id FROM symptoms WHERE id = ? AND user_id = ? AND deleted_at = ''", (sym_id, uid)
        ).fetchone()
        if not exists:
            return JSONResponse({"ok": False, "error": "Symptom not found"}, status_code=404)
        conn.execute(
            "UPDATE symptoms SET name = ?, severity = ?, notes = ?, timestamp = ?, end_time = ?"
            " WHERE id = ? AND user_id = ? AND deleted_at = ''",
            (name.strip(), severity, notes.strip(), timestamp, end_time, sym_id, uid),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, name, severity, notes, timestamp, end_time FROM symptoms WHERE id = ? AND user_id = ?",
            (sym_id, uid),
        ).fetchone()
    item = dict(row)
    item["timestamp"] = _from_utc_storage(item["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
    if item["end_time"]:
        item["end_time"] = _from_utc_storage(item["end_time"]).strftime("%Y-%m-%d %H:%M:%S")
    return JSONResponse({"ok": True, "symptom": item})


@router.post("/api/symptoms/{sym_id}/soft-delete")
def api_symptoms_soft_delete(sym_id: int):
    uid = _current_user_id.get()
    deleted_at = _to_utc_storage(_client_now_or_server())
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM symptoms WHERE id = ? AND user_id = ? AND deleted_at = ''",
            (sym_id, uid),
        ).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "Symptom not found"}, status_code=404)
        conn.execute(
            "UPDATE symptoms SET deleted_at = ? WHERE id = ? AND user_id = ?",
            (deleted_at, sym_id, uid),
        )
        conn.commit()
    return JSONResponse({"ok": True, "undo_window_seconds": SOFT_DELETE_RECOVERY_SECONDS})


@router.post("/api/symptoms/{sym_id}/restore")
def api_symptoms_restore(sym_id: int):
    uid = _current_user_id.get()
    now_local = _client_now_or_server()
    with get_db() as conn:
        row = conn.execute(
            "SELECT deleted_at FROM symptoms WHERE id = ? AND user_id = ?",
            (sym_id, uid),
        ).fetchone()
        if not row:
            return JSONResponse({"ok": False, "error": "Symptom not found"}, status_code=404)
        deleted_at_raw = row["deleted_at"] or ""
        if not deleted_at_raw:
            return JSONResponse({"ok": False, "error": "Symptom is not deleted"}, status_code=400)
        deleted_at_local = _from_utc_storage(deleted_at_raw)
        if now_local - deleted_at_local > timedelta(seconds=SOFT_DELETE_RECOVERY_SECONDS):
            return JSONResponse({"ok": False, "error": "Undo window expired"}, status_code=400)
        conn.execute(
            "UPDATE symptoms SET deleted_at = '' WHERE id = ? AND user_id = ?",
            (sym_id, uid),
        )
        conn.commit()
    return JSONResponse({"ok": True})
