"""Shared medication helpers used by both medications.py and medications_adherence.py.

Keeping these here prevents a circular import between the two router modules.
"""
import html
from datetime import date, timedelta

from config import FREQ_LABELS, _today_local

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


def _doses_per_day(frequency: str) -> int:
    return {"once_daily": 1, "twice_daily": 2, "three_daily": 3, "prn": 0}.get(frequency, 0)


def _adherence_7d(conn, schedule_id: int, user_id: int, start_date_str: str, frequency: str) -> dict:
    dpd = _doses_per_day(frequency)
    today_local = _today_local()
    if dpd == 0:
        taken = conn.execute(
            "SELECT COUNT(*) FROM medication_doses WHERE schedule_id=? AND user_id=? AND status='taken'"
            " AND scheduled_date >= ?",
            (schedule_id, user_id, (today_local - timedelta(days=6)).isoformat()),
        ).fetchone()[0]
        return {"expected": None, "taken": taken, "pct": None}
    window_start = max(today_local - timedelta(days=6), date.fromisoformat(start_date_str))
    window_end = today_local
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
