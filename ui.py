import html
import re
from datetime import datetime

from config import _current_user_id, _physician_ctx, CSRF_COOKIE_NAME
from db import get_db


def _severity_color(s):
    if s <= 3: return "#22c55e"   # green
    if s <= 6: return "#eab308"   # yellow
    if s <= 8: return "#f97316"   # orange
    return "#ef4444"              # red


def _calc_age(dob_str: str):
    if not dob_str:
        return None
    try:
        dob = datetime.strptime(dob_str, "%Y-%m-%d")
        today = datetime.today()
        return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    except ValueError:
        return None


_SIDEBAR_FREQ = {
    "once_daily":  "once daily",
    "twice_daily": "twice daily",
    "three_daily": "three times daily",
    "prn":         "as needed (PRN)",
}


def _sidebar_meds(conn, uid: int) -> list:
    """Return list of dicts: name, dose, freq (or None), href."""
    schedules = conn.execute(
        "SELECT name, dose, frequency FROM medication_schedules"
        " WHERE user_id=? AND active=1 ORDER BY name", (uid,),
    ).fetchall()
    seen, items = set(), []
    for r in schedules:
        freq = _SIDEBAR_FREQ.get(r["frequency"], r["frequency"])
        items.append({"name": r["name"], "dose": r["dose"] or "", "freq": freq, "href": "/medications/schedules"})
        seen.add(r["name"].lower())
    for r in conn.execute(
        "SELECT name, dose FROM medications WHERE user_id=?"
        " GROUP BY name ORDER BY MAX(timestamp) DESC", (uid,),
    ).fetchall():
        if r["name"].lower() not in seen:
            items.append({"name": r["name"], "dose": r["dose"] or "", "freq": None, "href": "/medications"})
            seen.add(r["name"].lower())
    return items


def _sidebar_meds_html(items: list) -> str:
    if not items:
        return '<em style="color:#d1d5db;">—</em>'
    rows = []
    for m in items:
        name_e = html.escape(m["name"])
        dose_e = html.escape(m["dose"])
        freq_e = html.escape(m["freq"]) if m["freq"] else ""
        meta_parts = []
        if dose_e:
            meta_parts.append(dose_e)
        if freq_e:
            meta_parts.append(freq_e)
        meta_html = (
            f'<span style="font-size:11px;color:#6b7280;display:block;margin-top:1px;">'
            f'{" · ".join(meta_parts)}</span>'
        ) if meta_parts else ""
        rows.append(
            f'<a href="{m["href"]}" style="display:block;text-decoration:none;padding:5px 7px;'
            f'border-radius:6px;margin-bottom:3px;background:#f9fafb;border:1px solid #e5e7eb;">'
            f'<span style="font-size:13px;font-weight:600;color:#1e3a8a;">{name_e}</span>'
            f'{meta_html}</a>'
        )
    return "".join(rows)


