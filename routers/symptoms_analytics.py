from collections import defaultdict

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from analysis import _compute_correlations, _pearson
from config import _current_user_id
from db import get_db
from ui import PAGE_STYLE, _nav_bar

router = APIRouter()


@router.get("/api/symptoms/correlations")
def api_symptoms_correlations(from_date: str = "", to_date: str = ""):
    uid = _current_user_id.get()
    clauses: list[str] = ["user_id = ?"]
    params: list = [uid]
    if from_date:
        clauses.append("substr(timestamp, 1, 10) >= ?")
        params.append(from_date)
    if to_date:
        clauses.append("substr(timestamp, 1, 10) <= ?")
        params.append(to_date)
    where = "WHERE " + " AND ".join(clauses)
    with get_db() as conn:
        rows = conn.execute(f"""
            SELECT name, substr(timestamp, 1, 10) AS date, AVG(severity) AS avg_severity
            FROM symptoms {where}
            GROUP BY name, date
        """, params).fetchall()
    names, matrix = _compute_correlations(rows)
    return JSONResponse({"names": names, "matrix": matrix})


@router.get("/api/correlations/med-symptom")
def api_med_symptom_correlations(from_date: str = "", to_date: str = ""):
    uid = _current_user_id.get()
    clauses: list[str] = ["user_id = ?"]
    params: list = [uid]
    if from_date:
        clauses.append("substr(timestamp, 1, 10) >= ?")
        params.append(from_date)
    if to_date:
        clauses.append("substr(timestamp, 1, 10) <= ?")
        params.append(to_date)
    where = "WHERE " + " AND ".join(clauses)
    with get_db() as conn:
        symp_rows = conn.execute(f"""
            SELECT name, substr(timestamp, 1, 10) AS date, AVG(severity) AS avg_severity
            FROM symptoms {where} GROUP BY name, date
        """, params).fetchall()
        med_rows = conn.execute(f"""
            SELECT name, substr(timestamp, 1, 10) AS date, COUNT(*) AS cnt
            FROM medications {where} GROUP BY name, date
        """, params).fetchall()
    symp_avg = {(r["name"], r["date"]): r["avg_severity"] for r in symp_rows}
    dates_by_symp = defaultdict(set)
    for name, date in symp_avg:
        dates_by_symp[name].add(date)
    med_cnt = {(r["name"], r["date"]): r["cnt"] for r in med_rows}
    symp_names = sorted(dates_by_symp)
    med_names = sorted({r["name"] for r in med_rows})
    if not symp_names or not med_names:
        return JSONResponse({"med_names": [], "symp_names": [], "matrix": []})
    matrix = []
    for med in med_names:
        row = []
        for symp in symp_names:
            dates = sorted(dates_by_symp[symp])
            xs = [med_cnt.get((med, d), 0) for d in dates]
            ys = [symp_avg[(symp, d)] for d in dates]
            row.append(_pearson(xs, ys) if sum(xs) > 0 else None)
        matrix.append(row)
    return JSONResponse({"med_names": med_names, "symp_names": symp_names, "matrix": matrix})


