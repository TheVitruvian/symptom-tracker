import html
from datetime import datetime

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from config import _current_user_id
from db import get_db
from ui import PAGE_STYLE, _nav_bar

router = APIRouter()

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


@router.get("/api/medications")
def api_medications():
    uid = _current_user_id.get()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, dose, notes, timestamp FROM medications"
            " WHERE user_id = ? ORDER BY timestamp ASC",
            (uid,),
        ).fetchall()
    return JSONResponse({"medications": [dict(r) for r in rows]})


@router.get("/medications", response_class=HTMLResponse)
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


@router.get("/medications/new", response_class=HTMLResponse)
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


@router.post("/medications")
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


@router.post("/medications/delete")
def medications_delete(id: int = Form(...)):
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute("DELETE FROM medications WHERE id = ? AND user_id = ?", (id, uid))
        conn.commit()
    return RedirectResponse(url="/medications", status_code=303)


@router.get("/medications/{med_id}/edit", response_class=HTMLResponse)
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


@router.post("/medications/{med_id}/edit")
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