def _sidebar() -> str:
    uid = _current_user_id.get()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM user_profile WHERE id = ?", (uid,)).fetchone()
        meds_items = _sidebar_meds(conn, uid)
    p = dict(row) if row else {"name": "", "dob": "", "conditions": "", "medications": "", "photo_ext": ""}
    meds_html = _sidebar_meds_html(meds_items)
    photo_ext = p.get("photo_ext", "")
    if photo_ext:
        avatar = (
            '<a href="/profile" style="display:block;text-decoration:none;">'
            '<img src="/profile/photo" alt="Profile photo"'
            ' style="width:80px;height:80px;border-radius:50%;object-fit:cover;'
            'border:3px solid #e5e7eb;display:block;margin:0 auto;"></a>'
        )
    else:
        avatar = (
            '<a href="/profile" style="display:block;text-decoration:none;">'
            '<div style="width:80px;height:80px;border-radius:50%;background:#e5e7eb;'
            'display:flex;align-items:center;justify-content:center;margin:0 auto;">'
            '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 24 24"'
            ' fill="none" stroke="#9ca3af" stroke-width="1.5">'
            '<circle cx="12" cy="8" r="4"/><path d="M4 20c0-4 3.6-7 8-7s8 3 8 7"/>'
            '</svg></div></a>'
        )
    age = _calc_age(p.get("dob", ""))
    age_str = f"{age} years old" if age else ""
    name_esc = html.escape(p.get("name") or "")
    dob_esc = html.escape(p.get("dob") or "")
    conditions_raw = p.get("conditions") or ""
    cond_parts = [c.strip() for c in re.split(r"[,\n]", conditions_raw) if c.strip()]
    if cond_parts:
        cond_tags_html = "".join(
            f'<span style="display:inline-block;background:#f0f9ff;border:1px solid #bae6fd;'
            f'color:#0369a1;border-radius:20px;padding:3px 10px;font-size:11px;'
            f'font-weight:500;margin:2px 2px 2px 0;">{html.escape(c)}</span>'
            for c in cond_parts
        )
    else:
        cond_tags_html = '<em style="color:#d1d5db;font-size:13px;">—</em>'
    cond_esc = html.escape(conditions_raw)
    lbl = 'style="font-size:10px;font-weight:700;color:#9ca3af;text-transform:uppercase;letter-spacing:.08em;display:block;margin-bottom:6px;"'
    inp = 'style="width:100%;box-sizing:border-box;border:1px solid #d1d5db;border-radius:6px;padding:6px 8px;font-size:13px;font-family:inherit;"'
    ta  = 'style="width:100%;box-sizing:border-box;border:1px solid #d1d5db;border-radius:6px;padding:6px 8px;font-size:13px;font-family:inherit;resize:vertical;"'
    divider = '<hr style="border:none;border-top:1px solid #f3f4f6;margin:14px 0;">'
    return f"""<aside class="sidebar">
  <div style="text-align:center;padding-bottom:16px;border-bottom:2px solid #f3f4f6;margin-bottom:16px;">
    {avatar}
    <p id="sb-name-v" style="font-weight:700;font-size:16px;margin:10px 0 3px;color:#111;">{name_esc or '<em style="color:#aaa;font-style:normal;font-size:14px;">Add your name</em>'}</p>
    <p id="sb-age-v" style="font-size:12px;color:#9ca3af;margin:0;">{age_str}</p>
  </div>
  <div id="sb-view">
    <p {lbl}>Conditions</p>
    <div id="sb-cond-v" style="margin:0 0 4px;line-height:1.8;">{cond_tags_html}</div>
    {divider}
    <p {lbl}>Medications</p>
    <div id="sb-meds-v" style="margin:0 0 4px;">{meds_html}</div>
    {divider}
    <div style="display:flex;gap:6px;margin-top:4px;">
      <a href="/profile" style="flex:1;text-align:center;text-decoration:none;background:#f9fafb;border:1px solid #e5e7eb;border-radius:6px;padding:7px;font-size:12px;color:#374151;font-weight:500;">Full profile</a>
      <button onclick="sbToggle(true)" style="flex:1;background:#1e3a8a;color:#fff;border:none;border-radius:6px;padding:7px;font-size:12px;font-weight:500;cursor:pointer;font-family:inherit;">&#9998; Edit</button>
    </div>
  </div>
  <form id="sb-edit" style="display:none;" onsubmit="sbSave(event)">
    <div style="margin-bottom:10px;"><label {lbl}>Name</label>
      <input type="text" name="name" id="sb-name-i" value="{name_esc}" {inp}></div>
    <div style="margin-bottom:10px;"><label {lbl}>Date of Birth</label>
      <input type="date" name="dob" id="sb-dob-i" value="{dob_esc}" {inp}></div>
    <div style="margin-bottom:14px;"><label {lbl}>Conditions</label>
      <textarea name="conditions" id="sb-cond-i" rows="3" {ta}>{cond_esc}</textarea>
      <span style="font-size:11px;color:#9ca3af;margin-top:3px;display:block;">Separate with commas</span></div>
    <div style="display:flex;gap:6px;">
      <button type="submit" style="flex:1;background:#1e3a8a;color:#fff;border:none;border-radius:6px;padding:7px;font-size:13px;font-weight:600;cursor:pointer;font-family:inherit;">Save</button>
      <button type="button" onclick="sbToggle(false)" style="flex:1;background:none;border:1px solid #e5e7eb;border-radius:6px;padding:7px;font-size:13px;color:#6b7280;cursor:pointer;font-family:inherit;">Cancel</button>
    </div>
  </form>
</aside>
<script>
document.body.classList.add('has-sidebar');
(function(){{
  var nav = document.querySelector('nav');
  var sb  = document.querySelector('.sidebar');
  if (nav && sb) sb.style.top = nav.getBoundingClientRect().bottom + 'px';
}})();
function sbToggle(e){{document.getElementById('sb-view').style.display=e?'none':'';document.getElementById('sb-edit').style.display=e?'':'none';}}
function sbCookie(name){{
  const prefix = name + "=";
  return document.cookie.split(";").map(v=>v.trim()).find(v=>v.startsWith(prefix))?.slice(prefix.length) || "";
}}
function sbSetTextOrPlaceholder(elId, text, placeholderStyle, placeholderText){{
  const el = document.getElementById(elId);
  el.textContent = "";
  if (text) {{
    el.textContent = text;
    return;
  }}
  const ph = document.createElement("em");
  ph.setAttribute("style", placeholderStyle);
  ph.textContent = placeholderText;
  el.appendChild(ph);
}}
async function sbSave(e){{
  e.preventDefault();
  const fd=new FormData(document.getElementById('sb-edit'));
  await fetch('/api/profile',{{method:'POST',headers:{{'X-CSRF-Token':sbCookie('csrf_token')}},body:fd}});
  const name=(document.getElementById('sb-name-i').value||'').trim();
  const cond=(document.getElementById('sb-cond-i').value||'').trim();
  const dob=document.getElementById('sb-dob-i').value;
  sbSetTextOrPlaceholder('sb-name-v', name, 'color:#aaa;font-style:normal;font-size:14px;', 'Add your name');
  const condEl = document.getElementById('sb-cond-v');
  condEl.innerHTML = '';
  if (cond) {{
    cond.split(/[,\\n]/).map(s => s.trim()).filter(Boolean).forEach(c => {{
      const tag = document.createElement('span');
      tag.textContent = c;
      tag.setAttribute('style', 'display:inline-block;background:#f0f9ff;border:1px solid #bae6fd;color:#0369a1;border-radius:20px;padding:3px 10px;font-size:11px;font-weight:500;margin:2px 2px 2px 0;');
      condEl.appendChild(tag);
    }});
  }} else {{
    condEl.innerHTML = '<em style="color:#d1d5db;font-size:13px;">\u2014</em>';
  }}
  if(dob){{const t=new Date(),d=new Date(dob+'T00:00:00');let a=t.getFullYear()-d.getFullYear();if(t<new Date(t.getFullYear(),d.getMonth(),d.getDate()))a--;document.getElementById('sb-age-v').textContent=a+'y';}}
  else{{document.getElementById('sb-age-v').textContent='';}}
  sbToggle(false);
}}
</script>"""