@router.get("/symptoms/chart", response_class=HTMLResponse)
def symptoms_chart():
    import html as _html
    uid = _current_user_id.get()
    with get_db() as conn:
        _row = conn.execute("SELECT name FROM user_profile WHERE id=?", (uid,)).fetchone()
    patient_name = _html.escape(_row["name"] if _row and _row["name"] else "")
    return f"""<!DOCTYPE html>
<html>
<head>
  {PAGE_STYLE}
  <title>Health Report</title>
  <style>
@media print {{
  nav, .screen-only {{ display: none !important; }}
  .print-only        {{ display: block !important; }}
  body               {{ font-size: 11pt; }}
  .container         {{ max-width: 100% !important; padding: 0 !important; }}
  #chart-wrapper     {{ display: block !important; box-shadow: none !important;
                       border: 1px solid #e5e7eb !important; }}
  canvas             {{ max-width: 100% !important; }}
  #corr-wrapper, #med-corr-wrapper, #insights-wrapper {{ display: block !important; }}
  details            {{ display: block; }}
  details > *        {{ display: block !important; }}
  details summary    {{ display: none !important; }}
  h3, h2             {{ page-break-after: avoid; }}
}}
  </style>
</head>
<body>
  {_nav_bar('chart')}
  <div class="container" style="max-width:860px;">
    <div class="print-only" style="display:none; border-bottom:2px solid #1e3a8a; padding-bottom:12px; margin-bottom:16px;">
      <h2 style="margin:0 0 6px; font-size:18pt; color:#1e3a8a;">Health Report</h2>
      <p style="margin:2px 0; font-size:11pt;"><strong>Patient:</strong> {patient_name}</p>
      <p style="margin:2px 0; font-size:11pt;"><strong>Period:</strong> <span id="print-date-range"></span></p>
      <p style="margin:2px 0; font-size:10pt; color:#6b7280;">Generated <span id="print-generated-date"></span></p>
    </div>
    <div class="screen-only" style="display:flex; align-items:center; justify-content:space-between; gap:12px; flex-wrap:wrap;">
      <h1 style="margin:0;">Health Report</h1>
      <button onclick="printReport()" style="border:1px solid #7c3aed; background:#7c3aed; color:#fff; border-radius:6px; padding:6px 12px; font-size:13px; cursor:pointer; font-family:inherit;">Print Report</button>
    </div>

    <div id="no-data" class="empty" style="display:none; margin-top:28px;">
      Not enough data yet &mdash; log at least 2 symptoms first.
    </div>

    <div id="insights-wrapper" class="card" style="display:none; margin-top:20px; padding:20px;"></div>

    <h2 class="screen-only" style="margin:20px 0 10px; font-size:18px; font-weight:700; color:#111827;">Symptom Trend Graph</h2>

    <div class="screen-only" style="display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-top:0;">
      <div style="display:flex; align-items:center; gap:6px;">
        <label for="range-from" style="font-size:13px; font-weight:600; color:#555;">From</label>
        <input type="date" id="range-from" onchange="render()"
          style="border:1px solid #d1d5db; border-radius:6px; padding:5px 8px; font-size:13px; font-family:inherit;">
      </div>
      <div style="display:flex; align-items:center; gap:6px;">
        <label for="range-to" style="font-size:13px; font-weight:600; color:#555;">To</label>
        <input type="date" id="range-to" onchange="render()"
          style="border:1px solid #d1d5db; border-radius:6px; padding:5px 8px; font-size:13px; font-family:inherit;">
      </div>
      <div style="display:flex; gap:6px;">
        <button onclick="setPreset(7)"  style="border:1px solid #d1d5db; background:#fff; border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; font-family:inherit;">7d</button>
        <button onclick="setPreset(30)" style="border:1px solid #d1d5db; background:#fff; border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; font-family:inherit;">30d</button>
        <button onclick="setPreset(90)" style="border:1px solid #d1d5db; background:#fff; border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; font-family:inherit;">90d</button>
        <button onclick="setPresetAll()" style="border:1px solid #d1d5db; background:#fff; border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; font-family:inherit;">All</button>
      </div>
      <button id="smooth-btn" onclick="toggleSmooth()" data-help="Smooth averages symptom severity over recent days to reduce short-term noise. Turn it off to see raw day-to-day changes." style="border:1px solid #1e3a8a; background:#1e3a8a; color:#fff; border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; font-family:inherit;">Smooth</button>
      <button id="bucket-btn" onclick="toggleBucket()" data-help="Daily shows each day separately. Weekly groups data into week buckets (Mon-Sun) so overall trends are easier to read." style="border:1px solid #0f766e; background:#0f766e; color:#fff; border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; font-family:inherit;">Daily</button>
    </div>

    <div id="chart-wrapper" class="card" style="display:none; margin-top:20px; padding:24px;">
      <div id="toggle-bar" style="display:flex; flex-wrap:wrap; gap:6px; margin-bottom:16px;"></div>
      <canvas id="symptomChart"></canvas>
    </div>
    <div id="med-tooltip" style="display:none; position:fixed; background:#1e3a8a; color:#fff;
      padding:6px 10px; border-radius:6px; font-size:13px; pointer-events:none; z-index:100;
      white-space:nowrap; box-shadow:0 2px 8px rgba(0,0,0,0.2); line-height:1.5;"></div>
    <div id="control-tooltip" style="display:none; position:fixed; background:#111827; color:#fff;
      padding:8px 10px; border-radius:8px; font-size:12px; pointer-events:none; z-index:120;
      max-width:280px; box-shadow:0 6px 18px rgba(0,0,0,0.24); line-height:1.45;"></div>

    <div id="corr-wrapper" style="display:none; margin-top:32px;">
      <details>
        <summary style="cursor:pointer; padding:10px 14px; background:#f9fafb; border:1px solid #e5e7eb;
          border-radius:8px; font-size:16px; font-weight:700; color:#111; user-select:none;">
          How Symptoms Connect
        </summary>
        <div style="margin-top:10px;">
          <p style="font-size:12px; color:#6b7280; margin:0 0 8px; display:flex; gap:14px; flex-wrap:wrap; align-items:center;">
            <span><span style="display:inline-block;width:10px;height:10px;background:#ffaaaa;border-radius:2px;vertical-align:middle;margin-right:3px;"></span>tend to occur together</span>
            <span><span style="display:inline-block;width:10px;height:10px;background:#aaaaff;border-radius:2px;vertical-align:middle;margin-right:3px;"></span>tend to alternate</span>
            <span><span style="display:inline-block;width:10px;height:10px;background:#e5e7eb;border-radius:2px;vertical-align:middle;margin-right:3px;"></span>no data</span>
          </p>
          <div id="corr-table" style="overflow-x:auto;"></div>
        </div>
      </details>
    </div>

    <div id="med-corr-wrapper" style="display:none; margin-top:32px;">
      <details>
        <summary style="cursor:pointer; padding:10px 14px; background:#f9fafb; border:1px solid #e5e7eb;
          border-radius:8px; font-size:16px; font-weight:700; color:#111; user-select:none;">
          How Medications Affect Symptoms
        </summary>
        <div style="margin-top:10px;">
          <p style="font-size:12px; color:#6b7280; margin:0 0 8px; display:flex; gap:14px; flex-wrap:wrap; align-items:center;">
            <span><span style="display:inline-block;width:10px;height:10px;background:#ffaaaa;border-radius:2px;vertical-align:middle;margin-right:3px;"></span>taken more on bad days</span>
            <span><span style="display:inline-block;width:10px;height:10px;background:#aaaaff;border-radius:2px;vertical-align:middle;margin-right:3px;"></span>taken more on good days</span>
            <span><span style="display:inline-block;width:10px;height:10px;background:#e5e7eb;border-radius:2px;vertical-align:middle;margin-right:3px;"></span>no data</span>
          </p>
          <div id="med-corr-table" style="overflow-x:auto;"></div>
        </div>
      </details>
    </div>
    <div id="adherence-print-section" class="print-only" style="display:none; margin-top:24px;"></div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
  <script src="https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3/dist/chartjs-plugin-annotation.min.js"></script>
  <script>
    const PALETTE = [
      "#3b82f6","#ef4444","#22c55e","#f97316","#a855f7",
      "#06b6d4","#eab308","#ec4899","#14b8a6","#f43f5e","#8b5cf6","#84cc16"
    ];
    const MED_PALETTE = ["#7c3aed","#9333ea","#a855f7","#6d28d9","#c026d3","#0ea5e9","#0f766e","#b45309"];
    function showMedTooltip(e, name, dose, time) {{
      const tip = document.getElementById("med-tooltip");
      let html = `<strong>${{escHtml(name)}}</strong>`;
      if (dose) html += `<br>${{escHtml(dose)}}`;
      html += `<br>${{time}}`;
      tip.innerHTML = html;
      tip.style.display = "block";
      tip.style.left = (e.clientX + 14) + "px";
      tip.style.top  = (e.clientY - 10) + "px";
    }}
    function hideMedTooltip() {{
      document.getElementById("med-tooltip").style.display = "none";
    }}

    function showControlTip(target) {{
      const tip = document.getElementById("control-tooltip");
      const msg = target.getAttribute("data-help") || "";
      if (!msg) return;
      tip.textContent = msg;
      tip.style.display = "block";
      const rect = target.getBoundingClientRect();
      const w = tip.offsetWidth || 260;
      const h = tip.offsetHeight || 44;
      let left = rect.left + (rect.width - w) / 2;
      let top = rect.top - h - 10;
      if (left < 8) left = 8;
      if (left + w > window.innerWidth - 8) left = window.innerWidth - w - 8;
      if (top < 8) top = rect.bottom + 10;
      tip.style.left = left + "px";
      tip.style.top = top + "px";
    }}

    function hideControlTip() {{
      document.getElementById("control-tooltip").style.display = "none";
    }}

    function bindControlTips() {{
      ["smooth-btn", "bucket-btn"].forEach((id) => {{
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener("mouseenter", () => showControlTip(el));
        el.addEventListener("focus", () => showControlTip(el));
        el.addEventListener("mouseleave", hideControlTip);
        el.addEventListener("blur", hideControlTip);
      }});
    }}

    function escHtml(v) {{
      return String(v)
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#39;");
    }}

    function fmtDate(dateStr) {{
      const d = new Date(dateStr + "T00:00:00Z");
      return d.toLocaleDateString("en-US", {{ month: "short", day: "numeric", timeZone: "UTC" }});
    }}

    function addDays(dateStr, days) {{
      const d = new Date(dateStr + "T00:00:00Z");
      d.setUTCDate(d.getUTCDate() + days);
      return d.toISOString().slice(0, 10);
    }}

    function bucketKey(dateStr) {{
      if (_timeBucket !== "weekly") return dateStr;
      const d = new Date(dateStr + "T00:00:00Z");
      const day = (d.getUTCDay() + 6) % 7; // Monday=0
      d.setUTCDate(d.getUTCDate() - day);
      return d.toISOString().slice(0, 10);
    }}

    function bucketLabel(key) {{
      if (_timeBucket !== "weekly") return fmtDate(key);
      const end = addDays(key, 6);
      return `${{fmtDate(key)}} - ${{fmtDate(end)}}`;
    }}

    function expandSymptomDates(s) {{
      const start = s.timestamp.slice(0, 10);
      const end   = s.end_time ? s.end_time.slice(0, 10) : start;
      if (start === end) return [start];
      const dates = [];
      let d = new Date(start + "T00:00:00");
      const last = new Date(end + "T00:00:00");
      while (d <= last) {{
        dates.push(d.toISOString().slice(0, 10));
        d = new Date(d.getTime() + 86400000);
      }}
      return dates;
    }}

    function stableIndex(name, size) {{
      let h = 2166136261;
      for (let i = 0; i < name.length; i++) {{
        h ^= name.charCodeAt(i);
        h = Math.imul(h, 16777619);
      }}
      return ((h >>> 0) % size);
    }}

    function corrColor(r) {{
      if (r === null) return {{ bg: "#e5e7eb", text: "#9ca3af" }};
      const t = Math.abs(r);
      const light = Math.round(255 * (1 - t));
      const bg = r >= 0
        ? `rgb(255,${{light}},${{light}})`
        : `rgb(${{light}},${{light}},255)`;
      return {{ bg, text: t > 0.55 ? "#fff" : "#333" }};
    }}

    let _allSymp = [], _allMeds = [], _adherenceData = {{}}, _chart = null, _smoothed = true, _timeBucket = "daily";

    async function init() {{
      bindControlTips();
      const [sr, mr, ar] = await Promise.all([
        fetch("/api/symptoms"), fetch("/api/medications"), fetch("/api/medications/adherence")
      ]);
      const [sd, md, ad] = await Promise.all([sr.json(), mr.json(), ar.json()]);
      _allSymp = sd.symptoms;
      _allMeds = md.medications;
      _adherenceData = {{}};
      for (const s of ad.schedules) _adherenceData[s.name] = s;

      if (_allSymp.length < 2 && _allMeds.length === 0) {{
        document.getElementById("no-data").style.display = "block";
        return;
      }}

      // Default range: last 30 days of data (use end_time for multi-day symptoms)
      const dates = [
        ..._allSymp.map(s => (s.end_time || s.timestamp).slice(0, 10)),
        ..._allMeds.map(m => m.timestamp.slice(0, 10)),
      ].sort();
      if (dates.length) {{
        const latest = new Date(dates[dates.length - 1] + "T00:00:00");
        const from30 = new Date(+latest - 29 * 86400000);
        document.getElementById("range-from").value = from30.toISOString().slice(0, 10);
        document.getElementById("range-to").value = dates[dates.length - 1];
      }}

      render();
      renderAdherencePrint();
    }}

    function setPreset(days) {{
      const to = new Date();
      const from = new Date(+to - days * 86400000);
      document.getElementById("range-from").value = from.toISOString().slice(0, 10);
      document.getElementById("range-to").value = to.toISOString().slice(0, 10);
      render();
    }}

    function setPresetAll() {{
      const dates = [
        ..._allSymp.map(s => s.timestamp.slice(0, 10)),
        ..._allSymp.map(s => (s.end_time || s.timestamp).slice(0, 10)),
        ..._allMeds.map(m => m.timestamp.slice(0, 10)),
      ].sort();
      if (dates.length) {{
        document.getElementById("range-from").value = dates[0];
        document.getElementById("range-to").value = dates[dates.length - 1];
      }}
      render();
    }}

    function toggleSmooth() {{
      _smoothed = !_smoothed;
      const btn = document.getElementById("smooth-btn");
      btn.style.background = _smoothed ? "#1e3a8a" : "#fff";
      btn.style.color = _smoothed ? "#fff" : "inherit";
      render();
    }}

    function toggleBucket() {{
      _timeBucket = _timeBucket === "daily" ? "weekly" : "daily";
      const btn = document.getElementById("bucket-btn");
      btn.textContent = _timeBucket === "daily" ? "Daily" : "Weekly";
      btn.style.background = _timeBucket === "daily" ? "#0f766e" : "#fff";
      btn.style.color = _timeBucket === "daily" ? "#fff" : "#0f766e";
      render();
    }}

    function applySmoothing(pts) {{
      return pts.map((pt, i) => {{
        const start = Math.max(0, i - 6);
        const vals = pts.slice(start, i + 1).map(p => p.y);
        return {{ x: pt.x, y: Math.round(vals.reduce((a, b) => a + b, 0) / vals.length * 10) / 10 }};
      }});
    }}

    function renderAdherencePrint() {{
      const el = document.getElementById("adherence-print-section");
      const schedules = Object.values(_adherenceData);
      if (!schedules.length) {{ el.innerHTML = ""; return; }}
      const FREQ = {{"once_daily":"Once daily","twice_daily":"Twice daily","three_daily":"3\u00d7 daily","prn":"As needed (PRN)"}};
      let rows = "";
      for (const s of schedules) {{
        const freq = FREQ[s.frequency] || s.frequency;
        const adh = s.adherence_7d_pct !== null
          ? Math.round(s.adherence_7d_pct) + "%"
          : s.taken_7d + "\u00d7 this week";
        rows += `<tr>
          <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;">${{escHtml(s.name)}}</td>
          <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;">${{escHtml(s.dose || "\u2014")}}</td>
          <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;">${{freq}}</td>
          <td style="padding:5px 8px;border-bottom:1px solid #e5e7eb;font-weight:600;">${{adh}}</td>
        </tr>`;
      }}
      el.innerHTML = `
        <h3 style="font-size:13pt;font-weight:700;margin:0 0 8px;color:#111;">
          Medication Adherence \u2014 Past 7 Days
        </h3>
        <table style="width:100%;border-collapse:collapse;font-size:10pt;">
          <thead><tr style="background:#f3f4f6;">
            <th style="text-align:left;padding:6px 8px;border-bottom:2px solid #d1d5db;">Medication</th>
            <th style="text-align:left;padding:6px 8px;border-bottom:2px solid #d1d5db;">Dose</th>
            <th style="text-align:left;padding:6px 8px;border-bottom:2px solid #d1d5db;">Frequency</th>
            <th style="text-align:left;padding:6px 8px;border-bottom:2px solid #d1d5db;">7-Day Adherence</th>
          </tr></thead>
          <tbody>${{rows}}</tbody>
        </table>`;
    }}

    function printReport() {{
      const from = document.getElementById("range-from").value;
      const to   = document.getElementById("range-to").value;
      document.getElementById("print-date-range").textContent =
        (from ? fmtDate(from) : "\u2014") + " \u2013 " + (to ? fmtDate(to) : "\u2014");
      document.getElementById("print-generated-date").textContent =
        new Date().toLocaleDateString("en-US", {{year:"numeric",month:"long",day:"numeric"}});
      const details = [...document.querySelectorAll("details")];
      const wasOpen = details.map(d => d.open);
      details.forEach(d => {{ d.open = true; }});
      window.onafterprint = () => {{ details.forEach((d, i) => {{ d.open = wasOpen[i]; }}); }};
      window.print();
    }}

    function buildSampleInfo(symptoms) {{
      const datesBySymptom = new Map();
      symptoms.forEach(s => {{
        if (!datesBySymptom.has(s.name)) datesBySymptom.set(s.name, new Set());
        const set = datesBySymptom.get(s.name);
        expandSymptomDates(s).forEach(d => set.add(d));
      }});
      return {{ datesBySymptom }};
    }}

    function pairSampleSize(sampleInfo, nameA, nameB) {{
      const a = sampleInfo.datesBySymptom.get(nameA) || new Set();
      const b = sampleInfo.datesBySymptom.get(nameB) || new Set();
      let n = 0;
      const small = a.size <= b.size ? a : b;
      const large = a.size <= b.size ? b : a;
      small.forEach(d => {{ if (large.has(d)) n += 1; }});
      return n;
    }}

    function render() {{
      const from = document.getElementById("range-from").value;
      const to   = document.getElementById("range-to").value;
      const syms = _allSymp.filter(s => {{
        const start = s.timestamp.slice(0, 10);
        const end   = s.end_time ? s.end_time.slice(0, 10) : start;
        return (!from || end >= from) && (!to || start <= to);
      }});
      const meds = _allMeds.filter(m => {{
        const d = m.timestamp.slice(0, 10);
        return (!from || d >= from) && (!to || d <= to);
      }});
      const sampleInfo = buildSampleInfo(syms);
      renderChart(syms, meds);
      renderCorrelations(from, to, sampleInfo);
      renderMedCorrelations(from, to, sampleInfo);
      renderInsights(from, to);
    }}

    function renderChart(symptoms, medications) {{
      document.getElementById("toggle-bar").innerHTML = "";
      if (_chart) {{ _chart.destroy(); _chart = null; }}

      const hasData = symptoms.length > 0 || medications.length > 0;
      document.getElementById("chart-wrapper").style.display = hasData ? "block" : "none";
      document.getElementById("no-data").style.display = hasData ? "none" : "block";
      if (!hasData) return;

      const allBuckets = new Set();
      symptoms.forEach(s => expandSymptomDates(s).forEach(date => allBuckets.add(bucketKey(date))));
      medications.forEach(m => allBuckets.add(bucketKey(m.timestamp.slice(0, 10))));
      const bucketKeys = [...allBuckets].sort();
      const labels = bucketKeys.map(bucketLabel);
      const labelByKey = new Map(bucketKeys.map(k => [k, bucketLabel(k)]));

      const groups = new Map();
      symptoms.forEach(s => {{
        expandSymptomDates(s).forEach(date => {{
          const b = bucketKey(date);
          if (!groups.has(s.name)) groups.set(s.name, new Map());
          const byDate = groups.get(s.name);
          if (!byDate.has(b)) byDate.set(b, []);
          byDate.get(b).push(s.severity);
        }});
      }});

      const sympCounts = new Map();
      symptoms.forEach(s => sympCounts.set(s.name, (sympCounts.get(s.name) || 0) + 1));
      const topSymptoms = new Set(
        [...sympCounts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 3).map(([n]) => n)
      );

      let i = 0;
      const datasets = [];
      const symptomMeta = [];
      for (const [name, byDate] of groups) {{
        const color = PALETTE[i % PALETTE.length]; i++;
        let pts = [...byDate.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([key, sevs]) => ({{
          x: labelByKey.get(key),
          y: Math.round(sevs.reduce((a, b) => a + b, 0) / sevs.length * 10) / 10,
        }}));
        if (_smoothed) pts = applySmoothing(pts);
        const datasetIndex = datasets.length;
        datasets.push({{
          label: name,
          data: pts,
          yAxisID: "y",
          borderColor: color, backgroundColor: color + "33",
          tension: 0.4, pointRadius: 4, pointHoverRadius: 7,
        }});
        symptomMeta.push({{ label: name, color, datasetIndex }});
      }}

      const medGroups = new Map();
      medications.forEach(m => {{
        if (!medGroups.has(m.name)) medGroups.set(m.name, []);
        medGroups.get(m.name).push(m);
      }});

      // Medication annotations (replaces scatter lane)
      const medAnnotations = {{}};
      const medMeta = []; // {{ name, color, annotationIds }}
      for (const [name, meds] of [...medGroups.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {{
        const color = MED_PALETTE[stableIndex(name, MED_PALETTE.length)];
        const ids = [];
        meds.forEach((m, j) => {{
          const id = `med_${{j}}_${{name}}`;
          ids.push(id);
          const bKey = bucketKey(m.timestamp.slice(0, 10));
          medAnnotations[id] = {{
            type: "line",
            scaleID: "x",
            value: labelByKey.get(bKey),
            borderColor: color,
            borderWidth: 1.5,
            borderDash: [5, 4],
            display: false,
            enter(ctx, event) {{ showMedTooltip(event.native, name, m.dose, m.timestamp.slice(11, 16)); }},
            leave() {{ hideMedTooltip(); }},
          }};
        }});
        medMeta.push({{ name, color, annotationIds: ids }});
      }}

      _chart = new Chart(document.getElementById("symptomChart"), {{
        type: "line",
        data: {{ labels, datasets }},
        options: {{
          responsive: true,
          scales: {{
            x: {{ type: "category", title: {{ display: true, text: "Date (UTC)" }} }},
            y: {{
              min: 1, max: 10,
              ticks: {{ stepSize: 1 }},
              title: {{ display: true, text: "Avg Severity" }},
            }},
          }},
          plugins: {{
            annotation: {{ annotations: medAnnotations }},
            tooltip: {{
              callbacks: {{
                title: (items) => items[0].label,
                label: (item) => `${{item.dataset.label}}: avg severity ${{item.parsed.y}}`,
              }},
            }},
            legend: {{ display: false }},
          }},
        }},
      }});

      buildToggles(_chart, symptomMeta, medMeta, topSymptoms);
    }}

    function buildToggles(chart, symptomMeta, medMeta, topSymptoms) {{
      const bar = document.getElementById("toggle-bar");
      symptomMeta.forEach((info) => {{
        const color = info.color;
        const btn = document.createElement("button");
        const dot = document.createElement("span");
        dot.style.cssText = `width:10px;height:10px;border-radius:50%;background:${{color}};flex-shrink:0;display:inline-block;`;
        btn.appendChild(dot);
        btn.appendChild(document.createTextNode(` ${{info.label}}`));
        btn.style.cssText = `display:inline-flex;align-items:center;gap:5px;padding:4px 12px;`
          + `border-radius:20px;border:1.5px solid ${{color}};background:${{color}}22;`
          + `font-size:13px;cursor:pointer;font-family:inherit;color:#111;transition:opacity .15s;`;
        btn.onclick = () => {{
          const meta = chart.getDatasetMeta(info.datasetIndex);
          meta.hidden = !meta.hidden;
          chart.update();
          const hidden = meta.hidden;
          btn.style.opacity = hidden ? "0.35" : "1";
          btn.style.background = hidden ? "transparent" : `${{color}}22`;
          btn.style.borderColor = hidden ? "#d1d5db" : color;
          btn.style.color = hidden ? "#9ca3af" : "#111";
        }};
        if (!topSymptoms.has(info.label)) {{
          chart.getDatasetMeta(info.datasetIndex).hidden = true;
          btn.style.opacity = "0.35";
          btn.style.background = "transparent";
          btn.style.borderColor = "#d1d5db";
          btn.style.color = "#9ca3af";
        }}
        bar.appendChild(btn);
      }});

      medMeta.forEach(({{ name, color, annotationIds }}) => {{
        const btn = document.createElement("button");
        const icon = document.createElement("span");
        icon.style.cssText = `display:inline-block;width:18px;height:0;border-top:2px dashed ${{color}};vertical-align:middle;flex-shrink:0;`;
        btn.appendChild(icon);
        btn.appendChild(document.createTextNode(` ${{name}}`));
        const adh = _adherenceData[name];
        if (adh) {{
          const badge = document.createElement("span");
          if (adh.adherence_7d_pct !== null) {{
            const pct = Math.round(adh.adherence_7d_pct);
            const bg = pct >= 80 ? "#dcfce7;color:#15803d" : pct >= 50 ? "#fef9c3;color:#92400e" : "#fee2e2;color:#b91c1c";
            badge.textContent = " " + pct + "%";
            badge.style.cssText = "font-size:11px;background:" + bg + ";border-radius:10px;padding:1px 6px;margin-left:4px;font-weight:700;";
          }} else {{
            badge.textContent = " " + adh.taken_7d + "\u00d7";
            badge.style.cssText = "font-size:11px;background:#ede9fe;color:#7c3aed;border-radius:10px;padding:1px 6px;margin-left:4px;font-weight:700;";
          }}
          btn.appendChild(badge);
        }}
        btn.style.cssText = `display:inline-flex;align-items:center;gap:5px;padding:4px 12px;`
          + `border-radius:20px;border:1.5px solid #d1d5db;background:transparent;`
          + `font-size:13px;cursor:pointer;font-family:inherit;color:#9ca3af;transition:opacity .15s;opacity:0.35;`;
        let hidden = true;
        btn.onclick = () => {{
          hidden = !hidden;
          annotationIds.forEach(id => {{
            chart.options.plugins.annotation.annotations[id].display = !hidden;
          }});
          chart.update();
          btn.style.opacity = hidden ? "0.35" : "1";
          btn.style.background = hidden ? "transparent" : `${{color}}22`;
          btn.style.borderColor = hidden ? "#d1d5db" : color;
          btn.style.color = hidden ? "#9ca3af" : "#111";
        }};
        bar.appendChild(btn);
      }});
      chart.update();
    }}

    function describeR(r, isMed) {{
      if (r === null) return "&mdash;";
      const a = Math.abs(r);
      if (a < 0.1) return isMed ? "no clear pattern" : "no clear link";
      if (isMed) {{
        if (r >=  0.5) return "mostly on bad days";
        if (r >=  0.3) return "more on bad days";
        if (r >=  0.1) return "slightly on bad days";
        if (r <= -0.5) return "mostly on good days";
        if (r <= -0.3) return "more on good days";
        return "slightly on good days";
      }} else {{
        if (r >=  0.7) return "very often together";
        if (r >=  0.5) return "often together";
        if (r >=  0.3) return "sometimes together";
        if (r >=  0.1) return "weakly linked";
        if (r <= -0.7) return "almost never together";
        if (r <= -0.5) return "rarely together";
        if (r <= -0.3) return "tend to alternate";
        return "weakly opposite";
      }}
    }}

    async function renderInsights(from, to) {{
      const params = new URLSearchParams();
      if (from) params.set("from_date", from);
      if (to)   params.set("to_date", to);
      const [sr, mr] = await Promise.all([
        fetch(`/api/symptoms/correlations?${{params}}`),
        fetch(`/api/correlations/med-symptom?${{params}}`),
      ]);
      const [sd, md] = await Promise.all([sr.json(), mr.json()]);

      const insights = [];

      // Symptom–symptom pairs (upper triangle only, avoid duplicates)
      const {{ names, matrix: sm }} = sd;
      const sympPairs = [];
      for (let r = 0; r < names.length; r++) {{
        for (let c = r + 1; c < names.length; c++) {{
          const v = sm[r][c];
          if (v !== null && Math.abs(v) >= 0.4) sympPairs.push({{ v, a: names[r], b: names[c] }});
        }}
      }}
      sympPairs.sort((x, y) => Math.abs(y.v) - Math.abs(x.v));
      for (const {{ v, a, b }} of sympPairs.slice(0, 2)) {{
        const pos = v >= 0;
        insights.push({{
          color:  pos ? "#dc2626" : "#2563eb",
          bg:     pos ? "#fef2f2" : "#eff6ff",
          border: pos ? "#fca5a5" : "#bfdbfe",
          text:   pos
            ? `<strong>${{escHtml(a)}}</strong> and <strong>${{escHtml(b)}}</strong> tend to occur together`
            : `<strong>${{escHtml(a)}}</strong> and <strong>${{escHtml(b)}}</strong> tend to alternate`,
          sub: `Symptom pattern &middot; r = ${{v >= 0 ? "+" : ""}}${{v.toFixed(2)}}`,
        }});
      }}

      // Medication–symptom pairs
      const {{ med_names, symp_names, matrix: mm }} = md;
      const medPairs = [];
      for (let r = 0; r < med_names.length; r++) {{
        for (let c = 0; c < symp_names.length; c++) {{
          const v = mm[r][c];
          if (v !== null && Math.abs(v) >= 0.4) medPairs.push({{ v, med: med_names[r], symp: symp_names[c] }});
        }}
      }}
      medPairs.sort((x, y) => Math.abs(y.v) - Math.abs(x.v));
      for (const {{ v, med, symp }} of medPairs.slice(0, 2)) {{
        const pos = v >= 0;
        insights.push({{
          color:  pos ? "#b45309" : "#15803d",
          bg:     pos ? "#fffbeb" : "#f0fdf4",
          border: pos ? "#fde68a" : "#bbf7d0",
          text:   pos
            ? `<strong>${{escHtml(med)}}</strong> is taken more on worse <strong>${{escHtml(symp)}}</strong> days`
            : `<strong>${{escHtml(med)}}</strong> is associated with better <strong>${{escHtml(symp)}}</strong> days`,
          sub: `Medication pattern &middot; r = ${{v >= 0 ? "+" : ""}}${{v.toFixed(2)}}`,
        }});
      }}

      const wrapper = document.getElementById("insights-wrapper");
      if (!insights.length) {{ wrapper.style.display = "none"; return; }}
      wrapper.style.display = "block";
      let html = `<h2 style="margin:0 0 12px; font-size:18px; font-weight:700; color:#111827;">Key Patterns</h2>`;
      html += `<div style="display:flex;flex-direction:column;gap:10px;">`;
      for (const ins of insights) {{
        html += `<div style="padding:10px 14px;background:${{ins.bg}};border:1px solid ${{ins.border}};`
              + `border-left:4px solid ${{ins.color}};border-radius:8px;">`
              + `<div style="font-size:14px;color:#111;line-height:1.5;">${{ins.text}}</div>`
              + `<div style="font-size:11px;color:#6b7280;margin-top:3px;">${{ins.sub}}</div>`
              + `</div>`;
      }}
      html += `</div>`;
      wrapper.innerHTML = html;
    }}

    async function renderCorrelations(from, to, sampleInfo) {{
      const params = new URLSearchParams();
      if (from) params.set("from_date", from);
      if (to)   params.set("to_date", to);
      const resp = await fetch(`/api/symptoms/correlations?${{params}}`);
      const data = await resp.json();

      const corrWrapper = document.getElementById("corr-wrapper");
      if (data.names.length < 2) {{ corrWrapper.style.display = "none"; return; }}
      corrWrapper.style.display = "block";

      const names = data.names, matrix = data.matrix;
      const thStyle = `style="padding:6px 8px; font-size:12px; font-weight:600;
        text-align:center; white-space:nowrap; background:#f5f5f5;"`;
      const rowHeadStyle = `style="padding:6px 10px; font-size:12px; font-weight:600;
        text-align:right; white-space:nowrap; background:#f5f5f5;"`;

      let html = `<table style="border-collapse:collapse; width:100%;">`;
      html += `<thead><tr><th ${{thStyle}}></th>`;
      for (const name of names) html += `<th ${{thStyle}}>${{escHtml(name)}}</th>`;
      html += `</tr></thead><tbody>`;
      for (let r = 0; r < names.length; r++) {{
        html += `<tr><th ${{rowHeadStyle}}>${{escHtml(names[r])}}</th>`;
        for (let c = 0; c < names.length; c++) {{
          const val = matrix[r][c];
          const {{ bg, text }} = corrColor(val);
          const isDiag = r === c;
          const label = isDiag ? "&mdash;" : describeR(val, false);
          const nDays = isDiag ? 0 : pairSampleSize(sampleInfo, names[r], names[c]);
          const cellBg = isDiag ? "#f3f4f6" : bg;
          const cellText = isDiag ? "#9ca3af" : text;
          const rStr = (!isDiag && val !== null) ? `<div style="font-size:10px;opacity:0.75;margin-top:2px;">${{val >= 0 ? "+" : ""}}${{val.toFixed(2)}}</div>` : "";
          const nStr = !isDiag ? `<div style="font-size:10px;opacity:0.65;margin-top:1px;">n=${{nDays}}d</div>` : "";
          const isStrong = !isDiag && val !== null && Math.abs(val) >= 0.5;
          html += `<td style="min-width:80px; padding:6px 6px; text-align:center;
            font-size:12px; font-weight:600; white-space:nowrap; background:${{cellBg}}; color:${{cellText}};${{isStrong ? "outline:2px solid rgba(0,0,0,0.22);outline-offset:-2px;" : ""}}">${{label}}${{rStr}}${{nStr}}</td>`;
        }}
        html += `</tr>`;
      }}
      html += `</tbody></table>`;
      document.getElementById("corr-table").innerHTML = html;
    }}

    async function renderMedCorrelations(from, to, sampleInfo) {{
      const params = new URLSearchParams();
      if (from) params.set("from_date", from);
      if (to)   params.set("to_date", to);
      const resp = await fetch(`/api/correlations/med-symptom?${{params}}`);
      const data = await resp.json();

      const wrapper = document.getElementById("med-corr-wrapper");
      if (!data.med_names.length || !data.symp_names.length) {{ wrapper.style.display = "none"; return; }}
      wrapper.style.display = "block";

      const {{ med_names, symp_names, matrix }} = data;
      const thStyle = `style="padding:6px 8px; font-size:12px; font-weight:600;
        text-align:center; white-space:nowrap; background:#f5f5f5;"`;
      const rowHeadStyle = `style="padding:6px 10px; font-size:12px; font-weight:600;
        text-align:right; white-space:nowrap; background:#f5f5f5;"`;

      let html = `<table style="border-collapse:collapse; width:100%;">`;
      html += `<thead><tr><th ${{thStyle}}></th>`;
      for (const s of symp_names) html += `<th ${{thStyle}}>${{escHtml(s)}}</th>`;
      html += `</tr></thead><tbody>`;
      for (let r = 0; r < med_names.length; r++) {{
        html += `<tr><th ${{rowHeadStyle}}>${{escHtml(med_names[r])}}</th>`;
        for (let c = 0; c < symp_names.length; c++) {{
          const val = matrix[r][c];
          const {{ bg, text }} = corrColor(val);
          const label = describeR(val, true);
          const rStr = val !== null ? `<div style="font-size:10px;opacity:0.75;margin-top:2px;">${{val >= 0 ? "+" : ""}}${{val.toFixed(2)}}</div>` : "";
          const nDays = (sampleInfo.datesBySymptom.get(symp_names[c]) || new Set()).size;
          const nStr = `<div style="font-size:10px;opacity:0.65;margin-top:1px;">n=${{nDays}}d</div>`;
          const isStrong = val !== null && Math.abs(val) >= 0.5;
          html += `<td style="min-width:80px; padding:6px 6px; text-align:center;
            font-size:12px; font-weight:600; white-space:nowrap; background:${{bg}}; color:${{text}};${{isStrong ? "outline:2px solid rgba(0,0,0,0.22);outline-offset:-2px;" : ""}}">${{label}}${{rStr}}${{nStr}}</td>`;
        }}
        html += `</tr>`;
      }}
      html += `</tbody></table>`;
      document.getElementById("med-corr-table").innerHTML = html;
    }}

    init();
  </script>
</body>
</html>
"""


