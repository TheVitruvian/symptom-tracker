import html
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from config import _current_user_id
from db import get_db
from ui import PAGE_STYLE, _nav_bar

router = APIRouter()

FREQ_LABELS = {
    "once_daily":  "Once daily",
    "twice_daily": "Twice daily",
    "three_daily": "Three times daily",
    "prn":         "As needed (PRN)",
}
VALID_FREQUENCIES = set(FREQ_LABELS)


def _doses_per_day(frequency: str) -> int:
    return {"once_daily": 1, "twice_daily": 2, "three_daily": 3, "prn": 0}[frequency]


def _safe_meds_redirect(redirect_to: str) -> str:
    val = (redirect_to or "").strip()
    if not val.startswith("/medications"):
        return "/medications/today"
    return val


def _parse_valid_scheduled_date(scheduled_date: str):
    try:
        d = date.fromisoformat((scheduled_date or "").strip())
    except ValueError:
        return None
    if d > date.today():
        return None
    return d


def _parse_created_at(created_at: str):
    try:
        return datetime.strptime((created_at or "").strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _dose_label(dose_num: int, total: int) -> str:
    if total == 1:
        return "Daily dose"
    if total == 2:
        return ["Morning dose", "Evening dose"][dose_num - 1]
    return ["Morning dose", "Afternoon dose", "Evening dose"][dose_num - 1]


def _adherence_7d(conn, schedule_id: int, user_id: int, start_date_str: str, frequency: str) -> dict:
    dpd = _doses_per_day(frequency)
    if dpd == 0:
        taken = conn.execute(
            "SELECT COUNT(*) FROM medication_doses WHERE schedule_id=? AND user_id=? AND status='taken'"
            " AND scheduled_date >= ?",
            (schedule_id, user_id, (date.today() - timedelta(days=6)).isoformat()),
        ).fetchone()[0]
        return {"expected": None, "taken": taken, "pct": None}
    window_start = max(date.today() - timedelta(days=6), date.fromisoformat(start_date_str))
    window_end = date.today()
    if window_start > window_end:
        return {"expected": 0, "taken": 0, "pct": None}
    days_in_window = (window_end - window_start).days + 1
    expected = days_in_window * dpd
    taken = conn.execute(
        "SELECT COUNT(*) FROM medication_doses WHERE schedule_id=? AND user_id=? AND status='taken'"
        " AND scheduled_date >= ? AND scheduled_date <= ?",
        (schedule_id, user_id, window_start.isoformat(), window_end.isoformat()),
    ).fetchone()[0]
    pct = round(taken / expected * 100, 1) if expected > 0 else None
    return {"expected": expected, "taken": taken, "pct": pct}


def _adherence_badge(adh: dict) -> str:
    if adh["expected"] is None:
        n = adh["taken"]
        word = "dose" if n == 1 else "doses"
        return (
            f'<span style="font-size:12px;background:#ede9fe;color:#7c3aed;border-radius:10px;'
            f'padding:2px 8px;font-weight:700;">{n} {word} this week</span>'
        )
    if adh["expected"] == 0:
        return '<span style="font-size:12px;color:#9ca3af;">No data yet</span>'
    pct = adh["pct"] if adh["pct"] is not None else 0.0
    if pct >= 80:
        bg, fg = "#dcfce7", "#15803d"
    elif pct >= 50:
        bg, fg = "#fef9c3", "#92400e"
    else:
        bg, fg = "#fee2e2", "#b91c1c"
    return (
        f'<span style="font-size:12px;background:{bg};color:{fg};border-radius:10px;'
        f'padding:2px 8px;font-weight:700;">{pct}% adherence (7d)</span>'
    )


def _meds_subnav(active_key: str) -> str:
    def lnk(href, label, key):
        s = (
            "font-weight:700;color:#7c3aed;border-bottom:2px solid #7c3aed;padding-bottom:2px;"
            if active_key == key
            else "color:#6b7280;"
        )
        return f'<a href="{href}" style="text-decoration:none;font-size:14px;{s}">{label}</a>'
    return (
        '<div style="display:flex;gap:20px;border-bottom:1px solid #e5e7eb;'
        'padding-bottom:12px;margin-bottom:20px;flex-wrap:wrap;">'
        + lnk("/medications/today",     "Today's Doses", "today")
        + lnk("/medications/schedules", "Schedules",     "schedules")
        + lnk("/medications",           "Log",           "log")
        + "</div>"
    )


# ── Today's check-off ────────────────────────────────────────────────────────

@router.get("/medications/today", response_class=HTMLResponse)
def medications_today(d: str = "", w_end: str = ""):
    uid = _current_user_id.get()
    with get_db() as conn:
        schedules = conn.execute(
            "SELECT id, name, dose, frequency, created_at FROM medication_schedules"
            " WHERE user_id=? AND active=1 ORDER BY name",
            (uid,),
        ).fetchall()

    if not schedules:
        return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}<title>Today's Doses</title></head>
<body>
  {_nav_bar('meds')}
  <div class="container">
    <h1>Today's Doses</h1>
    {_meds_subnav('today')}
    <p class="empty">No schedules yet. <a href="/medications/schedules/new" style="color:#7c3aed;">Add a schedule</a> to start tracking adherence.</p>
  </div>