def _nav_bar(active: str = "") -> str:
    physician_banner = ""
    patient_name = _physician_ctx.get()
    logout_action = "/physician/logout" if patient_name is not None else "/logout"
    if patient_name is not None:
        physician_banner = (
            '<div style="background:#fef9c3; border-bottom:1px solid #fde047; padding:6px 24px;'
            ' display:flex; align-items:center; justify-content:space-between; font-size:13px;">'
            f'<span>&#128104;&#8205;&#9877;&#65039; Physician view &mdash; <strong>{html.escape(patient_name)}</strong></span>'
            '<form method="post" action="/physician/exit" style="margin:0;">'
            '<button type="submit" style="background:#854d0e; color:#fff; border:none; border-radius:6px;'
            ' padding:4px 12px; font-size:13px; cursor:pointer; font-family:inherit;">'
            '&#8592; Exit to portal</button>'
            '</form>'
            '</div>'
        )
    def dlnk(href, label, key):
        """Desktop nav link with active underline indicator."""
        if active == key:
            s = "color:#fff; font-weight:600; border-bottom:2px solid rgba(255,255,255,0.8); padding-bottom:2px;"
        else:
            s = "color:rgba(255,255,255,0.7); font-weight:500;"
        return f'<a href="{href}" style="text-decoration:none; font-size:14px; {s}">{label}</a>'
    def mlnk(href, label, key):
        """Mobile dropdown link."""
        s = "color:#fff; font-weight:600;" if active == key else "color:rgba(255,255,255,0.85); font-weight:400;"
        return (
            f'<a href="{href}" style="text-decoration:none; font-size:15px;'
            f' padding:12px 0; border-bottom:1px solid rgba(255,255,255,0.1); {s}">{label}</a>'
        )
    return (
        physician_banner
        + '<nav style="background:#1e3a8a;">'
        # ── Desktop row ───────────────────────────────────────────────────
        '<div style="padding:0 24px; height:52px; display:flex; align-items:center; gap:20px;">'
        '<span style="font-weight:800; color:#fff; font-size:15px; flex-shrink:0; margin-right:8px;">'
        'Symptom Tracker</span>'
        '<div class="nav-desktop-links">'
        + dlnk("/symptoms/chart", "Health Report", "chart")
        + dlnk("/symptoms/calendar", "Calendar", "calendar")
        + dlnk("/symptoms", "Symptoms", "list")
        + dlnk("/medications/today", "Meds", "meds")
        + '</div>'
        '<div class="nav-desktop-actions">'
        '<a href="/symptoms/new" style="background:#fff; color:#1e3a8a; text-decoration:none;'
        ' font-size:13px; font-weight:700; padding:6px 14px; border-radius:20px; white-space:nowrap;">'
        '+ Log Symptom</a>'
        '<a href="/medications" style="background:#a855f7; color:#fff; text-decoration:none;'
        ' font-size:13px; font-weight:700; padding:6px 14px; border-radius:20px; white-space:nowrap;">'
        '+ Log Medication</a>'
        + dlnk("/profile", "Profile", "profile")
        + f'<form method="post" action="{logout_action}" style="margin:0;">'
        '<button type="submit" style="background:transparent; border:1px solid rgba(255,255,255,0.4);'
        ' color:rgba(255,255,255,0.7); border-radius:6px; padding:4px 12px;'
        ' font-size:13px; cursor:pointer; font-family:inherit;">Log Out</button>'
        '</form>'
        '</div>'
        # Hamburger (hidden on desktop, shown on mobile via CSS)
        '<button type="button" class="nav-hamburger" id="nav-toggle" aria-label="Open menu"'
        ' onclick="window._navToggle&&window._navToggle(event);return false;">&#9776;</button>'
        '</div>'
        # ── Mobile dropdown ───────────────────────────────────────────────
        '<div id="nav-menu">'
        + mlnk("/symptoms/chart", "Health Report", "chart")
        + mlnk("/symptoms/calendar", "Calendar", "calendar")
        + mlnk("/symptoms", "Symptoms", "list")
        + mlnk("/medications/today", "Meds", "meds")
        + mlnk("/profile", "Profile", "profile")
        + '<div style="display:flex; gap:8px; flex-wrap:wrap; padding:12px 0 4px;">'
        '<a href="/symptoms/new" style="background:#fff; color:#1e3a8a; text-decoration:none;'
        ' font-size:13px; font-weight:700; padding:7px 14px; border-radius:20px; white-space:nowrap;">'
        '+ Log Symptom</a>'
        '<a href="/medications" style="background:#a855f7; color:#fff; text-decoration:none;'
        ' font-size:13px; font-weight:700; padding:7px 14px; border-radius:20px; white-space:nowrap;">'
        '+ Log Medication</a>'
        f'<form method="post" action="{logout_action}" style="margin:0;">'
        '<button type="submit" style="background:transparent; border:1px solid rgba(255,255,255,0.4);'
        ' color:rgba(255,255,255,0.7); border-radius:6px; padding:6px 12px;'
        ' font-size:13px; cursor:pointer; font-family:inherit;">Log Out</button>'
        '</form>'
        '</div>'
        '</div>'
        '</nav>'
        '<script>window._navToggle=function(){'
        'var m=document.getElementById(\'nav-menu\');'
        'var b=document.getElementById(\'nav-toggle\');'
        'if(!m||!b)return;'
        'm.classList.toggle(\'open\');'
        'b.innerHTML=m.classList.contains(\'open\')?\'&#10005;\':\'&#9776;\';'
        'if(window._applyClientTimeDefaults){window._applyClientTimeDefaults(document);}'
        '};'
        'window._navToggleAlias=window._navToggle;'
        'function _navToggle(e){return window._navToggleAlias&&window._navToggleAlias(e);}'
        '</script>'
        + _sidebar()
    )


