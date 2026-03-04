import html
import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from ai import _ai_configured, stream_weekly_summary, parse_natural_log, stream_chat_response, _get_cached_summary
from analysis import _symptom_trends, _time_patterns
from config import _current_user_id, _from_utc_storage
from db import get_db
from security import _is_ai_allowed
from ui import PAGE_STYLE, _nav_bar, _severity_color

router = APIRouter()


def _sev_chip(val: float) -> str:
    c = _severity_color(val)
    return (
        f'<span style="display:inline-block;background:{c}22;color:{c};'
        f'border:1px solid {c}66;border-radius:12px;padding:1px 10px;'
        f'font-size:13px;font-weight:700;min-width:34px;text-align:center;">'
        f'{val}</span>'
    )


def _trend_badge(trend_dir: str, trend_pct) -> str:
    if trend_dir == "up":
        pct = f"+{int(trend_pct)}%" if trend_pct is not None else ""
        return f'<span style="color:#ef4444;font-weight:600;font-size:13px;">&#8593; {pct}</span>'
    if trend_dir == "down":
        pct = f"{int(trend_pct)}%" if trend_pct is not None else ""
        return f'<span style="color:#22c55e;font-weight:600;font-size:13px;">&#8595; {pct}</span>'
    return '<span style="color:#9ca3af;font-size:13px;">&#8594; stable</span>'


# ── SSE: stream AI summary ───────────────────────────────────────────────────