@router.get("/symptoms/calendar", response_class=HTMLResponse)
def symptoms_calendar():
    return """<!DOCTYPE html>
<html>
<head>""" + PAGE_STYLE + """
  <title>Symptom Calendar</title>
  <style>
    .cal-nav { display: flex; align-items: center; justify-content: space-between; margin: 16px 0 8px; }
    .cal-nav button { background: #fff; border: 1px solid #d1d5db; border-radius: 6px;
      padding: 6px 14px; font-size: 18px; cursor: pointer; color: #374151; }
    .cal-nav button:hover { background: #f3f4f6; }
    .cal-month { font-size: 18px; font-weight: 700; color: #111; }
    .cal-grid { width: 100%; border-collapse: collapse; table-layout: fixed; }
    .cal-grid th { padding: 6px 0; text-align: center; font-size: 12px; font-weight: 600;
      color: #6b7280; border-bottom: 2px solid #e5e7eb; }
    .cal-grid td { width: 14.28%; min-height: 72px; height: 72px; vertical-align: top;
      padding: 5px 6px; border: 1px solid #e5e7eb; background: #fff; }
    .cal-grid td.other-month { background: #f9fafb; }
    .cal-grid td.other-month .day-num { color: #d1d5db; }
    .cal-grid td.today { outline: 2px solid #3b82f6; outline-offset: -2px; }
    .cal-grid td.has-data { cursor: pointer; }
    .cal-grid td.has-data:hover { background: #f0f9ff; }
    .cal-grid td.selected { background: #eff6ff; }
    .day-num { font-size: 12px; font-weight: 600; color: #374151; }
    .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-left: 3px;
      vertical-align: middle; }
    .dot.dot-interactive { cursor: pointer; }
    .dot:hover { outline: 2px solid #11182733; outline-offset: 1px; }
    .count { font-size: 11px; color: #6b7280; margin-left: 2px; vertical-align: middle; }
    .cal-legend { display:flex; gap:14px; align-items:center; flex-wrap:wrap; margin: 2px 0 10px; }
    .cal-legend-item { font-size:12px; color:#4b5563; display:flex; align-items:center; gap:6px; }
    .cal-legend-dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
    #cal-loading { color:#6b7280; font-size:14px; margin: 8px 0 12px; }
    #dot-tooltip { display:none; position:fixed; z-index:200; pointer-events:none;
      background:#111827; color:#fff; border-radius:8px; padding:8px 10px;
      font-size:12px; line-height:1.45; max-width:260px;
      box-shadow:0 6px 18px rgba(0,0,0,0.24); }
    #day-detail { display: none; margin-top: 20px; }
    #day-detail h3 { font-size: 16px; margin: 0 0 12px; color: #111; }
    .detail-card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
      padding: 12px 14px; margin-bottom: 10px; }
    .detail-header { display: flex; align-items: center; gap: 10px; }
    .detail-time { font-size: 12px; color: #6b7280; margin-top: 2px; }
    .detail-notes { font-size: 13px; color: #555; margin: 6px 0 0; }
    @media (max-width: 640px) {
      .cal-grid td { height: 52px; min-height: 52px; padding: 3px 4px; }
      .count { font-size: 10px; margin-left: 1px; }
    }
  </style>
</head>
<body>
""" + _nav_bar('calendar') + """
  <div class="container" style="max-width:700px;">
    <h1>Symptom Calendar</h1>
    <div id="cal-loading">Loading calendar...</div>
    <div id="cal-error" class="alert" style="display:none;">
      Unable to load symptom calendar right now.
      <button type="button" onclick="loadData()"
        style="margin-left:8px;background:#fff;border:1px solid #fca5a5;border-radius:6px;padding:4px 8px;
        color:#b91c1c;cursor:pointer;font-family:inherit;">Retry</button>
    </div>
    <div class="cal-nav">
      <button id="prev-btn" onclick="shiftMonth(-1)">&#8592;</button>
      <span class="cal-month" id="month-label"></span>
      <button id="next-btn" onclick="shiftMonth(1)">&#8594;</button>
    </div>
    <div class="cal-legend" aria-hidden="true">
      <span class="cal-legend-item"><span class="cal-legend-dot" style="background:#f97316;"></span>Symptoms</span>
      <span class="cal-legend-item"><span class="cal-legend-dot" style="background:#a855f7;"></span>Medications</span>
      <span class="cal-legend-item"><span style="font-weight:700;">xN</span>entries that day</span>
    </div>
    <table id="cal-table" class="cal-grid" style="display:none;">
      <thead>
        <tr>
          <th>Sun</th><th>Mon</th><th>Tue</th><th>Wed</th><th>Thu</th><th>Fri</th><th>Sat</th>
        </tr>
      </thead>
      <tbody id="cal-body"></tbody>
    </table>
    <div id="day-detail">
      <h3 id="detail-title"></h3>
      <div id="detail-cards"></div>
    </div>
  </div>
  <div id="dot-tooltip"></div>
  <script>
    const MONTHS = ["January","February","March","April","May","June",
                    "July","August","September","October","November","December"];
    function escHtml(v) {
      return String(v)
        .replace(/&/g,"&amp;")
        .replace(/</g,"&lt;")
        .replace(/>/g,"&gt;")
        .replace(/"/g,"&quot;")
        .replace(/'/g,"&#39;");
    }

    function sevColor(s) {
      if (s <= 3) return "#22c55e";
      if (s <= 6) return "#eab308";
      if (s <= 8) return "#f97316";
      return "#ef4444";
    }

    function pad(n) { return String(n).padStart(2, "0"); }
    const dotTip = document.getElementById("dot-tooltip");
    let pinnedDot = null;
    function eventPoint(e) {
      if (e && typeof e.clientX === "number" && typeof e.clientY === "number") {
        return { x: e.clientX, y: e.clientY };
      }
      const t = e && e.currentTarget ? e.currentTarget : null;
      if (t && t.getBoundingClientRect) {
        const r = t.getBoundingClientRect();
        return { x: r.left + (r.width / 2), y: r.top + (r.height / 2) };
      }
      return { x: 40, y: 40 };
    }

    function showDotTip(e, html) {
      if (!html) return;
      dotTip.innerHTML = html;
      dotTip.style.display = "block";
      moveDotTip(e);
    }

    function moveDotTip(e) {
      if (dotTip.style.display !== "block") return;
      const point = eventPoint(e);
      const pad = 14;
      const w = dotTip.offsetWidth || 240;
      const h = dotTip.offsetHeight || 56;
      let left = point.x + pad;
      let top = point.y + pad;
      if (left + w > window.innerWidth - 8) left = point.x - w - pad;
      if (top + h > window.innerHeight - 8) top = point.y - h - pad;
      dotTip.style.left = Math.max(8, left) + "px";
      dotTip.style.top = Math.max(8, top) + "px";
    }

    function hideDotTip() {
      dotTip.style.display = "none";
    }

    function symptomDotTip(entries) {
      const byName = {};
      for (const e of entries) {
        if (!byName[e.name]) byName[e.name] = { count: 0, maxSev: 0 };
        byName[e.name].count += 1;
        byName[e.name].maxSev = Math.max(byName[e.name].maxSev, e.severity);
      }
      const rows = Object.entries(byName)
        .sort((a, b) => b[1].maxSev - a[1].maxSev || b[1].count - a[1].count)
        .slice(0, 3)
        .map(([name, info]) => `${escHtml(name)} (${info.maxSev}/10${info.count > 1 ? `, ×${info.count}` : ""})`);
      const more = Object.keys(byName).length > 3 ? `<br>+${Object.keys(byName).length - 3} more` : "";
      return `<strong>Symptoms</strong><br>${rows.join("<br>")}${more}`;
    }

    function medsDotTip(entries) {
      const names = [...new Set(entries.map(m => m.name))];
      const rows = names.slice(0, 4).map(n => escHtml(n));
      const more = names.length > 4 ? `<br>+${names.length - 4} more` : "";
      return `<strong>Medications</strong><br>${rows.join("<br>")}${more}`;
    }

    function bindDotTip(el, html) {
      el.classList.add("dot-interactive");
      el.setAttribute("role", "button");
      el.setAttribute("tabindex", "0");
      el.setAttribute("aria-label", "Show details");
      el.addEventListener("mouseenter", (ev) => { if (!pinnedDot) showDotTip(ev, html); });
      el.addEventListener("mousemove", (ev) => { if (!pinnedDot) moveDotTip(ev); });
      el.addEventListener("mouseleave", () => { if (!pinnedDot) hideDotTip(); });
      el.addEventListener("focus", (ev) => showDotTip(ev, html));
      el.addEventListener("blur", () => { if (!pinnedDot) hideDotTip(); });
      el.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        if (pinnedDot === el) {
          pinnedDot = null;
          hideDotTip();
          return;
        }
        pinnedDot = el;
        showDotTip(ev, html);
      });
      el.addEventListener("keydown", (ev) => {
        if (ev.key !== "Enter" && ev.key !== " ") return;
        ev.preventDefault();
        ev.stopPropagation();
        if (pinnedDot === el) {
          pinnedDot = null;
          hideDotTip();
        } else {
          pinnedDot = el;
          showDotTip(ev, html);
        }
      });
    }
    document.addEventListener("click", (ev) => {
      if (!pinnedDot) return;
      if (ev.target === pinnedDot) return;
      pinnedDot = null;
      hideDotTip();
    });
    document.addEventListener("keydown", (ev) => {
      if (ev.key !== "Escape" || !pinnedDot) return;
      pinnedDot = null;
      hideDotTip();
    });

    let byDate = {};     // "YYYY-MM-DD" -> [{id,name,severity,notes,timestamp,end_time}]
    let medsByDate = {}; // "YYYY-MM-DD" -> [{id,name,dose,notes,timestamp}]
    let curYear, curMonth, selectedDate = null;

    function expandSymptomDatesCal(s) {
      const start = s.timestamp.slice(0, 10);
      const end   = s.end_time ? s.end_time.slice(0, 10) : start;
      if (start === end) return [start];
      const dates = [];
      let d = new Date(start + "T00:00:00");
      const last = new Date(end + "T00:00:00");
      while (d <= last) {
        dates.push(d.toISOString().slice(0, 10));
        d = new Date(d.getTime() + 86400000);
      }
      return dates;
    }

    function fmtDetailTime(e) {
      const startTime = e.timestamp.slice(11, 16);
      if (!e.end_time) return startTime;
      const startDate = e.timestamp.slice(0, 10);
      const endDate   = e.end_time.slice(0, 10);
      const endTime   = e.end_time.slice(11, 16);
      if (startDate === endDate) return startTime + " \u2013 " + endTime;
      const sd = new Date(startDate + "T00:00:00");
      const ed = new Date(endDate   + "T00:00:00");
      const days = Math.round((ed - sd) / 86400000) + 1;
      const fmt = d => d.toLocaleDateString("en-US", {month: "short", day: "numeric", timeZone: "UTC"});
      return fmt(sd) + " " + startTime + " \u2192 " + fmt(ed) + " " + endTime + " (" + days + " days)";
    }

    async function loadData() {
      const loading = document.getElementById("cal-loading");
      const error = document.getElementById("cal-error");
      const table = document.getElementById("cal-table");
      loading.style.display = "block";
      error.style.display = "none";
      table.style.display = "none";
      document.getElementById("day-detail").style.display = "none";
      pinnedDot = null;
      hideDotTip();
      try {
        const [sympResp, medResp] = await Promise.all([fetch("/api/symptoms"), fetch("/api/medications")]);
        if (!sympResp.ok || !medResp.ok) throw new Error("Calendar API request failed");
        const [sympData, medData] = await Promise.all([sympResp.json(), medResp.json()]);
        byDate = {};
        for (const s of sympData.symptoms) {
          for (const date of expandSymptomDatesCal(s)) {
            if (!byDate[date]) byDate[date] = [];
            byDate[date].push(s);
          }
        }
        medsByDate = {};
        for (const m of medData.medications) {
          const date = m.timestamp.slice(0, 10);
          if (!medsByDate[date]) medsByDate[date] = [];
          medsByDate[date].push(m);
        }
        const now = new Date();
        curYear = now.getFullYear();
        curMonth = now.getMonth();  // 0-indexed
        selectedDate = null;
        table.style.display = "";
        renderCalendar();
        // Default selection: today
        const todayStr = now.getFullYear() + "-" + pad(now.getMonth() + 1) + "-" + pad(now.getDate());
        onDayClick(todayStr);
      } catch (err) {
        console.error(err);
        error.style.display = "block";
      } finally {
        loading.style.display = "none";
      }
    }

    function shiftMonth(delta) {
      pinnedDot = null;
      hideDotTip();
      curMonth += delta;
      if (curMonth > 11) { curMonth = 0; curYear++; }
      if (curMonth < 0)  { curMonth = 11; curYear--; }
      selectedDate = null;
      document.getElementById("day-detail").style.display = "none";
      renderCalendar();
    }

    function renderCalendar() {
      document.getElementById("month-label").textContent = MONTHS[curMonth] + " " + curYear;

      const today = new Date();
      const todayStr = today.getFullYear() + "-" + pad(today.getMonth()+1) + "-" + pad(today.getDate());

      // First day of month (0=Sun), days in month
      const firstDay = new Date(curYear, curMonth, 1).getDay();
      const daysInMonth = new Date(curYear, curMonth + 1, 0).getDate();
      // Days from previous month to fill first row
      const prevMonthDays = new Date(curYear, curMonth, 0).getDate();

      const tbody = document.getElementById("cal-body");
      tbody.innerHTML = "";

      let dayCount = 1;
      let nextCount = 1;

      for (let row = 0; row < 6; row++) {
        if (row > 0 && dayCount > daysInMonth) break;
        const tr = document.createElement("tr");
        for (let col = 0; col < 7; col++) {
          const td = document.createElement("td");
          const cellIndex = row * 7 + col;

          if (cellIndex < firstDay) {
            // Previous month filler
            const d = prevMonthDays - firstDay + cellIndex + 1;
            td.className = "other-month";
            td.innerHTML = `<span class="day-num">${d}</span>`;
          } else if (dayCount > daysInMonth) {
            // Next month filler
            td.className = "other-month";
            td.innerHTML = `<span class="day-num">${nextCount++}</span>`;
          } else {
            const dateStr = curYear + "-" + pad(curMonth + 1) + "-" + pad(dayCount);
            const entries = byDate[dateStr];
            const medEntries = medsByDate[dateStr];
            let classes = "";
            if (dateStr === todayStr) classes += " today";
            if (entries || medEntries) classes += " has-data";
            if (dateStr === selectedDate) classes += " selected";
            td.className = classes.trim();

            let inner = `<span class="day-num">${dayCount}</span>`;
            const hasEntries = !!entries;
            const hasMedEntries = !!medEntries;
            if (entries) {
              const maxSev = Math.max(...entries.map(e => e.severity));
              const color = sevColor(maxSev);
              inner += `<span class="dot" style="background:${color}"></span>`;
              if (entries.length > 1) {
                inner += `<span class="count">×${entries.length}</span>`;
              }
            }
            if (medEntries) {
              inner += `<span class="dot" style="background:#a855f7"></span>`;
              if (medEntries.length > 1) {
                inner += `<span class="count">×${medEntries.length}</span>`;
              }
            }
            if (entries || medEntries) {
              td.setAttribute("data-date", dateStr);
              td.addEventListener("click", () => onDayClick(dateStr));
            }
            td.innerHTML = inner;
            const dots = td.querySelectorAll(".dot");
            if (hasEntries && dots[0]) bindDotTip(dots[0], symptomDotTip(entries));
            if (hasMedEntries && dots[hasEntries ? 1 : 0]) bindDotTip(dots[hasEntries ? 1 : 0], medsDotTip(medEntries));
            dayCount++;
          }
          tr.appendChild(td);
        }
        tbody.appendChild(tr);
      }
    }

    function onDayClick(dateStr) {
      pinnedDot = null;
      hideDotTip();
      const detail = document.getElementById("day-detail");
      if (selectedDate === dateStr) {
        // Toggle off
        selectedDate = null;
        detail.style.display = "none";
        renderCalendar();
        return;
      }
      selectedDate = dateStr;
      renderCalendar();

      const entries = byDate[dateStr] || [];
      const [year, month, day] = dateStr.split("-");
      document.getElementById("detail-title").textContent =
        MONTHS[parseInt(month) - 1] + " " + parseInt(day) + ", " + year;

      const cards = document.getElementById("detail-cards");
      cards.innerHTML = "";
      const medEntries = medsByDate[dateStr] || [];
      for (const m of medEntries) {
        const time = m.timestamp.slice(11, 16);
        const doseHtml = m.dose
          ? `<span style="font-size:12px;color:#7c3aed;margin-top:2px;display:block;">${escHtml(m.dose)}</span>`
          : "";
        const notesHtml = m.notes
          ? `<p class="detail-notes">${escHtml(m.notes)}</p>`
          : "";
        const div = document.createElement("div");
        div.className = "detail-card";
        div.innerHTML = `
          <div class="detail-header">
            <div class="badge" style="background:#a855f7;width:32px;height:32px;font-size:11px;flex-shrink:0;">Rx</div>
            <div>
              <div class="card-name">${escHtml(m.name)}</div>
              ${doseHtml}
              <div class="detail-time">${time}</div>
            </div>
          </div>
          ${notesHtml}
        `;
        cards.appendChild(div);
      }
      for (const e of entries) {
        const time = fmtDetailTime(e);
        const notesHtml = e.notes
          ? `<p class="detail-notes">${escHtml(e.notes)}</p>`
          : "";
        const div = document.createElement("div");
        div.className = "detail-card";
        div.innerHTML = `
          <div class="detail-header">
            <div class="badge" style="background:${sevColor(e.severity)};width:32px;height:32px;font-size:14px;">${e.severity}</div>
            <div>
              <div class="card-name">${escHtml(e.name)}</div>
              <div class="detail-time">${time}</div>
            </div>
          </div>
          ${notesHtml}
        `;
        cards.appendChild(div);
      }
      detail.style.display = "block";
    }

    loadData();
  </script>
</body>
</html>
"""
