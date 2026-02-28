import html
from datetime import datetime
from typing import Optional, Tuple
from urllib.parse import quote_plus

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from config import _current_user_id, _from_utc_storage, _now_local, FREQ_LABELS
from db import get_db
from routers.medications_utils import _MED_DATALIST, _adherence_7d, _adherence_badge
from ui import PAGE_STYLE, _nav_bar

router = APIRouter()

MAX_MED_NAME_LEN = 120
MAX_DOSE_LEN = 80
MAX_NOTES_LEN = 1000


def _entry_error_url(base_path: str, message: str) -> str:
    return f"{base_path}?error={quote_plus(message)}"


def _validate_medication_entry(
    name: str, dose: str, notes: str, med_date: str
) -> Tuple[Optional[str], Optional[datetime]]:
    if not name.strip():
        return ("Medication name is required", None)
    if len(name.strip()) > MAX_MED_NAME_LEN:
        return (f"Medication name must be {MAX_MED_NAME_LEN} characters or fewer", None)
    if len(dose.strip()) > MAX_DOSE_LEN:
        return (f"Dose must be {MAX_DOSE_LEN} characters or fewer", None)
    if len(notes.strip()) > MAX_NOTES_LEN:
        return (f"Notes must be {MAX_NOTES_LEN} characters or fewer", None)
    try:
        ts_dt = datetime.strptime(med_date, "%Y-%m-%dT%H:%M")
    except ValueError:
        return ("Invalid date format", None)
    now_ref = _now_local()
    if ts_dt > now_ref:
        return ("Date cannot be in the future", None)
    return (None, ts_dt)