@router.get("/insights/summary/stream")
def insights_summary_stream(request: Request):
    ip = request.client.host if request.client else "unknown"
    if not _is_ai_allowed(ip):
        async def _too_many():
            yield 'data: {"error": "Too many requests. Please wait before refreshing."}\n\n'
        return StreamingResponse(_too_many(), media_type="text/event-stream")
    uid = _current_user_id.get()
    return StreamingResponse(
        stream_weekly_summary(uid),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── API: parse natural-language symptom log ──────────────────────────────────

@router.post("/api/insights/parse-natural")
def insights_parse_natural(text: str = Form("")):
    if not text.strip():
        return JSONResponse({"ok": False, "error": "No text provided"}, status_code=400)
    result = parse_natural_log(text.strip())
    if result is None:
        return JSONResponse({"ok": False, "error": "Could not parse symptom description"}, status_code=422)
    return JSONResponse({"ok": True, **result})


# ── API: streaming chat ──────────────────────────────────────────────────────

@router.post("/insights/chat/stream")
async def insights_chat_stream(request: Request):
    ip = request.client.host if request.client else "unknown"
    if not _is_ai_allowed(ip):
        async def _too_many():
            yield 'data: {"error": "Too many requests. Please wait before sending more messages."}\n\n'
        return StreamingResponse(_too_many(), media_type="text/event-stream")
    uid = _current_user_id.get()
    try:
        body = await request.json()
        messages = body.get("messages", [])
    except Exception:
        messages = []
    # Cap history to last 20 messages to limit token spend
    messages = messages[-20:]
    return StreamingResponse(
        stream_chat_response(uid, messages),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Main insights page ───────────────────────────────────────────────────────

@router.get("/insights", response_class=HTMLResponse)
def insights_page():
    uid = _current_user_id.get()
    with get_db() as conn:
        symp_rows = conn.execute(
            "SELECT name, severity, timestamp FROM symptoms"
            " WHERE user_id = ? AND deleted_at = ''",
            (uid,),
        ).fetchall()

    entries = []
    for r in symp_rows:
        try:
            entries.append((r["name"], float(r["severity"]), _from_utc_storage(r["timestamp"])))
        except (ValueError, TypeError):
            pass

    trends = _symptom_trends(entries)
    time_pats = _time_patterns(entries)

    # ── Trends section ───────────────────────────────────────────────────────
    if trends:
        th = (
            'style="padding:8px 12px;text-align:left;font-size:11px;color:#9ca3af;'
            'font-weight:700;text-transform:uppercase;letter-spacing:.05em;'
            'border-bottom:1px solid #f3f4f6;"'
        )
        rows_html = []
        for t in trends:
            name_e = html.escape(t["name"])
            badge = _trend_badge(t["trend_dir"], t["trend_pct"])
            chip = _sev_chip(t["avg"])
            count_str = f'{t["count"]} log{"s" if t["count"] != 1 else ""}'
            recent_note = ""
            if t["recent_avg"] is not None:
                recent_note = (
                    f'<span style="font-size:11px;color:#6b7280;margin-left:8px;">'
                    f'7-day: {_sev_chip(t["recent_avg"])}</span>'
                )
            rows_html.append(
                f'<tr style="border-bottom:1px solid #f9fafb;">'
                f'<td style="padding:10px 12px;font-weight:600;color:#111;">{name_e}</td>'
                f'<td style="padding:10px 12px;text-align:center;">{chip}</td>'
                f'<td style="padding:10px 12px;">{badge}{recent_note}</td>'
                f'<td style="padding:10px 12px;text-align:right;color:#9ca3af;font-size:12px;">'
                f'{count_str}</td>'
                f'</tr>'
            )
        trends_html = (
            '<table style="width:100%;border-collapse:collapse;">'
            '<thead><tr>'
            f'<th {th}>Symptom</th>'
            f'<th {th} style="text-align:center;">30-day avg</th>'
            f'<th {th}>Trend (vs prior 3 weeks)</th>'
            f'<th {th}></th>'
            '</tr></thead><tbody>'
            + "".join(rows_html)
            + '</tbody></table>'
        )
    else:
        trends_html = (
            '<p style="color:#9ca3af;margin:0;font-size:14px;">'
            'No symptom data in the last 30 days.</p>'
        )

    # ── Time patterns section ────────────────────────────────────────────────
    pat_blocks = []
    for symp_name, pats in sorted(time_pats.items()):
        name_e = html.escape(symp_name)
        lines = []
        if pats["dow"]:
            best_day = min(pats["dow"], key=pats["dow"].get)
            worst_day = max(pats["dow"], key=pats["dow"].get)
            if best_day != worst_day:
                bv = pats["dow"][best_day]
                wv = pats["dow"][worst_day]
                lines.append(
                    f'<span style="font-size:13px;color:#6b7280;">'
                    f'Worst day: <strong style="color:#111;">{worst_day}</strong>'
                    f' (avg\u00a0{wv})'
                    f' &nbsp;&middot;&nbsp; '
                    f'Best day: <strong style="color:#111;">{best_day}</strong>'
                    f' (avg\u00a0{bv})</span>'
                )
        if pats["tod"]:
            best_tod = min(pats["tod"], key=pats["tod"].get)
            worst_tod = max(pats["tod"], key=pats["tod"].get)
            if best_tod != worst_tod:
                btv = pats["tod"][best_tod]
                wtv = pats["tod"][worst_tod]
                lines.append(
                    f'<span style="font-size:13px;color:#6b7280;">'
                    f'Worst time: <strong style="color:#111;">{worst_tod}</strong>'
                    f' (avg\u00a0{wtv})'
                    f' &nbsp;&middot;&nbsp; '
                    f'Best time: <strong style="color:#111;">{best_tod}</strong>'
                    f' (avg\u00a0{btv})</span>'
                )
        if lines:
            pat_blocks.append(
                f'<div style="padding:10px 0;border-bottom:1px solid #f3f4f6;">'
                f'<div style="font-weight:600;color:#111;margin-bottom:5px;">{name_e}</div>'
                + "<br>".join(lines)
                + "</div>"
            )

    if pat_blocks:
        pats_html = "".join(pat_blocks)
    else:
        pats_html = (
            '<p style="color:#9ca3af;margin:0;font-size:14px;">'
            'Not enough data yet. At least 3 symptom entries in the same '
            'day-of-week or time-of-day bucket are needed to show patterns.</p>'
        )

    # ── AI section (conditional) ─────────────────────────────────────────────
    ai_html = ""
    if _ai_configured():
        cached = _get_cached_summary(uid)
        cached_json = json.dumps(cached or "")
        ai_html = f"""
  <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px 24px;margin-bottom:20px;">
    <h2 style="font-size:16px;font-weight:700;color:#111;margin:0 0 16px;display:flex;align-items:center;gap:8px;">
      &#129504; AI Health Summary
      <span style="font-size:12px;font-weight:400;color:#9ca3af;">refreshes every 24 hours</span>
    </h2>
    <div id="ai-summary-text" style="font-size:14px;line-height:1.7;color:#374151;white-space:pre-wrap;"></div>
    <div id="ai-summary-loading" style="display:none;font-size:13px;color:#9ca3af;">Generating summary\u2026</div>
    <button id="ai-refresh-btn" onclick="aiRefreshSummary()"
      style="margin-top:12px;background:none;border:1px solid #d1d5db;border-radius:6px;
             padding:5px 14px;font-size:13px;color:#6b7280;cursor:pointer;">
      &#8635; Refresh
    </button>
  </div>

  <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px 24px;margin-bottom:20px;">
    <h2 style="font-size:16px;font-weight:700;color:#111;margin:0 0 16px;display:flex;align-items:center;gap:8px;">
      &#128172; Ask Your Data
    </h2>
    <div id="chat-messages" style="max-height:340px;overflow-y:auto;margin-bottom:12px;"></div>
    <div style="display:flex;gap:8px;">
      <input id="chat-input" type="text" placeholder="Ask about your symptoms or medications\u2026"
        style="flex:1;border:1px solid #d1d5db;border-radius:6px;padding:8px 12px;
               font-size:14px;font-family:inherit;"
        onkeydown="if(event.key==='Enter')sendChat()">
      <button onclick="sendChat()"
        style="background:#4f46e5;color:#fff;border:none;border-radius:6px;
               padding:8px 18px;font-size:14px;cursor:pointer;font-weight:600;">
        Send
      </button>
    </div>
  </div>

  <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px 24px;margin-bottom:20px;">
    <h2 style="font-size:16px;font-weight:700;color:#111;margin:0 0 4px;">&#9997; Log by Voice or Text</h2>
    <p style="font-size:13px;color:#6b7280;margin:0 0 14px;">
      Describe a symptom in plain language and we'll parse the details for you.
    </p>
    <div style="display:flex;gap:8px;margin-bottom:8px;">
      <input id="nl-input" type="text" placeholder="e.g. bad headache on the right side, maybe a 7"
        style="flex:1;border:1px solid #d1d5db;border-radius:6px;padding:8px 12px;
               font-size:14px;font-family:inherit;"
        onkeydown="if(event.key==='Enter')parseNL()">
      <button onclick="parseNL()"
        style="background:#0891b2;color:#fff;border:none;border-radius:6px;
               padding:8px 18px;font-size:14px;cursor:pointer;font-weight:600;">
        Parse
      </button>
    </div>
    <div id="nl-result" style="display:none;background:#f0fdf4;border:1px solid #86efac;
         border-radius:8px;padding:12px 16px;font-size:14px;"></div>
    <div id="nl-error" style="display:none;background:#fee2e2;border:1px solid #fca5a5;
         border-radius:8px;padding:10px 14px;font-size:14px;color:#b91c1c;"></div>
  </div>

  <script>
  (function() {{
    var cached = {cached_json};
    var chatHistory = [];

    // ── AI Summary ──────────────────────────────────────────────────────────
    function showSummary(text) {{
      document.getElementById('ai-summary-text').textContent = text;
      document.getElementById('ai-summary-loading').style.display = 'none';
      document.getElementById('ai-refresh-btn').style.display = '';
    }}

    function aiRefreshSummary() {{
      document.getElementById('ai-summary-text').textContent = '';
      document.getElementById('ai-summary-loading').style.display = 'block';
      document.getElementById('ai-refresh-btn').style.display = 'none';
      var es = new EventSource('/insights/summary/stream');
      var buf = '';
      es.onmessage = function(e) {{
        if (e.data === '[DONE]') {{ es.close(); showSummary(buf); return; }}
        try {{
          var chunk = JSON.parse(e.data);
          if (chunk.error) {{ es.close(); showSummary('Error: ' + chunk.error); return; }}
          buf += chunk;
          document.getElementById('ai-summary-text').textContent = buf;
        }} catch(ex) {{}}
      }};
      es.onerror = function() {{ es.close(); showSummary('Failed to load summary. Please try again.'); }};
    }}

    // Load on page open: use cache if available, otherwise stream
    if (cached) {{
      showSummary(cached);
    }} else {{
      aiRefreshSummary();
    }}
    window.aiRefreshSummary = aiRefreshSummary;

    // ── Chat ────────────────────────────────────────────────────────────────
    function appendChatMsg(role, text) {{
      var box = document.getElementById('chat-messages');
      var div = document.createElement('div');
      div.style.cssText = 'margin-bottom:10px;' + (role === 'user'
        ? 'text-align:right;'
        : 'text-align:left;');
      var bubble = document.createElement('span');
      bubble.style.cssText = 'display:inline-block;max-width:80%;padding:8px 12px;border-radius:10px;'
        + 'font-size:13px;line-height:1.5;white-space:pre-wrap;'
        + (role === 'user'
          ? 'background:#4f46e5;color:#fff;border-bottom-right-radius:2px;'
          : 'background:#f3f4f6;color:#111;border-bottom-left-radius:2px;');
      bubble.textContent = text;
      div.appendChild(bubble);
      box.appendChild(div);
      box.scrollTop = box.scrollHeight;
      return bubble;
    }}

    window.sendChat = function() {{
      var input = document.getElementById('chat-input');
      var text = input.value.trim();
      if (!text) return;
      input.value = '';
      chatHistory.push({{role: 'user', content: text}});
      appendChatMsg('user', text);
      var bubble = appendChatMsg('assistant', '');
      var buf = '';

      fetch('/insights/chat/stream', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{messages: chatHistory}})
      }}).then(function(res) {{
        var reader = res.body.getReader();
        var decoder = new TextDecoder();
        var leftover = '';
        function pump() {{
          reader.read().then(function(result) {{
            if (result.done) {{
              chatHistory.push({{role: 'assistant', content: buf}});
              return;
            }}
            leftover += decoder.decode(result.value, {{stream: true}});
            var parts = leftover.split('\\n\\n');
            leftover = parts.pop();
            parts.forEach(function(part) {{
              if (!part.startsWith('data: ')) return;
              var payload = part.slice(6);
              if (payload === '[DONE]') return;
              try {{
                var chunk = JSON.parse(payload);
                if (!chunk.error) {{ buf += chunk; bubble.textContent = buf; }}
              }} catch(ex) {{}}
            }});
            var box = document.getElementById('chat-messages');
            box.scrollTop = box.scrollHeight;
            pump();
          }});
        }}
        pump();
      }}).catch(function() {{
        bubble.textContent = 'Network error. Please try again.';
      }});
    }};

    // ── Natural-language log ────────────────────────────────────────────────
    window.parseNL = function() {{
      var input = document.getElementById('nl-input');
      var text = input.value.trim();
      var resEl = document.getElementById('nl-result');
      var errEl = document.getElementById('nl-error');
      resEl.style.display = 'none';
      errEl.style.display = 'none';
      if (!text) return;

      var fd = new FormData();
      fd.append('text', text);
      fetch('/api/insights/parse-natural', {{
        method: 'POST',
        headers: {{'X-CSRF-Token': (document.cookie.split('; ').find(function(c){{return c.startsWith('csrf_token=');}}) || '').split('=')[1] || ''}},
        body: fd
      }}).then(function(r) {{ return r.json(); }}).then(function(data) {{
        if (!data.ok) {{
          errEl.textContent = data.error || 'Parse failed';
          errEl.style.display = 'block';
          return;
        }}
        resEl.innerHTML = '<strong>' + data.name + '</strong>'
          + ' &mdash; severity <strong>' + data.severity + '/10</strong>'
          + (data.notes ? ' &mdash; <em>' + data.notes + '</em>' : '')
          + '<br><a href="/symptoms/add?name=' + encodeURIComponent(data.name)
          + '&severity=' + data.severity
          + '&notes=' + encodeURIComponent(data.notes || '')
          + '" style="font-size:13px;color:#059669;font-weight:600;">'
          + '&rarr; Log this symptom</a>';
        resEl.style.display = 'block';
        input.value = '';
      }}).catch(function() {{
        errEl.textContent = 'Network error. Please try again.';
        errEl.style.display = 'block';
      }});
    }};
  }})();
  </script>
"""

    # ── Page layout ──────────────────────────────────────────────────────────
    card = (
        'style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;'
        'padding:20px 24px;margin-bottom:20px;"'
    )
    h2 = (
        'style="font-size:16px;font-weight:700;color:#111;margin:0 0 16px;'
        'display:flex;align-items:center;gap:8px;"'
    )
    body = f"""
<div style="max-width:820px;margin:32px auto;padding:0 16px 48px;">
  <h1 style="font-size:22px;font-weight:800;color:#111;margin:0 0 24px;">Insights</h1>
  {ai_html}
  <div {card}>
    <h2 {h2}>&#128200; Symptom Trends
      <span style="font-size:12px;font-weight:400;color:#9ca3af;">last 30 days</span>
    </h2>
    {trends_html}
  </div>
  <div {card}>
    <h2 {h2}>&#128336; Time Patterns</h2>
    {pats_html}
  </div>
</div>
"""
    return PAGE_STYLE + _nav_bar("insights") + body