</body>
</html>"""

    today = date.today()
    try:
        window_end = date.fromisoformat(w_end) if w_end else today
    except ValueError:
        window_end = today
    if window_end > today:
        window_end = today
    window_start = window_end - timedelta(days=6)

    try:
        selected_day = date.fromisoformat(d) if d else window_end
    except ValueError:
        selected_day = window_end
    if selected_day > today:
        selected_day = today
    if selected_day < window_start or selected_day > window_end:
        selected_day = window_end
    day_str = selected_day.isoformat()
    redirect_to = f"/medications/today?d={day_str}&w_end={window_end.isoformat()}"

    with get_db() as conn:
        dose_rows = conn.execute(
            "SELECT schedule_id, dose_num, status, taken_at FROM medication_doses"
            " WHERE user_id=? AND scheduled_date=?",
            (uid, day_str),
        ).fetchall()
    dose_map = {(r["schedule_id"], r["dose_num"]): r for r in dose_rows}

    day_tabs = ""
    for day_offset in range(6, -1, -1):
        day = window_end - timedelta(days=day_offset)
        is_active = " day-tab-active" if day == selected_day else ""
        tab_label = "Today" if day == today else ("Yesterday" if day == today - timedelta(days=1) else day.strftime("%a"))
        tab_sub = day.strftime("%b %-d")
        day_tabs += (
            f'<a class="day-tab{is_active}" href="/medications/today?d={day.isoformat()}&w_end={window_end.isoformat()}">'
            f'<span>{tab_label}</span><small>{tab_sub}</small></a>'
        )
    prev_week_end = (window_start - timedelta(days=1)).isoformat()
    prev_week_href = f"/medications/today?d={prev_week_end}&w_end={prev_week_end}"
    today_href = f"/medications/today?d={today.isoformat()}&w_end={today.isoformat()}"
    next_week_html = ""
    if window_end < today:
        next_window_end = min(today, window_end + timedelta(days=7))
        next_end_str = next_window_end.isoformat()
        next_week_href = f"/medications/today?d={next_end_str}&w_end={next_end_str}"
        next_week_html = f'<a class="week-nav-btn" href="{next_week_href}">Next week &rarr;</a>'

    slot_rows = {"Daily": "", "Morning": "", "Afternoon": "", "Evening": ""}
    prn_rows = ""
    scheduled_expected = 0
    scheduled_taken = 0
    scheduled_missed = 0
    scheduled_pending = 0
    prn_logs = 0

    for sched in schedules:
        sid = sched["id"]
        sname = html.escape(sched["name"])
        sdose = html.escape(sched["dose"])
        freq = sched["frequency"]
        dpd = _doses_per_day(freq)
        dose_str = f'<span class="med-dose">{sdose}</span>' if sched["dose"] else ""
        created_at = _parse_created_at(sched["created_at"]) if "created_at" in sched.keys() else None
        if created_at and selected_day < created_at.date():
            continue

        if dpd == 0:
            prn_taken = [r for r in dose_rows if r["schedule_id"] == sid and r["status"] == "taken"]
            prn_count = len(prn_taken)
            prn_logs += prn_count
            prn_word = "dose" if prn_count == 1 else "doses"
            prn_undo_html = ""
            if prn_count > 0:
                prn_undo_html = f"""
                  <form method="post" action="/medications/doses/undo" class="dose-action-form" style="margin:0;">
                    <input type="hidden" name="schedule_id" value="{sid}">
                    <input type="hidden" name="scheduled_date" value="{day_str}">
                    <input type="hidden" name="dose_num" value="{prn_count}">
                    <input type="hidden" name="redirect_to" value="{redirect_to}">
                    <button type="submit" class="dose-btn dose-btn-secondary dose-btn-x" aria-label="Undo last PRN entry" title="Undo">
                      <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M6 6L18 18M18 6L6 18"/></svg>
                    </button>
                  </form>"""
            prn_rows += f"""
            <article class="med-row">
              <div class="med-main">
                <div class="med-title">{sname}{dose_str}</div>
                <div class="med-meta">PRN</div>
                <div class="dose-chip dose-chip-neutral">Logged: {prn_count} {prn_word}</div>
              </div>
              <div class="dose-actions">
                <form method="post" action="/medications/doses/take" class="dose-action-form" style="margin:0;">
                  <input type="hidden" name="schedule_id" value="{sid}">
                  <input type="hidden" name="scheduled_date" value="{day_str}">
                  <input type="hidden" name="dose_num" value="{prn_count + 1}">
                  <input type="hidden" name="redirect_to" value="{redirect_to}">
                  <button type="submit" class="dose-btn dose-btn-primary">+ Log now</button>
                </form>
                <details class="dose-more">
                  <summary>More options</summary>
                  <div class="dose-more-actions">
                    <form method="post" action="/medications/doses/take" class="dose-time-form dose-action-form">
                      <input type="hidden" name="schedule_id" value="{sid}">
                      <input type="hidden" name="scheduled_date" value="{day_str}">
                      <input type="hidden" name="dose_num" value="{prn_count + 1}">
                      <input type="hidden" name="redirect_to" value="{redirect_to}">
                      <input type="time" name="taken_time" data-date="{day_str}" class="dose-time dose-time-input">
                      <button type="submit" class="dose-btn dose-btn-secondary">Log with time</button>
                    </form>
                    {prn_undo_html}
                  </div>
                </details>
              </div>
            </article>"""
            continue

        for dn in range(1, dpd + 1):
            scheduled_expected += 1
            dlabel = _dose_label(dn, dpd)
            if dlabel.startswith("Morning"):
                slot_name = "Morning"
            elif dlabel.startswith("Afternoon"):
                slot_name = "Afternoon"
            elif dlabel.startswith("Evening"):
                slot_name = "Evening"
            else:
                slot_name = "Daily"

            record = dose_map.get((sid, dn))
            if record:
                if record["status"] == "taken":
                    scheduled_taken += 1
                    t = record["taken_at"][11:16] if record["taken_at"] else ""
                    status_and_actions = (
                        f'<div class="status-row">'
                        f'<div class="dose-chip dose-chip-taken">&#10003; Taken{f" at {t}" if t else ""}</div>'
                        f'<form method="post" action="/medications/doses/undo" class="dose-action-form" style="margin:0;">'
                        f'<input type="hidden" name="schedule_id" value="{sid}">'
                        f'<input type="hidden" name="scheduled_date" value="{day_str}">'
                        f'<input type="hidden" name="dose_num" value="{dn}">'
                        f'<input type="hidden" name="redirect_to" value="{redirect_to}">'
                        f'<button type="submit" class="dose-btn dose-btn-x" aria-label="Undo dose" title="Undo"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M6 6L18 18M18 6L6 18"/></svg></button>'
                        f'</form>'
                        f'</div>'
                    )
                else:
                    scheduled_missed += 1
                    status_and_actions = (
                        f'<div class="status-row">'
                        f'<div class="dose-chip dose-chip-missed">&#10007; Missed</div>'
                        f'<form method="post" action="/medications/doses/undo" class="dose-action-form" style="margin:0;">'
                        f'<input type="hidden" name="schedule_id" value="{sid}">'
                        f'<input type="hidden" name="scheduled_date" value="{day_str}">'
                        f'<input type="hidden" name="dose_num" value="{dn}">'
                        f'<input type="hidden" name="redirect_to" value="{redirect_to}">'
                        f'<button type="submit" class="dose-btn dose-btn-x" aria-label="Undo dose" title="Undo"><svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M6 6L18 18M18 6L6 18"/></svg></button>'
                        f'</form>'
                        f'</div>'
                    )
            else:
                scheduled_pending += 1
                status_and_actions = (
                    f'<div class="primary-actions-row">'
                    f'<form method="post" action="/medications/doses/take" class="dose-action-form" style="margin:0;">'
                    f'<input type="hidden" name="schedule_id" value="{sid}">'
                    f'<input type="hidden" name="scheduled_date" value="{day_str}">'
                    f'<input type="hidden" name="dose_num" value="{dn}">'
                    f'<input type="hidden" name="redirect_to" value="{redirect_to}">'
                    f'<button type="submit" class="dose-btn dose-btn-muted">Take now</button>'
                    f'</form>'
                    f'<form method="post" action="/medications/doses/miss" class="dose-action-form" style="margin:0;">'
                    f'<input type="hidden" name="schedule_id" value="{sid}">'
                    f'<input type="hidden" name="scheduled_date" value="{day_str}">'
                    f'<input type="hidden" name="dose_num" value="{dn}">'
                    f'<input type="hidden" name="redirect_to" value="{redirect_to}">'
                    f'<button type="submit" class="dose-btn dose-btn-warn">Mark missed</button>'
                    f'</form>'
                    f'</div>'
                    f'<details class="dose-more">'
                    f'<summary>More options</summary>'
                    f'<div class="dose-more-actions">'
                    f'<form method="post" action="/medications/doses/take" class="dose-time-form dose-action-form">'
                    f'<input type="hidden" name="schedule_id" value="{sid}">'
                    f'<input type="hidden" name="scheduled_date" value="{day_str}">'
                    f'<input type="hidden" name="dose_num" value="{dn}">'
                    f'<input type="hidden" name="redirect_to" value="{redirect_to}">'
                    f'<input type="time" name="taken_time" data-date="{day_str}" class="dose-time dose-time-input">'
                    f'<button type="submit" class="dose-btn dose-btn-secondary">Take with time</button>'
                    f'</form>'
                    f'</div>'
                    f'</details>'
                )

            slot_rows[slot_name] += f"""
            <article class="med-row">
              <div class="med-main">
                <div class="med-title">{sname}{dose_str}</div>
                <div class="med-meta">{dlabel}</div>
              </div>
              <div class="dose-actions">
                {status_and_actions}
              </div>
            </article>"""

    slot_sections = ""
    for slot_name in ["Daily", "Morning", "Afternoon", "Evening"]:
        if slot_rows[slot_name]:
            slot_sections += f"""
          <section class="timeline-slot">
            <h3>{slot_name}</h3>
            {slot_rows[slot_name]}
          </section>"""

    if not slot_sections:
        slot_sections = '<p class="empty">No scheduled doses for this day.</p>'
    if not prn_rows:
        prn_rows = '<p class="empty">No PRN medications configured.</p>'

    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}<title>Today's Doses</title>
  <style>
    .today-shell {{ width:min(1320px, calc(100vw - 380px)); }}
    .today-header {{ display:flex; align-items:flex-end; justify-content:flex-start; gap:10px; flex-wrap:wrap; margin-bottom:14px; }}
    .today-label {{ margin:0; font-size:14px; color:#6b7280; font-weight:600; }}
    .today-jump-link {{ text-decoration:none; color:#6d28d9; font-size:12px; font-weight:700; white-space:nowrap; }}
    .today-jump-link:hover {{ text-decoration:underline; color:#5b21b6; }}
    .day-tabs-row {{ display:flex; align-items:center; gap:10px; margin-bottom:16px; }}
    .week-nav-btn {{ text-decoration:none; border:1px solid #d1d5db; color:#374151; background:#fff; border-radius:10px; padding:8px 10px; font-size:12px; font-weight:700; white-space:nowrap; }}
    .week-nav-btn:hover {{ background:#f9fafb; }}
    .day-tabs {{ display:flex; gap:8px; overflow:auto; padding-bottom:2px; margin-bottom:0; }}
    .day-tab {{ min-width:82px; text-decoration:none; border:1px solid #e5e7eb; background:#fff; border-radius:10px; padding:8px 10px; color:#374151; display:flex; flex-direction:column; gap:2px; }}
    .day-tab span {{ font-size:13px; font-weight:700; line-height:1.2; }}
    .day-tab small {{ font-size:11px; color:#9ca3af; }}
    .day-tab-active {{ border-color:#7c3aed; background:#faf5ff; }}
    .day-tab-active span, .day-tab-active small {{ color:#6d28d9; }}
    .kpi-grid {{ display:grid; grid-template-columns:repeat(4, minmax(120px, 1fr)); gap:10px; margin-bottom:16px; }}
    .kpi-card {{ border:1px solid #e5e7eb; border-radius:10px; background:#fff; padding:10px; }}
    .kpi-label {{ font-size:11px; color:#6b7280; text-transform:uppercase; letter-spacing:0.05em; font-weight:700; }}
    .kpi-value {{ font-size:22px; font-weight:800; color:#111827; margin-top:2px; line-height:1; }}
    .sections-grid {{ display:grid; grid-template-columns:minmax(0, 1.8fr) minmax(0, 1fr); gap:14px; }}
    .panel {{ border:1px solid #e5e7eb; border-radius:12px; background:#fff; padding:14px; }}
    .panel h2 {{ margin:0 0 12px; font-size:16px; color:#111827; }}
    .timeline-slot + .timeline-slot {{ margin-top:12px; }}
    .timeline-slot h3 {{ margin:0 0 8px; font-size:12px; text-transform:uppercase; letter-spacing:0.06em; color:#6b7280; }}
    .med-row {{ border:1px solid #eef2f7; border-radius:10px; padding:10px; background:#fcfcfd; display:flex; gap:10px; align-items:flex-start; justify-content:space-between; margin-bottom:8px; }}
    .med-main {{ min-width:0; }}
    .med-title {{ font-size:14px; font-weight:700; color:#111827; line-height:1.3; word-break:break-word; }}
    .med-dose {{ font-size:12px; color:#7c3aed; margin-left:6px; font-weight:700; }}
    .med-meta {{ font-size:12px; color:#6b7280; margin-top:2px; }}
    .dose-actions {{ display:flex; flex-direction:column; gap:6px; min-width:132px; }}
    .dose-chip {{ font-size:11px; font-weight:700; border-radius:999px; padding:3px 8px; line-height:1.2; width:fit-content; }}
    .dose-chip-taken {{ background:#dcfce7; color:#166534; border:1px solid #86efac; }}
    .dose-chip-missed {{ background:#fee2e2; color:#991b1b; border:1px solid #fecaca; }}
    .dose-chip-pending {{ background:#eff6ff; color:#1d4ed8; border:1px solid #bfdbfe; }}
    .dose-chip-neutral {{ background:#f3f4f6; color:#374151; border:1px solid #e5e7eb; }}
    .dose-btn {{ min-height:30px; border-radius:7px; padding:6px 9px; font-size:12px; cursor:pointer; font-family:inherit; font-weight:600; width:100%; }}
    .dose-btn-primary {{ background:#15803d; color:#fff; border:none; }}
    .dose-btn-primary:hover {{ background:#166534; }}
    .dose-btn-secondary {{ background:#fff; color:#374151; border:1px solid #d1d5db; }}
    .dose-btn-secondary:hover {{ background:#f9fafb; }}
    .dose-btn-muted {{ background:#f0fdf4; color:#166534; border:1px solid #bbf7d0; }}
    .dose-btn-muted:hover {{ background:#dcfce7; }}
    .dose-btn-warn {{ background:#fef2f2; color:#991b1b; border:1px solid #fecaca; }}
    .dose-btn-warn:hover {{ background:#fee2e2; }}
    .dose-btn-ghost {{ background:#fff; color:#b91c1c; border:1px solid #fca5a5; }}
    .dose-btn-ghost:hover {{ background:#fff1f2; }}
    .primary-actions-row {{ display:flex; gap:6px; width:100%; }}
    .primary-actions-row form {{ flex:1; }}
    .primary-actions-row .dose-btn {{ white-space:nowrap; font-size:11px; padding:6px 7px; }}
    .status-row {{ display:flex; align-items:center; gap:6px; }}
    .status-row .dose-chip {{ min-height:22px; display:inline-flex; align-items:center; padding:0 8px; }}
    .dose-btn-x {{ width:22px; min-width:22px; min-height:22px; padding:0; border:1px solid #ef4444; background:#ef4444; border-radius:999px; display:inline-flex; align-items:center; justify-content:center; }}
    .dose-btn-x:hover {{ background:#dc2626; border-color:#dc2626; }}
    .dose-btn-x svg {{ width:10px; height:10px; stroke:#ffffff; stroke-width:2.5; fill:none; stroke-linecap:round; }}
    .dose-more {{ width:100%; }}
    .dose-more summary {{ cursor:pointer; font-size:11px; color:#6b7280; user-select:none; }}
    .dose-more-actions {{ margin-top:6px; display:flex; flex-direction:column; gap:6px; }}
    .dose-time-form {{ display:flex; gap:6px; align-items:center; margin:0; flex-wrap:wrap; }}
    .dose-time-input {{ border:1px solid #d1d5db; border-radius:7px; padding:6px 8px; font-size:12px; font-family:inherit; min-height:30px; width:98px; }}
    @media (max-width: 1080px) {{
      .sections-grid {{ grid-template-columns:1fr; }}
    }}
    @media (max-width: 900px) {{
      .today-shell {{ width:100%; }}
      .kpi-grid {{ grid-template-columns:repeat(2, minmax(120px, 1fr)); }}
    }}
    @media (max-width: 640px) {{
      .med-row {{ flex-direction:column; }}
      .dose-actions {{ width:100%; min-width:0; }}
      .dose-time-input {{ width:100%; }}
    }}
  </style>
</head>
<body>
  {_nav_bar('meds')}
  <div class="container">
    <h1>Today's Doses</h1>
    {_meds_subnav('today')}
    <div class="today-shell">
      <div class="today-header">
        <p class="today-label">{selected_day.strftime("%A, %B %-d, %Y")}</p>
        <a class="today-jump-link" href="{today_href}">Today</a>
      </div>
      <div class="day-tabs-row">
        <a class="week-nav-btn" href="{prev_week_href}">&larr; Previous week</a>
        <div class="day-tabs">{day_tabs}</div>
        {next_week_html}
      </div>
      <div class="kpi-grid">
        <div class="kpi-card"><div class="kpi-label">Scheduled</div><div class="kpi-value">{scheduled_expected}</div></div>
        <div class="kpi-card"><div class="kpi-label">Taken</div><div class="kpi-value">{scheduled_taken}</div></div>
        <div class="kpi-card"><div class="kpi-label">Pending</div><div class="kpi-value">{scheduled_pending}</div></div>
        <div class="kpi-card"><div class="kpi-label">PRN Logs</div><div class="kpi-value">{prn_logs}</div></div>
      </div>
      <div class="sections-grid">
        <section class="panel">
          <h2>Scheduled Doses</h2>
          {slot_sections}
        </section>
        <section class="panel">
          <h2>PRN Medications</h2>
          {prn_rows}
        </section>
      </div>
    </div>
  </div>
  <script>
    (function () {{
      function clientLocalDateISO() {{
        const now = new Date();
        const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
        return local.toISOString().slice(0, 10);
      }}

      function initDoseTimeInputs(root) {{
        const now = new Date();
        const hh = String(now.getHours()).padStart(2, "0");
        const mm = String(now.getMinutes()).padStart(2, "0");
        const nowTime = `${{hh}}:${{mm}}`;
        const yyyy = now.getFullYear();
        const mo = String(now.getMonth() + 1).padStart(2, "0");
        const dd = String(now.getDate()).padStart(2, "0");
        const today = `${{yyyy}}-${{mo}}-${{dd}}`;
        root.querySelectorAll("input.dose-time[data-date]").forEach((el) => {{
          const d = el.getAttribute("data-date") || "";
          if (!el.value) el.value = nowTime;
          if (d === today) el.max = nowTime;
        }});
      }}

      async function refreshTodayShell(url, pushState) {{
        const targetUrl = url || (window.location.pathname + window.location.search);
        const res = await fetch(targetUrl, {{
          headers: {{ "X-Requested-With": "fetch" }},
          cache: "no-store",
        }});
        if (!res.ok) throw new Error("refresh failed");
        const htmlText = await res.text();
        const parser = new DOMParser();
        const doc = parser.parseFromString(htmlText, "text/html");
        const nextShell = doc.querySelector(".today-shell");
        const currentShell = document.querySelector(".today-shell");
        if (!nextShell || !currentShell) throw new Error("shell missing");
        currentShell.replaceWith(nextShell);
        if (pushState) window.history.pushState({{}}, "", targetUrl);
        initDoseTimeInputs(document);
      }}

      async function ensureClientDateDefaults() {{
        const u = new URL(window.location.href);
        const hasD = !!u.searchParams.get("d");
        const hasWEnd = !!u.searchParams.get("w_end");
        if (hasD && hasWEnd) return;
        const clientDate = clientLocalDateISO();
        u.searchParams.set("d", clientDate);
        u.searchParams.set("w_end", clientDate);
        window.history.replaceState({{}}, "", u.pathname + u.search);
        await refreshTodayShell(u.pathname + u.search, false);
      }}

      function setDayTabActive(href) {{
        const target = new URL(href, window.location.origin);
        const targetDay = target.searchParams.get("d");
        document.querySelectorAll(".day-tab").forEach((a) => {{
          const day = new URL(a.href, window.location.origin).searchParams.get("d");
          if (day && targetDay && day === targetDay) a.classList.add("day-tab-active");
          else a.classList.remove("day-tab-active");
        }});
      }}

      document.addEventListener("submit", async (e) => {{
        const form = e.target;
        if (!(form instanceof HTMLFormElement)) return;
        if (!form.classList.contains("dose-action-form")) return;
        e.preventDefault();
        const submitBtn = form.querySelector('button[type="submit"]');
        if (submitBtn) submitBtn.disabled = true;
        try {{
          const now = new Date();
          const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
          const fd = new FormData(form);
          fd.set("client_now", local.toISOString().slice(0, 16));
          const res = await fetch(form.action, {{
            method: "POST",
            body: fd,
            headers: {{ "X-Requested-With": "fetch" }},
          }});
          if (!res.ok) throw new Error("action failed");
          await refreshTodayShell("", false);
        }} catch (_) {{
          window.location.href = window.location.pathname + window.location.search;
        }} finally {{
          if (submitBtn) submitBtn.disabled = false;
        }}
      }});

      document.addEventListener("click", async (e) => {{
        const link = e.target.closest("a.day-tab, a.week-nav-btn, a.today-jump-link");
        if (!link) return;
        e.preventDefault();
        const href = link.getAttribute("href") || "";
        if (!href) return;
        if (link.classList.contains("day-tab")) setDayTabActive(href);
        try {{
          await refreshTodayShell(href, true);
        }} catch (_) {{
          window.location.href = href;
        }}
      }});

      window.addEventListener("popstate", async () => {{
        try {{
          await refreshTodayShell("", false);
        }} catch (_) {{
          window.location.reload();
        }}
      }});

      initDoseTimeInputs(document);
      ensureClientDateDefaults().catch(() => {{}});
    }})();
  </script>
</body>
</html>"""


