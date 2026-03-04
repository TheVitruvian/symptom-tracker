"""
AI engine — wraps the Anthropic SDK.

All public functions return None / empty-generator gracefully when
ANTHROPIC_API_KEY is not configured, so the rest of the app can call
them unconditionally and just hide the UI when _ai_configured() is False.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Generator, Optional

import anthropic
from pydantic import BaseModel

from db import get_db

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_OPUS   = "claude-opus-4-6"
_HAIKU  = "claude-haiku-4-5"
_SUMMARY_TTL_HOURS = 24


def _ai_configured() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def _client() -> anthropic.Anthropic:
    return anthropic.Anthropic()


# ---------------------------------------------------------------------------
# Health-context builder
# ---------------------------------------------------------------------------

def _build_health_context(uid: int, days: int = 30) -> str:
    """Return a compact plain-text block summarising the patient's recent data."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    lines: list[str] = []

    with get_db() as conn:
        # Symptoms
        symp_rows = conn.execute(
            "SELECT name, severity, timestamp FROM symptoms"
            " WHERE user_id = ? AND deleted_at = '' AND timestamp >= ?"
            " ORDER BY timestamp DESC LIMIT 200",
            (uid, cutoff),
        ).fetchall()

        # Active medication schedules
        med_rows = conn.execute(
            "SELECT name, dose, frequency FROM medication_schedules"
            " WHERE user_id = ? AND active = 1 AND paused = 0",
            (uid,),
        ).fetchall()

        # Adherence summary for each active schedule
        dose_stats = conn.execute(
            """SELECT ms.name,
                      COUNT(*) AS total,
                      SUM(CASE WHEN md.status = 'taken' THEN 1 ELSE 0 END) AS taken
               FROM medication_doses md
               JOIN medication_schedules ms ON ms.id = md.schedule_id
               WHERE md.user_id = ? AND md.scheduled_date >= DATE('now', ?)
               GROUP BY ms.id""",
            (uid, f"-{days} days"),
        ).fetchall()

    if not symp_rows and not med_rows:
        return "No health data recorded in this period."

    lines.append(f"Health data summary (last {days} days):")

    if med_rows:
        lines.append("\nCurrent medications:")
        adherence_map = {r["name"]: r for r in dose_stats}
        for m in med_rows:
            stat = adherence_map.get(m["name"])
            if stat and stat["total"] > 0:
                pct = round(stat["taken"] / stat["total"] * 100)
                lines.append(f"  - {m['name']} {m['dose']} ({m['frequency']}): {pct}% adherence")
            else:
                lines.append(f"  - {m['name']} {m['dose']} ({m['frequency']})")

    if symp_rows:
        # Aggregate per symptom: count, avg severity, last occurrence
        from collections import defaultdict
        buckets: dict = defaultdict(list)
        for r in symp_rows:
            buckets[r["name"]].append(r["severity"])
        lines.append("\nSymptoms logged:")
        for name, sevs in sorted(buckets.items(), key=lambda x: -len(x[1])):
            avg = round(sum(sevs) / len(sevs), 1)
            lines.append(f"  - {name}: {len(sevs)} entries, avg severity {avg}/10")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _get_cached_summary(uid: int) -> Optional[str]:
    """Return cached summary if still fresh, else None."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT summary, generated_at FROM ai_insights WHERE user_id = ?", (uid,)
        ).fetchone()
    if not row or not row["summary"]:
        return None
    try:
        gen_dt = datetime.strptime(row["generated_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - gen_dt < timedelta(hours=_SUMMARY_TTL_HOURS):
            return row["summary"]
    except ValueError:
        pass
    return None


def _store_summary(uid: int, text: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO ai_insights (user_id, summary, generated_at) VALUES (?, ?, ?)",
            (uid, text, now),
        )
        conn.commit()


# ---------------------------------------------------------------------------
# Weekly health summary  (Opus, streaming SSE)
# ---------------------------------------------------------------------------

def stream_weekly_summary(uid: int) -> Generator[str, None, None]:
    """
    Yields SSE-formatted strings: `data: <json>\n\n` for each text chunk,
    then `data: [DONE]\n\n`.  Caches the full result to ai_insights.
    Yields an error event on failure.
    """
    if not _ai_configured():
        yield 'data: {"error": "AI not configured"}\n\n'
        return

    context = _build_health_context(uid)
    system = (
        "You are a compassionate health assistant helping a patient understand "
        "their recent symptom and medication data. Write in plain, encouraging language. "
        "Be concise (3-5 short paragraphs). Do not give medical advice or diagnoses. "
        "Highlight positive trends when present. Flag patterns that a doctor might want to know about."
    )
    prompt = (
        f"Please write a weekly health summary for me based on the following data:\n\n"
        f"{context}\n\n"
        "Focus on: overall symptom trends, medication adherence, and any notable patterns."
    )

    full_text: list[str] = []
    try:
        with _client().messages.stream(
            model=_OPUS,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            system=system,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
                full_text.append(text)
                yield f"data: {json.dumps(text)}\n\n"
        _store_summary(uid, "".join(full_text))
    except Exception as exc:  # noqa: BLE001
        yield f'data: {{"error": {json.dumps(str(exc))}}}\n\n'
        return

    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Natural-language symptom parsing  (Haiku, structured output)
# ---------------------------------------------------------------------------

class _ParsedSymptom(BaseModel):
    name: str
    severity: int   # 1-10
    notes: str


def parse_natural_log(text: str) -> Optional[dict]:
    """
    Parse a free-text symptom description into {name, severity, notes}.
    Returns None if AI is not configured or parsing fails.
    """
    if not _ai_configured():
        return None
    try:
        response = _client().messages.parse(
            model=_HAIKU,
            max_tokens=256,
            system=(
                "Extract the symptom name, severity (1-10), and any additional notes "
                "from the user's description. If no explicit severity is given, infer "
                "one from words like 'mild' (2-3), 'moderate' (5-6), 'severe' (8-9). "
                "Keep the name short (1-4 words). Notes should capture context not in the name."
            ),
            messages=[{"role": "user", "content": text}],
            output_format=_ParsedSymptom,
        )
        parsed = response.parsed_output
        severity = max(1, min(10, parsed.severity))
        return {"name": parsed.name.strip(), "severity": severity, "notes": parsed.notes.strip()}
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Physician one-sentence digest  (Haiku)
# ---------------------------------------------------------------------------

def generate_physician_digest(uid: int) -> Optional[str]:
    """
    Return a single clinical sentence summarising the patient's recent status.
    Returns None if AI is not configured or on error.
    """
    if not _ai_configured():
        return None
    context = _build_health_context(uid, days=14)
    try:
        response = _client().messages.create(
            model=_HAIKU,
            max_tokens=120,
            system=(
                "You are a clinical assistant. Write exactly one concise sentence (≤30 words) "
                "summarising the patient's recent symptom trends and medication adherence "
                "for a physician. Use clinical but plain language. No diagnoses."
            ),
            messages=[{"role": "user", "content": context}],
        )
        return response.content[0].text.strip()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Chat  (Opus, streaming SSE)
# ---------------------------------------------------------------------------

def stream_chat_response(uid: int, messages: list) -> Generator[str, None, None]:
    """
    messages: list of {"role": "user"|"assistant", "content": str}
    Yields SSE: `data: <json>\n\n` chunks, then `data: [DONE]\n\n`.
    """
    if not _ai_configured():
        yield 'data: {"error": "AI not configured"}\n\n'
        return

    context = _build_health_context(uid)
    system = (
        "You are a helpful health assistant. The user is asking questions about their "
        "own health data shown below. Answer based only on this data — do not invent "
        "information. Do not give medical diagnoses or prescribe treatments. "
        "Encourage the user to discuss patterns with their doctor.\n\n"
        f"--- Patient data ---\n{context}\n--- End of data ---"
    )
    try:
        with _client().messages.stream(
            model=_OPUS,
            max_tokens=1024,
            thinking={"type": "adaptive"},
            system=system,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                yield f"data: {json.dumps(text)}\n\n"
    except Exception as exc:  # noqa: BLE001
        yield f'data: {{"error": {json.dumps(str(exc))}}}\n\n'
        return

    yield "data: [DONE]\n\n"
