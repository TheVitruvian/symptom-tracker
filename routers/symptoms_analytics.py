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
    return f"""<!DOCTYPE html>
<html>
<head>
  {PAGE_STYLE}
  <title>Symptom Chart</title>
</head>
<body>
  {_nav_bar('chart')}
  <div class="container" style="max-width:860px;">
    <h1>Symptom Chart</h1>

    <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-top:12px;">
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
      <div style="display:flex; gap:4px;">
        <button onclick="setPreset(7)"  style="border:1px solid #d1d5db; background:#fff; border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; font-family:inherit;">7d</button>
        <button onclick="setPreset(30)" style="border:1px solid #d1d5db; background:#fff; border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; font-family:inherit;">30d</button>
        <button onclick="setPreset(90)" style="border:1px solid #d1d5db; background:#fff; border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; font-family:inherit;">90d</button>
        <button onclick="setPresetAll()" style="border:1px solid #d1d5db; background:#fff; border-radius:6px; padding:5px 10px; font-size:13px; cursor:pointer; font-family:inherit;">All</button>
      </div>
    </div>

    <div id="no-data" class="empty" style="display:none; margin-top:24px;">
      Not enough data yet &mdash; log at least 2 symptoms first.
    </div>

    <div id="chart-wrapper" class="card" style="display:none; margin-top:16px; padding:24px;">
      <div id="toggle-bar" style="display:flex; flex-wrap:wrap; gap:6px; margin-bottom:16px;"></div>
      <canvas id="symptomChart"></canvas>
    </div>

    <div id="corr-wrapper" style="display:none; margin-top:28px;">
      <h2 style="font-size:18px; margin-bottom:4px;">Symptom Correlations</h2>
      <p style="font-size:13px; color:#666; margin:0 0 12px;">
        Pearson r between symptom severities, averaged by day.
        Requires &ge;3 shared days per pair. Red&nbsp;=&nbsp;positive, blue&nbsp;=&nbsp;negative.
      </p>
      <div id="corr-table" style="overflow-x:auto;"></div>
    </div>

    <div id="med-corr-wrapper" style="display:none; margin-top:28px;">
      <h2 style="font-size:18px; margin-bottom:4px;">Medication &ndash; Symptom Correlations</h2>
      <p style="font-size:13px; color:#666; margin:0 0 12px;">
        Pearson r between daily medication doses and symptom severity.
        Positive (red) = medication often taken on worse days; negative (blue) = associated with lower severity.
        Requires &ge;3 symptom days with at least one dose.
      </p>
      <div id="med-corr-table" style="overflow-x:auto;"></div>
    </div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
  <script>
    const PALETTE = [
      "#3b82f6","#ef4444","#22c55e","#f97316","#a855f7",
      "#06b6d4","#eab308","#ec4899","#14b8a6","#f43f5e","#8b5cf6","#84cc16"
    ];
    const MED_PALETTE = ["#7c3aed","#9333ea","#a855f7","#6d28d9","#c026d3","#0ea5e9","#0f766e","#b45309"];
    const MED_SHAPES = ["triangle","rectRot","star","crossRot","rect","circle"];
    const MED_SYMBOLS = {{
      triangle: "&#9650;",   // ▲
      rectRot: "&#9670;",    // ◆
      star: "&#9733;",       // ★
      crossRot: "&#10006;",  // ✖
      rect: "&#9632;",       // ■
      circle: "&#9679;",     // ●
    }};
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

    let _allSymp = [], _allMeds = [], _chart = null;

    async function init() {{
      const [sr, mr] = await Promise.all([fetch("/api/symptoms"), fetch("/api/medications")]);
      const [sd, md] = await Promise.all([sr.json(), mr.json()]);
      _allSymp = sd.symptoms;
      _allMeds = md.medications;

      if (_allSymp.length < 2 && _allMeds.length === 0) {{
        document.getElementById("no-data").style.display = "block";
        return;
      }}

      // Default range: last 30 days of data
      const dates = [
        ..._allSymp.map(s => s.timestamp.slice(0, 10)),
        ..._allMeds.map(m => m.timestamp.slice(0, 10)),
      ].sort();
      if (dates.length) {{
        const latest = new Date(dates[dates.length - 1] + "T00:00:00");
        const from30 = new Date(+latest - 29 * 86400000);
        document.getElementById("range-from").value = from30.toISOString().slice(0, 10);
        document.getElementById("range-to").value = dates[dates.length - 1];
      }}

      render();
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
        ..._allMeds.map(m => m.timestamp.slice(0, 10)),
      ].sort();
      if (dates.length) {{
        document.getElementById("range-from").value = dates[0];
        document.getElementById("range-to").value = dates[dates.length - 1];
      }}
      render();
    }}

    function render() {{
      const from = document.getElementById("range-from").value;
      const to   = document.getElementById("range-to").value;
      const syms = _allSymp.filter(s => {{
        const d = s.timestamp.slice(0, 10);
        return (!from || d >= from) && (!to || d <= to);
      }});
      const meds = _allMeds.filter(m => {{
        const d = m.timestamp.slice(0, 10);
        return (!from || d >= from) && (!to || d <= to);
      }});
      renderChart(syms, meds);
      renderCorrelations(from, to);
      renderMedCorrelations(from, to);
    }}

    function renderChart(symptoms, medications) {{
      document.getElementById("toggle-bar").innerHTML = "";
      if (_chart) {{ _chart.destroy(); _chart = null; }}

      const hasData = symptoms.length > 0 || medications.length > 0;
      document.getElementById("chart-wrapper").style.display = hasData ? "block" : "none";
      document.getElementById("no-data").style.display = hasData ? "none" : "block";
      if (!hasData) return;

      const allDates = new Set();
      symptoms.forEach(s => allDates.add(s.timestamp.slice(0, 10)));
      medications.forEach(m => allDates.add(m.timestamp.slice(0, 10)));
      const labels = [...allDates].sort().map(d => fmtDate(d));

      const groups = new Map();
      symptoms.forEach(s => {{
        const date = s.timestamp.slice(0, 10);
        if (!groups.has(s.name)) groups.set(s.name, new Map());
        const byDate = groups.get(s.name);
        if (!byDate.has(date)) byDate.set(date, []);
        byDate.get(date).push(s.severity);
      }});

      let i = 0;
      const datasets = [];
      for (const [name, byDate] of groups) {{
        const color = PALETTE[i % PALETTE.length]; i++;
        datasets.push({{
          label: name,
          data: [...byDate.entries()].sort(([a], [b]) => a.localeCompare(b)).map(([date, sevs]) => ({{
            x: fmtDate(date),
            y: Math.round(sevs.reduce((a, b) => a + b, 0) / sevs.length * 10) / 10,
          }})),
          borderColor: color, backgroundColor: color + "33",
          tension: 0.4, pointRadius: 4, pointHoverRadius: 7,
        }});
      }}

      const medGroups = new Map();
      medications.forEach(m => {{
        if (!medGroups.has(m.name)) medGroups.set(m.name, []);
        medGroups.get(m.name).push(m);
      }});
      let lane = 0;
      for (const [name, meds] of [...medGroups.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {{
        const color = MED_PALETTE[stableIndex(name, MED_PALETTE.length)];
        const shape = MED_SHAPES[stableIndex(name + "::shape", MED_SHAPES.length)];
        const yLane = 0.12 + lane * 0.14;
        lane += 1;
        datasets.push({{
          type: "scatter", label: name, _isMed: true, _medSymbol: MED_SYMBOLS[shape],
          data: meds.map(m => ({{
            x: fmtDate(m.timestamp.slice(0, 10)), y: yLane,
            _dose: m.dose, _time: m.timestamp.slice(11, 16),
          }})),
          // Use high-contrast fill + dark outline for crisper medication markers.
          backgroundColor: "#ffffff",
          borderColor: color,
          pointStyle: shape,
          pointRadius: 9,
          pointHoverRadius: 11,
          pointBorderWidth: 2.6,
          pointHoverBorderWidth: 3.2,
          pointHoverBackgroundColor: "#ffffff",
          pointHoverBorderColor: "#4c1d95",
        }});
      }}

      _chart = new Chart(document.getElementById("symptomChart"), {{
        type: "line",
        data: {{ labels, datasets }},
        options: {{
          responsive: true,
          scales: {{
            x: {{ type: "category", title: {{ display: true, text: "Date (UTC)" }} }},
            y: {{
              min: 0, max: 10,
              ticks: {{ stepSize: 1, callback: (val) => val === 0 ? "Rx" : val }},
              title: {{ display: true, text: "Avg Severity" }},
            }},
          }},
          plugins: {{
            tooltip: {{
              callbacks: {{
                title: (items) => items[0].dataset.label,
                label: (item) => {{
                  if (item.dataset._isMed) {{
                    const d = item.raw;
                    return d._dose ? `${{d._time}} — ${{d._dose}}` : `Taken at ${{d._time}}`;
                  }}
                  return `Avg severity: ${{item.parsed.y}} on ${{item.label}}`;
                }},
              }},
            }},
            legend: {{ display: false }},
          }},
        }},
      }});

      buildToggles(_chart, datasets);
    }}

    function buildToggles(chart, datasets) {{
      const bar = document.getElementById("toggle-bar");
      datasets.forEach((ds, i) => {{
        const color = ds.borderColor || ds.backgroundColor;
        const isMed = !!ds._isMed;
        const btn = document.createElement("button");
        if (isMed) {{
          const icon = document.createElement("span");
          icon.style.cssText = `font-size:10px;color:${{color}};line-height:1;`;
          icon.innerHTML = ds._medSymbol || "&#9650;";
          btn.appendChild(icon);
        }} else {{
          const dot = document.createElement("span");
          dot.style.cssText = `width:10px;height:10px;border-radius:50%;background:${{color}};flex-shrink:0;display:inline-block;`;
          btn.appendChild(dot);
        }}
        btn.appendChild(document.createTextNode(` ${{ds.label}}`));
        btn.style.cssText = `display:inline-flex;align-items:center;gap:5px;padding:4px 12px;`
          + `border-radius:20px;border:1.5px solid ${{color}};background:${{color}}22;`
          + `font-size:13px;cursor:pointer;font-family:inherit;color:#111;transition:opacity .15s;`;
        btn.onclick = () => {{
          const meta = chart.getDatasetMeta(i);
          meta.hidden = !meta.hidden;
          chart.update();
          const hidden = meta.hidden;
          btn.style.opacity = hidden ? "0.35" : "1";
          btn.style.background = hidden ? "transparent" : `${{color}}22`;
          btn.style.borderColor = hidden ? "#d1d5db" : color;
          btn.style.color = hidden ? "#9ca3af" : "#111";
        }};
        bar.appendChild(btn);
      }});
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

    async function renderCorrelations(from, to) {{
      const params = new URLSearchParams();
      if (from) params.set("from_date", from);
      if (to)   params.set("to_date", to);
      const resp = await fetch(`/api/symptoms/correlations?${{params}}`);
      const data = await resp.json();

      const corrWrapper = document.getElementById("corr-wrapper");
      if (data.names.length < 2) {{ corrWrapper.style.display = "none"; return; }}
      corrWrapper.style.display = "block";

      const names = data.names, matrix = data.matrix;
      const thStyle = `style="padding:8px 10px; font-size:13px; font-weight:600;
        text-align:center; white-space:nowrap; background:#f5f5f5;"`;
      const rowHeadStyle = `style="padding:8px 12px; font-size:13px; font-weight:600;
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
          const title = isDiag || val === null ? "" : ` title="r = ${{val >= 0 ? "+" : ""}}${{val.toFixed(2)}}"`;
          const cellBg = isDiag ? "#f3f4f6" : bg;
          const cellText = isDiag ? "#9ca3af" : text;
          html += `<td${{title}} style="min-width:110px; padding:9px 8px; text-align:center;
            font-size:12px; font-weight:600; white-space:nowrap; background:${{cellBg}}; color:${{cellText}}">${{label}}</td>`;
        }}
        html += `</tr>`;
      }}
      html += `</tbody></table>`;
      document.getElementById("corr-table").innerHTML = html;
    }}

    async function renderMedCorrelations(from, to) {{
      const params = new URLSearchParams();
      if (from) params.set("from_date", from);
      if (to)   params.set("to_date", to);
      const resp = await fetch(`/api/correlations/med-symptom?${{params}}`);
      const data = await resp.json();

      const wrapper = document.getElementById("med-corr-wrapper");
      if (!data.med_names.length || !data.symp_names.length) {{ wrapper.style.display = "none"; return; }}
      wrapper.style.display = "block";

      const {{ med_names, symp_names, matrix }} = data;
      const thStyle = `style="padding:8px 10px; font-size:13px; font-weight:600;
        text-align:center; white-space:nowrap; background:#f5f5f5;"`;
      const rowHeadStyle = `style="padding:8px 12px; font-size:13px; font-weight:600;
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
          const title = val === null ? "" : ` title="r = ${{val >= 0 ? "+" : ""}}${{val.toFixed(2)}}"`;
          html += `<td${{title}} style="min-width:110px; padding:9px 8px; text-align:center;
            font-size:12px; font-weight:600; white-space:nowrap; background:${{bg}}; color:${{text}}">${{label}}</td>`;
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
    .count { font-size: 11px; color: #6b7280; margin-left: 2px; vertical-align: middle; }
    #day-detail { display: none; margin-top: 20px; }
    #day-detail h3 { font-size: 16px; margin: 0 0 12px; color: #111; }
    .detail-card { background: #fff; border: 1px solid #e5e7eb; border-radius: 8px;
      padding: 12px 14px; margin-bottom: 10px; }
    .detail-header { display: flex; align-items: center; gap: 10px; }
    .detail-time { font-size: 12px; color: #6b7280; margin-top: 2px; }
    .detail-notes { font-size: 13px; color: #555; margin: 6px 0 0; }
    @media (max-width: 640px) {
      .cal-grid td { height: 52px; min-height: 52px; padding: 3px 4px; }
      .count { display: none; }
    }
  </style>
</head>
<body>
""" + _nav_bar('calendar') + """
  <div class="container" style="max-width:700px;">
    <h1>Symptom Calendar</h1>
    <div class="cal-nav">
      <button id="prev-btn" onclick="shiftMonth(-1)">&#8592;</button>
      <span class="cal-month" id="month-label"></span>
      <button id="next-btn" onclick="shiftMonth(1)">&#8594;</button>
    </div>
    <table class="cal-grid">
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

    let byDate = {};     // "YYYY-MM-DD" -> [{id,name,severity,notes,timestamp}]
    let medsByDate = {}; // "YYYY-MM-DD" -> [{id,name,dose,notes,timestamp}]
    let curYear, curMonth, selectedDate = null;

    async function loadData() {
      const [sympResp, medResp] = await Promise.all([fetch("/api/symptoms"), fetch("/api/medications")]);
      const [sympData, medData] = await Promise.all([sympResp.json(), medResp.json()]);
      byDate = {};
      for (const s of sympData.symptoms) {
        const date = s.timestamp.slice(0, 10);
        if (!byDate[date]) byDate[date] = [];
        byDate[date].push(s);
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
      renderCalendar();
    }

    function shiftMonth(delta) {
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
            dayCount++;
          }
          tr.appendChild(td);
        }
        tbody.appendChild(tr);
      }
    }

    function onDayClick(dateStr) {
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
        const time = e.timestamp.slice(11, 16);  // HH:MM
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
