import html
from datetime import datetime

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from config import _current_user_id
from db import get_db
from ui import PAGE_STYLE, _nav_bar, _sidebar, _severity_color

router = APIRouter()


@router.get("/symptoms", response_class=HTMLResponse)
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


@router.post("/symptoms")
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


@router.post("/symptoms/delete")
def symptoms_delete(id: int = Form(...)):
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute("DELETE FROM symptoms WHERE id = ? AND user_id = ?", (id, uid))
        conn.commit()
    return RedirectResponse(url="/symptoms", status_code=303)


@router.get("/symptoms/{sym_id}/edit", response_class=HTMLResponse)
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


@router.post("/symptoms/{sym_id}/edit")
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


@router.get("/api/symptoms")
def api_symptoms():
    uid = _current_user_id.get()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, severity, notes, timestamp FROM symptoms"
            " WHERE user_id = ? ORDER BY timestamp ASC",
            (uid,),
        ).fetchall()
    return JSONResponse({"symptoms": [dict(r) for r in rows]})