# ── Schedule CRUD ─────────────────────────────────────────────────────────────

@router.get("/medications/schedules", response_class=HTMLResponse)
def schedules_list():
    from routers.medications import _MED_DATALIST
    today_str = date.today().isoformat()
    freq_options = "".join(
        f'<option value="{k}">{html.escape(v)}</option>' for k, v in FREQ_LABELS.items()
    )
    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}<title>Medication Schedules</title>
  <style>
    .sched-shell {{ width:min(1120px, calc(100vw - 380px)); }}
    .sched-top {{ display:flex; align-items:center; justify-content:space-between; gap:10px; flex-wrap:wrap; margin-bottom:10px; }}
    .sched-inline-form {{ margin-bottom:16px; }}
    .sched-inline-form .card {{ margin:0; }}
    .sched-grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); gap:12px; }}
    .sched-card {{ border:1px solid #e5e7eb; border-radius:12px; background:#fff; padding:14px; }}
    .sched-name {{ font-size:16px; font-weight:700; color:#111827; }}
    .sched-dose {{ font-size:12px; color:#7c3aed; margin-top:2px; font-weight:700; }}
    .sched-meta {{ font-size:12px; color:#6b7280; margin-top:3px; }}
    .sched-actions {{ display:flex; gap:8px; margin-top:10px; }}
    .sched-empty {{ color:#6b7280; font-size:14px; padding:8px 2px; }}
    .sched-error {{ color:#b91c1c; font-size:13px; margin-bottom:8px; }}
    .sched-badge {{ font-size:12px; border-radius:999px; padding:3px 8px; font-weight:700; display:inline-block; margin-top:7px; }}
    .sched-badge-good {{ background:#dcfce7; color:#15803d; }}
    .sched-badge-mid {{ background:#fef9c3; color:#92400e; }}
    .sched-badge-low {{ background:#fee2e2; color:#b91c1c; }}
    .sched-badge-prn {{ background:#ede9fe; color:#7c3aed; }}
    @media (max-width: 900px) {{
      .sched-shell {{ width:100%; }}
    }}
  </style>
</head>
<body>
  {_nav_bar('meds')}
  <div class="container">
    <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:10px;margin-bottom:4px;">
      <h1 style="margin:0;">Schedules</h1>
      <button id="sched-add-btn" class="btn-primary" type="button" style="background:#7c3aed;font-size:14px;padding:7px 16px;">+ Add Schedule</button>
    </div>
    {_meds_subnav('schedules')}
    <div class="sched-shell">
      <section id="sched-inline-form" class="sched-inline-form" style="display:none;">
        <div class="card">
          <div id="sched-error" class="sched-error" style="display:none;"></div>
          <form id="sched-form">
            <input type="hidden" id="sched-id" value="">
            <div class="form-group">
              <label for="sched-name">Medication name <span style="color:#ef4444">*</span></label>
              <input type="text" id="sched-name" name="name" required list="med-suggestions" autocomplete="off">
              <datalist id="med-suggestions">{_MED_DATALIST}</datalist>
            </div>
            <div class="form-group">
              <label for="sched-dose">Dose <span style="color:#aaa;font-weight:400">(optional)</span></label>
              <input type="text" id="sched-dose" name="dose" placeholder="e.g. 500mg">
            </div>
            <div class="form-group">
              <label for="sched-frequency">Frequency <span style="color:#ef4444">*</span></label>
              <select id="sched-frequency" name="frequency"
                style="width:auto;border:1px solid #d1d5db;border-radius:6px;padding:8px 10px;font-size:15px;font-family:inherit;">
                {freq_options}
              </select>
            </div>
            <div class="form-group">
              <label for="sched-start-date">Start date <span style="color:#ef4444">*</span></label>
              <input type="date" id="sched-start-date" name="start_date" value="{today_str}" max="{today_str}" required style="width:auto;">
            </div>
            <div class="form-group">
              <label for="sched-notes">Notes <span style="color:#aaa;font-weight:400">(optional)</span></label>
              <textarea id="sched-notes" name="notes" rows="2" placeholder="Any additional details..."></textarea>
            </div>
            <div style="display:flex;gap:10px;align-items:center;">
              <button class="btn-primary" id="sched-save-btn" style="background:#7c3aed;" type="submit">Save Schedule</button>
              <button type="button" id="sched-cancel-btn" class="back" style="border:none;background:none;cursor:pointer;">Cancel</button>
            </div>
          </form>
        </div>
      </section>
      <div id="sched-list" class="sched-grid"></div>
      <p id="sched-empty" class="sched-empty" style="display:none;">No active schedules. Add one to start tracking adherence.</p>
    </div>
  </div>
  <script>
    (function () {{
      const addBtn = document.getElementById("sched-add-btn");
      const formWrap = document.getElementById("sched-inline-form");
      const formEl = document.getElementById("sched-form");
      const cancelBtn = document.getElementById("sched-cancel-btn");
      const listEl = document.getElementById("sched-list");
      const emptyEl = document.getElementById("sched-empty");
      const errorEl = document.getElementById("sched-error");
      const idEl = document.getElementById("sched-id");
      const nameEl = document.getElementById("sched-name");
      const doseEl = document.getElementById("sched-dose");
      const freqEl = document.getElementById("sched-frequency");
      const startDateEl = document.getElementById("sched-start-date");
      const notesEl = document.getElementById("sched-notes");
      const saveBtn = document.getElementById("sched-save-btn");

      function showForm(editing) {{
        formWrap.style.display = "";
        saveBtn.textContent = editing ? "Save Changes" : "Save Schedule";
        errorEl.style.display = "none";
        errorEl.textContent = "";
      }}

      function resetForm() {{
        idEl.value = "";
        formEl.reset();
        startDateEl.value = "{today_str}";
      }}

      function hideForm() {{
        formWrap.style.display = "none";
        resetForm();
      }}

      function badgeClass(adh) {{
        if (adh.expected === null) return "sched-badge sched-badge-prn";
        if (adh.expected === 0 || adh.pct === null) return "sched-badge sched-badge-mid";
        if (adh.pct >= 80) return "sched-badge sched-badge-good";
        if (adh.pct >= 50) return "sched-badge sched-badge-mid";
        return "sched-badge sched-badge-low";
      }}

      function badgeText(adh) {{
        if (adh.expected === null) {{
          const n = adh.taken || 0;
          return `${{n}} ${{n === 1 ? "dose" : "doses"}} this week`;
        }}
        if (adh.expected === 0 || adh.pct === null) return "No data yet";
        return `${{adh.pct}}% adherence (7d)`;
      }}

      function renderSchedules(items) {{
        listEl.innerHTML = "";
        if (!items.length) {{
          emptyEl.style.display = "";
          return;
        }}
        emptyEl.style.display = "none";
        for (const s of items) {{
          const card = document.createElement("article");
          card.className = "sched-card";
          const doseHtml = s.dose ? `<div class="sched-dose">${{escapeHtml(s.dose)}}</div>` : "";
          const badge = `<span class="${{badgeClass(s.adherence)}}">${{badgeText(s.adherence)}}</span>`;
          card.innerHTML = `
            <div class="sched-name">${{escapeHtml(s.name)}}</div>
            ${{doseHtml}}
            <div class="sched-meta">${{escapeHtml(s.frequency_label)}}</div>
            ${{badge}}
            <div class="sched-actions">
              <button type="button" class="btn-edit" data-action="edit" data-id="${{s.id}}">Edit</button>
              <button type="button" class="btn-delete" data-action="deactivate" data-id="${{s.id}}">Deactivate</button>
            </div>
          `;
          listEl.appendChild(card);
        }}
      }}

      async function loadSchedules() {{
        const res = await fetch("/api/medications/schedules", {{ cache: "no-store" }});
        if (!res.ok) throw new Error("load failed");
        const data = await res.json();
        renderSchedules(data.schedules || []);
      }}

      function escapeHtml(v) {{
        return (v || "").replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;");
      }}

      addBtn.addEventListener("click", () => {{
        resetForm();
        showForm(false);
        nameEl.focus();
      }});

      cancelBtn.addEventListener("click", hideForm);

      formEl.addEventListener("submit", async (e) => {{
        e.preventDefault();
        errorEl.style.display = "none";
        errorEl.textContent = "";
        const id = idEl.value.trim();
        const url = id ? `/api/medications/schedules/${{id}}/edit` : "/api/medications/schedules";
        const res = await fetch(url, {{
          method: "POST",
          body: new FormData(formEl),
        }});
        const payload = await res.json();
        if (!res.ok || !payload.ok) {{
          errorEl.textContent = payload.error || "Could not save schedule.";
          errorEl.style.display = "";
          return;
        }}
        hideForm();
        await loadSchedules();
      }});

      listEl.addEventListener("click", async (e) => {{
        const btn = e.target.closest("button[data-action]");
        if (!btn) return;
        const action = btn.getAttribute("data-action");
        const id = btn.getAttribute("data-id");
        if (!id) return;
        if (action === "edit") {{
          const res = await fetch("/api/medications/schedules", {{ cache: "no-store" }});
          if (!res.ok) return;
          const data = await res.json();
          const s = (data.schedules || []).find((x) => String(x.id) === String(id));
          if (!s) return;
          idEl.value = String(s.id);
          nameEl.value = s.name || "";
          doseEl.value = s.dose || "";
          freqEl.value = s.frequency || "once_daily";
          startDateEl.value = s.start_date || "{today_str}";
          notesEl.value = s.notes || "";
          showForm(true);
          nameEl.focus();
          return;
        }}
        if (action === "deactivate") {{
          if (!window.confirm("Stop tracking this schedule?")) return;
          const res = await fetch(`/api/medications/schedules/${{id}}/deactivate`, {{ method: "POST" }});
          if (!res.ok) return;
          await loadSchedules();
        }}
      }});

      loadSchedules().catch(() => {{
        emptyEl.style.display = "";
        emptyEl.textContent = "Could not load schedules.";
      }});
    }})();
  </script>
</body>
</html>"""


def _schedule_payload(conn, row, uid: int) -> dict:
    s = dict(row)
    adh = _adherence_7d(conn, s["id"], uid, s["start_date"], s["frequency"])
    return {
        "id": s["id"],
        "name": s["name"],
        "dose": s["dose"] or "",
        "notes": s["notes"] or "",
        "frequency": s["frequency"],
        "frequency_label": FREQ_LABELS.get(s["frequency"], s["frequency"]),
        "start_date": s["start_date"],
        "adherence": adh,
    }


@router.get("/api/medications/schedules")
def api_schedules_list():
    uid = _current_user_id.get()
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, name, dose, notes, frequency, start_date"
            " FROM medication_schedules WHERE user_id=? AND active=1 ORDER BY name",
            (uid,),
        ).fetchall()
        items = [_schedule_payload(conn, r, uid) for r in rows]
    return JSONResponse({"schedules": items})


@router.post("/api/medications/schedules")
def api_schedules_create(
    name: str = Form(""),
    dose: str = Form(""),
    frequency: str = Form(""),
    start_date: str = Form(""),
    notes: str = Form(""),
):
    if not name.strip():
        return JSONResponse({"ok": False, "error": "Medication name is required"}, status_code=400)
    if frequency not in VALID_FREQUENCIES:
        return JSONResponse({"ok": False, "error": "Invalid frequency"}, status_code=400)
    try:
        sd = date.fromisoformat(start_date)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid start date"}, status_code=400)
    if sd > date.today():
        return JSONResponse({"ok": False, "error": "Start date cannot be in the future"}, status_code=400)
    uid = _current_user_id.get()
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO medication_schedules (user_id, name, dose, notes, frequency, start_date, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (uid, name.strip(), dose.strip(), notes.strip(), frequency, start_date, created_at),
        )
        conn.commit()
        row = conn.execute(
            "SELECT id, name, dose, notes, frequency, start_date FROM medication_schedules WHERE id=? AND user_id=?",
            (cur.lastrowid, uid),
        ).fetchone()
        payload = _schedule_payload(conn, row, uid)
    return JSONResponse({"ok": True, "schedule": payload})


@router.post("/api/medications/schedules/{sched_id}/edit")
def api_schedules_edit(
    sched_id: int,
    name: str = Form(""),
    dose: str = Form(""),
    frequency: str = Form(""),
    start_date: str = Form(""),
    notes: str = Form(""),
):
    if not name.strip():
        return JSONResponse({"ok": False, "error": "Medication name is required"}, status_code=400)
    if frequency not in VALID_FREQUENCIES:
        return JSONResponse({"ok": False, "error": "Invalid frequency"}, status_code=400)
    try:
        sd = date.fromisoformat(start_date)
    except ValueError:
        return JSONResponse({"ok": False, "error": "Invalid start date"}, status_code=400)
    if sd > date.today():
        return JSONResponse({"ok": False, "error": "Start date cannot be in the future"}, status_code=400)
    uid = _current_user_id.get()
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM medication_schedules WHERE id=? AND user_id=? AND active=1",
            (sched_id, uid),
        ).fetchone()
        if row is None:
            return JSONResponse({"ok": False, "error": "Schedule not found"}, status_code=404)
        conn.execute(
            "UPDATE medication_schedules SET name=?, dose=?, notes=?, frequency=?, start_date=? WHERE id=? AND user_id=?",
            (name.strip(), dose.strip(), notes.strip(), frequency, start_date, sched_id, uid),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT id, name, dose, notes, frequency, start_date FROM medication_schedules WHERE id=? AND user_id=?",
            (sched_id, uid),
        ).fetchone()
        payload = _schedule_payload(conn, updated, uid)
    return JSONResponse({"ok": True, "schedule": payload})


@router.post("/api/medications/schedules/{sched_id}/deactivate")
def api_schedules_deactivate(sched_id: int):
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "UPDATE medication_schedules SET active=0 WHERE id=? AND user_id=?", (sched_id, uid)
        )
        conn.commit()
    return JSONResponse({"ok": True})


@router.get("/medications/schedules/new", response_class=HTMLResponse)
def schedules_new(error: str = ""):
    from routers.medications import _MED_DATALIST
    error_html = f'<div class="alert">{html.escape(error)}</div>' if error else ""
    today_str = date.today().isoformat()
    freq_options = "".join(
        f'<option value="{k}">{html.escape(v)}</option>' for k, v in FREQ_LABELS.items()
    )
    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}<title>Add Schedule</title></head>
<body>
  {_nav_bar('meds')}
  <div class="container">
    <h1>Add Medication Schedule</h1>
    {error_html}
    <div class="card">
      <form method="post" action="/medications/schedules">
        <div class="form-group">
          <label for="name">Medication name <span style="color:#ef4444">*</span></label>
          <input type="text" id="name" name="name" required list="med-suggestions" autocomplete="off">
          <datalist id="med-suggestions">{_MED_DATALIST}</datalist>
        </div>
        <div class="form-group">
          <label for="dose">Dose <span style="color:#aaa;font-weight:400">(optional)</span></label>
          <input type="text" id="dose" name="dose" placeholder="e.g. 500mg">
        </div>
        <div class="form-group">
          <label for="frequency">Frequency <span style="color:#ef4444">*</span></label>
          <select id="frequency" name="frequency"
            style="width:auto;border:1px solid #d1d5db;border-radius:6px;padding:8px 10px;font-size:15px;font-family:inherit;">
            {freq_options}
          </select>
        </div>
        <div class="form-group">
          <label for="start_date">Start date <span style="color:#ef4444">*</span></label>
          <input type="date" id="start_date" name="start_date" value="{today_str}" max="{today_str}" required style="width:auto;">
        </div>
        <div class="form-group">
          <label for="notes">Notes <span style="color:#aaa;font-weight:400">(optional)</span></label>
          <textarea id="notes" name="notes" rows="2" placeholder="Any additional details..."></textarea>
        </div>
        <button class="btn-primary" style="background:#7c3aed;" type="submit">Add Schedule</button>
      </form>
    </div>
  </div>
</body>
</html>"""


@router.post("/medications/schedules")
def schedules_create(
    name: str = Form(""),
    dose: str = Form(""),
    frequency: str = Form(""),
    start_date: str = Form(""),
    notes: str = Form(""),
):
    if not name.strip():
        return RedirectResponse(url="/medications/schedules/new?error=Medication+name+is+required", status_code=303)
    if frequency not in VALID_FREQUENCIES:
        return RedirectResponse(url="/medications/schedules/new?error=Invalid+frequency", status_code=303)
    try:
        sd = date.fromisoformat(start_date)
    except ValueError:
        return RedirectResponse(url="/medications/schedules/new?error=Invalid+start+date", status_code=303)
    if sd > date.today():
        return RedirectResponse(url="/medications/schedules/new?error=Start+date+cannot+be+in+the+future", status_code=303)
    uid = _current_user_id.get()
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO medication_schedules (user_id, name, dose, notes, frequency, start_date, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (uid, name.strip(), dose.strip(), notes.strip(), frequency, start_date, created_at),
        )
        conn.commit()
    return RedirectResponse(url="/medications/schedules", status_code=303)


@router.get("/medications/schedules/{sched_id}/edit", response_class=HTMLResponse)
def schedules_edit_get(sched_id: int, error: str = ""):
    from routers.medications import _MED_DATALIST
    uid = _current_user_id.get()
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM medication_schedules WHERE id=? AND user_id=?", (sched_id, uid)
        ).fetchone()
    if row is None:
        return RedirectResponse(url="/medications/schedules", status_code=303)
    s = dict(row)
    error_html = f'<div class="alert">{html.escape(error)}</div>' if error else ""
    today_str = date.today().isoformat()
    freq_options = "".join(
        f'<option value="{k}"{" selected" if k == s["frequency"] else ""}>{html.escape(v)}</option>'
        for k, v in FREQ_LABELS.items()
    )
    return f"""<!DOCTYPE html>
<html>
<head>{PAGE_STYLE}<title>Edit Schedule</title></head>
<body>
  {_nav_bar('meds')}
  <div class="container">
    <h1>Edit Schedule</h1>
    {error_html}
    <div class="card">
      <form method="post" action="/medications/schedules/{sched_id}/edit">
        <div class="form-group">
          <label for="name">Medication name <span style="color:#ef4444">*</span></label>
          <input type="text" id="name" name="name" value="{html.escape(s['name'])}"
            required list="med-suggestions" autocomplete="off">
          <datalist id="med-suggestions">{_MED_DATALIST}</datalist>
        </div>
        <div class="form-group">
          <label for="dose">Dose <span style="color:#aaa;font-weight:400">(optional)</span></label>
          <input type="text" id="dose" name="dose" value="{html.escape(s['dose'])}">
        </div>
        <div class="form-group">
          <label for="frequency">Frequency <span style="color:#ef4444">*</span></label>
          <select id="frequency" name="frequency"
            style="width:auto;border:1px solid #d1d5db;border-radius:6px;padding:8px 10px;font-size:15px;font-family:inherit;">
            {freq_options}
          </select>
        </div>
        <div class="form-group">
          <label for="start_date">Start date <span style="color:#ef4444">*</span></label>
          <input type="date" id="start_date" name="start_date"
            value="{html.escape(s['start_date'])}" max="{today_str}" required style="width:auto;">
        </div>
        <div class="form-group">
          <label for="notes">Notes <span style="color:#aaa;font-weight:400">(optional)</span></label>
          <textarea id="notes" name="notes" rows="2">{html.escape(s['notes'])}</textarea>
        </div>
        <div style="display:flex;gap:12px;align-items:center;">
          <button class="btn-primary" style="background:#7c3aed;" type="submit">Save Changes</button>
          <a href="/medications/schedules" class="back">Cancel</a>
        </div>
      </form>
    </div>
  </div>
</body>
</html>"""


@router.post("/medications/schedules/{sched_id}/edit")
def schedules_edit_post(
    sched_id: int,
    name: str = Form(""),
    dose: str = Form(""),
    frequency: str = Form(""),
    start_date: str = Form(""),
    notes: str = Form(""),
):
    if not name.strip():
        return RedirectResponse(url=f"/medications/schedules/{sched_id}/edit?error=Medication+name+is+required", status_code=303)
    if frequency not in VALID_FREQUENCIES:
        return RedirectResponse(url=f"/medications/schedules/{sched_id}/edit?error=Invalid+frequency", status_code=303)
    try:
        sd = date.fromisoformat(start_date)
    except ValueError:
        return RedirectResponse(url=f"/medications/schedules/{sched_id}/edit?error=Invalid+start+date", status_code=303)
    if sd > date.today():
        return RedirectResponse(url=f"/medications/schedules/{sched_id}/edit?error=Start+date+cannot+be+in+the+future", status_code=303)
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "UPDATE medication_schedules SET name=?,dose=?,notes=?,frequency=?,start_date=?"
            " WHERE id=? AND user_id=?",
            (name.strip(), dose.strip(), notes.strip(), frequency, start_date, sched_id, uid),
        )
        conn.commit()
    return RedirectResponse(url="/medications/schedules", status_code=303)


@router.post("/medications/schedules/{sched_id}/deactivate")
def schedules_deactivate(sched_id: int):
    uid = _current_user_id.get()
    with get_db() as conn:
        conn.execute(
            "UPDATE medication_schedules SET active=0 WHERE id=? AND user_id=?", (sched_id, uid)
        )
        conn.commit()
    return RedirectResponse(url="/medications/schedules", status_code=303)


# ── Dose logging actions ──────────────────────────────────────────────────────

@router.post("/medications/doses/take")
def doses_take(
    schedule_id: int = Form(...),
    scheduled_date: str = Form(...),
    dose_num: int = Form(1),
    taken_time: str = Form(""),
    client_now: str = Form(""),
    redirect_to: str = Form("/medications/today"),
):
    uid = _current_user_id.get()
    redirect_to = _safe_meds_redirect(redirect_to)
    scheduled_day = _parse_valid_scheduled_date(scheduled_date)
    if scheduled_day is None or dose_num < 1:
        return RedirectResponse(url=redirect_to, status_code=303)
    now_dt = datetime.now()
    if client_now.strip():
        try:
            now_dt = datetime.strptime(client_now.strip(), "%Y-%m-%dT%H:%M")
        except ValueError:
            pass
    taken_dt = now_dt
    if taken_time.strip():
        try:
            taken_dt = datetime.strptime(f"{scheduled_day.isoformat()} {taken_time.strip()}", "%Y-%m-%d %H:%M")
        except ValueError:
            return RedirectResponse(url=redirect_to, status_code=303)
        if taken_dt > now_dt:
            return RedirectResponse(url=redirect_to, status_code=303)
    taken_at = taken_dt.strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        sched = conn.execute(
            "SELECT frequency, start_date, created_at FROM medication_schedules WHERE id=? AND user_id=?",
            (schedule_id, uid),
        ).fetchone()
        if not sched:
            return RedirectResponse(url=redirect_to, status_code=303)
        sched_start = date.fromisoformat(sched["start_date"])
        if scheduled_day < sched_start:
            return RedirectResponse(url=redirect_to, status_code=303)
        created_at = _parse_created_at(sched["created_at"])
        if created_at and scheduled_day < created_at.date():
            return RedirectResponse(url=redirect_to, status_code=303)
        if created_at and scheduled_day == created_at.date() and taken_dt < created_at:
            return RedirectResponse(url=redirect_to, status_code=303)
        dpd = _doses_per_day(sched["frequency"])
        if dpd > 0 and dose_num > dpd:
            return RedirectResponse(url=redirect_to, status_code=303)
        conn.execute(
            "DELETE FROM medication_doses WHERE schedule_id=? AND user_id=? AND scheduled_date=? AND dose_num=?",
            (schedule_id, uid, scheduled_day.isoformat(), dose_num),
        )
        conn.execute(
            "INSERT INTO medication_doses (schedule_id, user_id, scheduled_date, dose_num, taken_at, status)"
            " VALUES (?,?,?,?,?,'taken')",
            (schedule_id, uid, scheduled_day.isoformat(), dose_num, taken_at),
        )
        conn.commit()
    return RedirectResponse(url=redirect_to, status_code=303)


@router.post("/medications/doses/miss")
def doses_miss(
    schedule_id: int = Form(...),
    scheduled_date: str = Form(...),
    dose_num: int = Form(1),
    redirect_to: str = Form("/medications/today"),
):
    uid = _current_user_id.get()
    redirect_to = _safe_meds_redirect(redirect_to)
    scheduled_day = _parse_valid_scheduled_date(scheduled_date)
    if scheduled_day is None or dose_num < 1:
        return RedirectResponse(url=redirect_to, status_code=303)
    with get_db() as conn:
        sched = conn.execute(
            "SELECT frequency, start_date, created_at FROM medication_schedules WHERE id=? AND user_id=?",
            (schedule_id, uid),
        ).fetchone()
        if not sched:
            return RedirectResponse(url=redirect_to, status_code=303)
        sched_start = date.fromisoformat(sched["start_date"])
        if scheduled_day < sched_start:
            return RedirectResponse(url=redirect_to, status_code=303)
        created_at = _parse_created_at(sched["created_at"])
        if created_at and scheduled_day < created_at.date():
            return RedirectResponse(url=redirect_to, status_code=303)
        dpd = _doses_per_day(sched["frequency"])
        if dpd > 0 and dose_num > dpd:
            return RedirectResponse(url=redirect_to, status_code=303)
        conn.execute(
            "DELETE FROM medication_doses WHERE schedule_id=? AND user_id=? AND scheduled_date=? AND dose_num=?",
            (schedule_id, uid, scheduled_day.isoformat(), dose_num),
        )
        conn.execute(
            "INSERT INTO medication_doses (schedule_id, user_id, scheduled_date, dose_num, taken_at, status)"
            " VALUES (?,?,?,?,'','missed')",
            (schedule_id, uid, scheduled_day.isoformat(), dose_num),
        )
        conn.commit()
    return RedirectResponse(url=redirect_to, status_code=303)


@router.post("/medications/doses/undo")
def doses_undo(
    schedule_id: int = Form(...),
    scheduled_date: str = Form(...),
    dose_num: int = Form(1),
    redirect_to: str = Form("/medications/today"),
):
    uid = _current_user_id.get()
    redirect_to = _safe_meds_redirect(redirect_to)
    scheduled_day = _parse_valid_scheduled_date(scheduled_date)
    if scheduled_day is None or dose_num < 1:
        return RedirectResponse(url=redirect_to, status_code=303)
    with get_db() as conn:
        sched = conn.execute(
            "SELECT frequency, start_date, created_at FROM medication_schedules WHERE id=? AND user_id=?",
            (schedule_id, uid),
        ).fetchone()
        if not sched:
            return RedirectResponse(url=redirect_to, status_code=303)
        sched_start = date.fromisoformat(sched["start_date"])
        if scheduled_day < sched_start:
            return RedirectResponse(url=redirect_to, status_code=303)
        created_at = _parse_created_at(sched["created_at"])
        if created_at and scheduled_day < created_at.date():
            return RedirectResponse(url=redirect_to, status_code=303)
        dpd = _doses_per_day(sched["frequency"])
        if dpd > 0 and dose_num > dpd:
            return RedirectResponse(url=redirect_to, status_code=303)
        conn.execute(
            "DELETE FROM medication_doses WHERE schedule_id=? AND user_id=? AND scheduled_date=? AND dose_num=?",
            (schedule_id, uid, scheduled_day.isoformat(), dose_num),
        )
        conn.commit()
    return RedirectResponse(url=redirect_to, status_code=303)


# ── Adherence API ─────────────────────────────────────────────────────────────

@router.get("/api/medications/adherence")
def api_medications_adherence():
    uid = _current_user_id.get()
    with get_db() as conn:
        schedules = conn.execute(
            "SELECT id, name, dose, frequency, start_date FROM medication_schedules WHERE user_id=? AND active=1",
            (uid,),
        ).fetchall()
        result = []
        for s in schedules:
            adh = _adherence_7d(conn, s["id"], uid, s["start_date"], s["frequency"])
            result.append({
                "name": s["name"],
                "dose": s["dose"],
                "frequency": s["frequency"],
                "adherence_7d_pct": adh["pct"],
                "taken_7d": adh["taken"],
            })
    return JSONResponse({"schedules": result})