PAGE_STYLE = """
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <script>
    (function () {
      // Backward-compatible global nav toggle for pages that still call _navToggle().
      var _navToggleImpl = function () {
        var m = document.getElementById("nav-menu");
        var b = document.getElementById("nav-toggle");
        if (!m || !b) return;
        m.classList.toggle("open");
        b.innerHTML = m.classList.contains("open") ? "&#10005;" : "&#9776;";
      };
      window._navToggle = _navToggleImpl;
      window._navToggleAlias = _navToggleImpl;
      // Ensure bare global name exists for inline onclick="_navToggle()".
      window._navToggleLegacy = function (e) { return _navToggleImpl(e); };
      // eslint-disable-next-line no-var
      var _navToggle = window._navToggleLegacy;

      function clientNowLocal() {
        var n = new Date();
        var l = new Date(n.getTime() - n.getTimezoneOffset() * 60000);
        return l.toISOString().slice(0, 16);
      }
      function setCookie(name, value) {
        document.cookie = name + "=" + encodeURIComponent(value) + "; path=/; max-age=31536000; SameSite=Lax";
      }
      window._clientNowLocal = clientNowLocal;
      window._applyClientTimeDefaults = function (root) {
        var ctx = root || document;
        var nowStr = clientNowLocal();
        var dayStr = nowStr.slice(0, 10);
        window.__clientNowLocal = nowStr;
        window.__clientDateLocal = dayStr;
        ctx.querySelectorAll("form").forEach(function (f) {
          var c = f.querySelector('input[name="client_now"]');
          if (!c) {
            c = document.createElement("input");
            c.type = "hidden";
            c.name = "client_now";
            f.appendChild(c);
          }
          c.value = nowStr;
          if (!f.dataset.clientNowBound) {
            f.addEventListener("submit", function () { c.value = clientNowLocal(); });
            f.dataset.clientNowBound = "1";
          }
        });
        ctx.querySelectorAll('input[type="date"]').forEach(function (el) {
          if (!el.value && !el.dataset.noClientDefault) el.value = dayStr;
          if (!el.max) el.max = dayStr;
        });
        ctx.querySelectorAll('input[type="datetime-local"]').forEach(function (el) {
          if (!el.value && !el.dataset.noClientDefault) el.value = nowStr;
          if (!el.max) el.max = nowStr;
        });
      };
      setCookie("tz_offset", String(new Date().getTimezoneOffset()));
      try {
        var tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
        if (tz) setCookie("tz", tz);
      } catch (_) {}
      if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", function () {
          window._applyClientTimeDefaults(document);
        });
      } else {
        window._applyClientTimeDefaults(document);
      }
    })();
  </script>
  <style>
    body { font-family: system-ui, sans-serif; background: #f5f5f5; margin: 0; padding: 0; color: #222; }
    .container { max-width: 560px; margin: 0 auto; padding: 24px; }
    h1 { margin-bottom: 4px; }
    .card { background: #fff; border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px; margin: 12px 0; }
    .card-header { display: flex; align-items: center; gap: 10px; }
    .badge { display: inline-block; width: 36px; height: 36px; border-radius: 50%;
             color: #fff; font-weight: 700; font-size: 15px;
             display: flex; align-items: center; justify-content: center; flex-shrink: 0; }
    .card-name { font-size: 17px; font-weight: 600; }
    .card-ts { font-size: 12px; color: #888; margin-top: 2px; }
    .card-notes { margin: 10px 0 0; font-size: 14px; color: #444; }
    .btn-delete { background: none; border: 1px solid #e0e0e0;
                  border-radius: 6px; padding: 4px 10px; font-size: 13px; color: #888;
                  cursor: pointer; }
    .btn-delete:hover { background: #fee2e2; border-color: #ef4444; color: #ef4444; }
    .btn-edit { font-size: 13px; color: #3b82f6; border: 1px solid #d1d5db;
                border-radius: 6px; padding: 4px 10px; text-decoration: none; display: inline-block; }
    .btn-edit:hover { background: #eff6ff; border-color: #3b82f6; }
    .btn-primary { background: #3b82f6; color: #fff; border: none; border-radius: 8px;
                   padding: 10px 22px; font-size: 15px; cursor: pointer; font-weight: 600; }
    .btn-primary:hover { background: #2563eb; }
    .btn-log { display: inline-block; background: #3b82f6; color: #fff; text-decoration: none;
               border-radius: 8px; padding: 8px 16px; font-size: 14px; font-weight: 600;
               margin-bottom: 8px; }
    .btn-log:hover { background: #2563eb; }
    .back { font-size: 14px; color: #3b82f6; text-decoration: none; }
    .back:hover { text-decoration: underline; }
    .form-group { margin-bottom: 20px; }
    label { display: block; font-weight: 600; font-size: 14px; margin-bottom: 6px; }
    input[type=text], input[type=password], input[type=email], input[type=date], input[type=datetime-local], textarea { width: 100%; box-sizing: border-box; border: 1px solid #d1d5db;
      border-radius: 6px; padding: 8px 10px; font-size: 15px; font-family: inherit; }
    input[type=text]:focus, input[type=password]:focus, input[type=email]:focus, input[type=date]:focus, input[type=datetime-local]:focus, textarea:focus { outline: 2px solid #3b82f6; border-color: transparent; }
    .slider-row { display: flex; align-items: center; gap: 14px; }
    input[type=range] { flex: 1; accent-color: #3b82f6; height: 6px; cursor: pointer; }
    .sev-badge { width: 42px; height: 42px; border-radius: 50%; color: #fff; font-weight: 700;
                 font-size: 18px; display: flex; align-items: center; justify-content: center;
                 flex-shrink: 0; transition: background 0.2s; }
    .sev-labels { display: flex; justify-content: space-between; font-size: 11px; color: #888;
                  margin-top: 4px; }
    .alert { background: #fee2e2; border: 1px solid #fca5a5; color: #b91c1c;
             border-radius: 6px; padding: 10px 14px; margin-bottom: 16px; font-size: 14px; }
    .empty { color: #888; font-style: italic; margin-top: 16px; }
    .btn-primary.med-submit { background: #7c3aed; }
    .btn-primary.med-submit:hover { background: #6d28d9; }
    /* ── Nav responsive ────────────────────────────────────────────────── */
    .nav-desktop-links { flex: 1; display: flex; gap: 20px; }
    .nav-desktop-actions { display: flex; align-items: center; gap: 16px; flex-shrink: 0; }
    .nav-hamburger { display: none; background: none; border: none; color: #fff;
                     font-size: 22px; cursor: pointer; padding: 4px 8px; line-height: 1;
                     margin-left: auto; }
    #nav-menu { display: none; flex-direction: column;
                padding: 4px 24px 16px; background: #1e3a8a;
                border-top: 1px solid rgba(255,255,255,0.15); }
    #nav-menu.open { display: flex; }
    @media (max-width: 640px) {
      .nav-desktop-links  { display: none; }
      .nav-desktop-actions { display: none; }
      .nav-hamburger { display: block; }
      .container { padding: 16px; }
    }
    /* ── Profile sidebar ────────────────────────────────────────────── */
    .sidebar {
      position: fixed; left: 0; top: 52px; bottom: 0; width: 300px;
      background: #fff; border-right: 1px solid #e5e7eb;
      overflow-y: auto; padding: 20px 16px; box-sizing: border-box; z-index: 5;
    }
    body.has-sidebar .container { margin-left: 324px; margin-right: 24px; }
    @media (max-width: 900px) {
      .sidebar { display: none; }
      body.has-sidebar .container { margin-left: auto; margin-right: auto; }
    }
  </style>
"""