@router.get("/api/medications")
def api_medications():
    uid = _current_user_id.get()
    with get_db() as conn:
        taken = conn.execute(
            "SELECT 'dose_taken_' || md.id AS id, md.id AS dose_id, ms.name, ms.dose,"
            " CASE WHEN ms.frequency='prn' THEN '' ELSE 'Scheduled dose taken' END AS notes,"
            " 'taken' AS status, md.taken_at AS timestamp"
            " FROM medication_doses md"
            " JOIN medication_schedules ms ON ms.id = md.schedule_id AND ms.user_id = md.user_id"
            " WHERE md.user_id=? AND md.status='taken' AND md.taken_at != ''"
            " ORDER BY md.taken_at ASC",
            (uid,),
        ).fetchall()
        missed = conn.execute(
            "SELECT 'dose_missed_' || md.id AS id, md.id AS dose_id, ms.name, ms.dose, 'Medication missed' AS notes, 'missed' AS status,"
            " md.scheduled_date || ' 00:00:00' AS timestamp"
            " FROM medication_doses md"
            " JOIN medication_schedules ms ON ms.id = md.schedule_id AND ms.user_id = md.user_id"
            " WHERE md.user_id=? AND md.status='missed'"
            " ORDER BY md.scheduled_date ASC",
            (uid,),
        ).fetchall()
    taken_local = []
    for r in taken:
        item = dict(r)
        item["timestamp"] = _from_utc_storage(item["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
        taken_local.append(item)
    result = sorted(taken_local + [dict(r) for r in missed], key=lambda r: r["timestamp"])
    return JSONResponse({"medications": result})


def _meds_subnav_log() -> str:
    """Inline copy of sub-nav (avoids circular import with medications_adherence)."""
    def lnk(href, label, active):
        s = (
            "font-weight:700;color:#7c3aed;border-bottom:2px solid #7c3aed;padding-bottom:2px;"
            if active else "color:#6b7280;"
        )
        return f'<a href="{href}" style="text-decoration:none;font-size:14px;{s}">{label}</a>'
    return (
        '<div style="display:flex;gap:20px;border-bottom:1px solid #e5e7eb;'
        'padding-bottom:12px;margin-bottom:20px;flex-wrap:wrap;">'
        + lnk("/medications/today",     "Today's Doses", False)
        + lnk("/medications/schedules", "Schedules",     False)
        + lnk("/medications",           "Log",           True)
        + "</div>"
    )


@router.get("/medications", response_class=HTMLResponse)
def medications_list():
    uid = _current_user_id.get()
    with get_db() as conn:
        taken_rows = conn.execute(
            "SELECT 'dose_taken_' || md.id AS id, ms.name, ms.dose, 'Taken' AS status,"
            " md.taken_at AS timestamp, '' AS notes"
            " FROM medication_doses md"
            " JOIN medication_schedules ms ON ms.id = md.schedule_id AND ms.user_id = md.user_id"
            " WHERE md.user_id = ? AND md.status='taken' AND md.taken_at != ''"
            " ORDER BY md.taken_at DESC",
            (uid,),
        ).fetchall()
        missed_rows = conn.execute(
            "SELECT 'dose_missed_' || md.id AS id, ms.name, ms.dose, 'Missed' AS status,"
            " md.scheduled_date || ' 00:00:00' AS timestamp, 'Scheduled dose missed' AS notes"
            " FROM medication_doses md"
            " JOIN medication_schedules ms ON ms.id = md.schedule_id AND ms.user_id = md.user_id"
            " WHERE md.user_id = ? AND md.status='missed'"
            " ORDER BY md.scheduled_date DESC",
            (uid,),
        ).fetchall()
        rows = sorted([dict(r) for r in taken_rows] + [dict(r) for r in missed_rows], key=lambda r: r["timestamp"], reverse=True)
        # Schedules summary for top section
        schedules = conn.execute(
            "SELECT id, name, dose, frequency, start_date, paused FROM medication_schedules"
            " WHERE user_id=? AND active=1 ORDER BY name",
            (uid,),
        ).fetchall()
        sched_cards = ""
        for s in schedules:
            is_paused = bool(s["paused"])
            if is_paused:
                badge = '<span style="font-size:12px;background:#f3f4f6;color:#6b7280;border-radius:10px;padding:2px 8px;font-weight:700;">Paused</span>'
            else:
                adh = _adherence_7d(conn, s["id"], uid, s["start_date"], s["frequency"])
                badge = _adherence_badge(adh)
            freq_label = FREQ_LABELS.get(s["frequency"], s["frequency"])
            row_style = (
                "display:flex;align-items:center;justify-content:space-between;"
                "flex-wrap:wrap;gap:6px;padding:10px 0;border-bottom:1px solid #f3f4f6;"
                + ("opacity:.5;" if is_paused else "")
            )
            sched_cards += (
                f'<div style="{row_style}">'
                f'<div>'
                f'<span style="font-weight:700;font-size:14px;">{html.escape(s["name"])}</span>'
                + (f' <span style="font-size:12px;color:#7c3aed;font-weight:600;">{html.escape(s["dose"])}</span>' if s["dose"] else "")
                + f'<span style="font-size:12px;color:#9ca3af;margin-left:6px;">{html.escape(freq_label)}</span>'
                f'</div>'
                f'<div>{badge}</div>'
                f'</div>'
            )

    schedules_section = ""
    if schedules:
        schedules_section = f"""
    <div class="card" style="margin-bottom:20px;padding:16px 18px;">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:2px;">
        <span style="font-size:15px;font-weight:700;color:#111;">Active Schedules</span>
        <a href="/medications/schedules" style="font-size:13px;color:#7c3aed;text-decoration:none;">Manage &rarr;</a>
      </div>
      {sched_cards}
    </div>"""

    groups: dict[str, list] = {}
    for row in rows:
        n = row["name"]
        if n not in groups:
            groups[n] = []
        groups[n].append(row)
    total_entries = len(rows)
    unique_meds = len(groups)
    last_logged = "—"
    if rows:
        if rows[0]["status"] == "Taken":
            last_logged = _from_utc_storage(rows[0]["timestamp"]).strftime("%b %-d, %Y %H:%M")
        else:
            last_logged = rows[0]["timestamp"]

    if groups:
        sections = ""
        for name, entries in groups.items():
            local_entries = []
            for e in entries:
                item = dict(e)
                if item["status"] == "Taken":
                    item["timestamp"] = _from_utc_storage(item["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
                local_entries.append(item)
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
              <div class="med-status med-status-{"taken" if e["status"] == "Taken" else "missed"}">{html.escape(e["status"])}</div>
              {"<p class='card-notes med-notes'>" + html.escape(e['notes']) + "</p>" if e['notes'] else ""}
            </div>
            """
                for e in local_entries
            )
            count = len(local_entries)
            label = "entry" if count == 1 else "entries"
            sections += f"""
        <details class="med-group">
          <summary class="med-group-header">
            <span class="med-group-name">{html.escape(name)}</span>
            <span class="med-count">{count} {label}</span>
          </summary>
          <div class="med-group-body">
            {cards}
          </div>
        </details>
        """
    else:
        sections = "<p class='empty'>No medications logged yet.</p>"

    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}
  <style>
    .med-shell {{ display:flex; flex-direction:column; gap:14px; }}
    .med-summary {{ border:1px solid #e5e7eb; border-radius:10px; background:#fff; padding:10px 12px;
                    display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:8px; }}
    .med-summary-item {{ border:1px solid #eef2f7; border-radius:8px; padding:8px 10px; background:#fcfcfd; }}
    .med-summary-label {{ font-size:11px; color:#6b7280; text-transform:uppercase; letter-spacing:.04em; font-weight:700; }}
    .med-summary-value {{ margin-top:2px; font-size:16px; color:#111827; font-weight:750; line-height:1.2; }}
    .med-controls {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
    .med-btn-primary {{ background:#7c3aed; color:#fff; text-decoration:none; border-radius:8px; padding:8px 14px; font-size:13px; font-weight:700; }}
    .med-btn-primary:hover {{ background:#6d28d9; }}
    .med-btn-secondary {{ border:1px solid #d1d5db; color:#374151; text-decoration:none; border-radius:8px; padding:8px 12px; font-size:13px; font-weight:600; background:#fff; }}
    .med-btn-secondary:hover {{ background:#f9fafb; }}
    .med-group {{ margin-bottom: 10px; border:1px solid #e5e7eb; border-radius:10px; background:#fff; }}
    .med-group-header {{ display:flex; align-items:center; justify-content:space-between; gap:10px;
                         padding:10px 12px; cursor:pointer; list-style:none; user-select:none; }}
    .med-group-header::-webkit-details-marker {{ display:none; }}
    .med-group-body {{ padding:0 10px 8px; border-top:1px solid #eef2f7; }}
    .med-group-name {{ font-size: 18px; font-weight: 750; color: #111827; line-height: 1.2; }}
    .med-count {{ font-size: 12px; color: #374151; background: #eef2ff; border: 1px solid #e0e7ff;
                  border-radius: 999px; padding: 2px 9px; font-weight:700; }}
    .med-card {{ border-color: #e5e7eb; padding: 14px; margin:8px 0; border-radius:10px; }}
    .med-badge {{ background: #7c3aed; font-size: 12px; width: 34px; height: 34px; }}
    .med-name {{ font-size: 16px; font-weight: 700; color: #111827; }}
    .med-dose {{ font-size: 13px; color: #5b21b6; margin-top: 2px; font-weight: 600; line-height: 1.35; }}
    .med-ts {{ font-size: 12px; color: #6b7280; margin-top: 4px; }}
    .med-notes {{ font-size: 13px; color: #1f2937; line-height: 1.5; margin:10px 0 0; }}
    .med-status {{ margin-top:8px; display:inline-flex; align-items:center; border-radius:999px; padding:2px 8px; font-size:11px; font-weight:700; }}
    .med-status-taken {{ background:#dcfce7; color:#166534; border:1px solid #86efac; }}
    .med-status-missed {{ background:#fee2e2; color:#991b1b; border:1px solid #fecaca; }}
    .sched-summary {{ margin:0; padding:12px 14px; border-radius:10px; }}
    @media (max-width: 640px) {{
      .med-summary {{ grid-template-columns:1fr; }}
      .med-controls a {{ flex:1; text-align:center; }}
      .med-card {{ padding: 12px; }}
      .med-actions {{ width:100%; }}
      .med-actions .btn-edit, .med-actions .btn-delete {{ flex:1; text-align:center; }}
    }}
  </style>
</head>
<body>
  {_nav_bar('meds')}
  <div class="container">
    <h1>Medications</h1>
    {_meds_subnav_log()}
    <div class="med-shell">
      <div class="med-summary">
        <div class="med-summary-item">
          <div class="med-summary-label">Logged Entries</div>
          <div class="med-summary-value">{total_entries}</div>
        </div>
        <div class="med-summary-item">
          <div class="med-summary-label">Medications</div>
          <div class="med-summary-value">{unique_meds}</div>
        </div>
        <div class="med-summary-item">
          <div class="med-summary-label">Last Logged</div>
          <div class="med-summary-value" style="font-size:13px;font-weight:700;">{html.escape(last_logged)}</div>
        </div>
      </div>
      <div class="med-controls">
        <a href="/medications/today" class="med-btn-primary">Open Today's Doses</a>
        <a href="/medications/schedules" class="med-btn-secondary">Manage schedules</a>
      </div>
      {schedules_section.replace('class="card"', 'class="card sched-summary"') if schedules_section else ""}
      {sections}
    </div>
  </div>
</body>
</html>
"""


@router.get("/medications/new", response_class=HTMLResponse)
def medications_new(error: str = ""):
    return RedirectResponse(
        url="/medications/schedules",
        status_code=303,
    )


@router.post("/medications")
def medications_create(
    name: str = Form(...),
    dose: str = Form(""),
    notes: str = Form(""),
    med_date: str = Form(...),
):
    return RedirectResponse(
        url="/medications/schedules",
        status_code=303,
    )


@router.post("/medications/delete")
def medications_delete(id: int = Form(...)):
    return RedirectResponse(
        url="/medications",
        status_code=303,
    )


@router.get("/medications/{med_id}/edit", response_class=HTMLResponse)
def medications_edit_get(med_id: int, error: str = ""):
    return RedirectResponse(
        url="/medications",
        status_code=303,
    )


@router.post("/medications/{med_id}/edit")
def medications_edit_post(
    med_id: int,
    name: str = Form(...),
    dose: str = Form(""),
    notes: str = Form(""),
    med_date: str = Form(...),
):
    return RedirectResponse(
        url="/medications",
        status_code=303,
    )
