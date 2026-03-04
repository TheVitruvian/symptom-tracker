from collections import defaultdict
from datetime import datetime, timedelta
from math import sqrt


def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    cov = sx = sy = 0.0
    for x, y in zip(xs, ys):
        dx, dy = x - mx, y - my
        cov += dx * dy
        sx += dx * dx
        sy += dy * dy
    den = sqrt(sx * sy)
    return round(cov / den, 2) if den != 0 else None


def _linear_slope(xs, ys):
    """Least-squares slope for a series of (x, y) pairs."""
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    den = sum((x - mx) ** 2 for x in xs)
    return num / den if den != 0 else 0.0


def _symptom_trends(entries):
    """
    entries: list of (name: str, severity: float, dt: datetime)
    Returns list of dicts per symptom, sorted by count desc:
      {name, count, avg, recent_avg, prior_avg, trend_pct, trend_dir, slope}
    Uses last 30 days, comparing the most-recent 7-day window vs days 8-30.
    """
    now = datetime.now()
    cutoff_30 = now - timedelta(days=30)
    cutoff_7 = now - timedelta(days=7)
    buckets = defaultdict(lambda: {"recent": [], "prior": [], "days": [], "sevs": []})
    for name, severity, dt in entries:
        if dt < cutoff_30:
            continue
        b = buckets[name]
        b["days"].append((dt - cutoff_30).days)
        b["sevs"].append(severity)
        if dt >= cutoff_7:
            b["recent"].append(severity)
        else:
            b["prior"].append(severity)
    results = []
    for name, b in buckets.items():
        if not b["sevs"]:
            continue
        count = len(b["sevs"])
        avg = round(sum(b["sevs"]) / count, 1)
        recent_avg = round(sum(b["recent"]) / len(b["recent"]), 1) if b["recent"] else None
        prior_avg = round(sum(b["prior"]) / len(b["prior"]), 1) if b["prior"] else None
        trend_pct = None
        trend_dir = "stable"
        if recent_avg is not None and prior_avg is not None and prior_avg > 0:
            trend_pct = round((recent_avg - prior_avg) / prior_avg * 100)
            if trend_pct > 10:
                trend_dir = "up"
            elif trend_pct < -10:
                trend_dir = "down"
        results.append({
            "name": name,
            "count": count,
            "avg": avg,
            "recent_avg": recent_avg,
            "prior_avg": prior_avg,
            "trend_pct": trend_pct,
            "trend_dir": trend_dir,
            "slope": round(_linear_slope(b["days"], b["sevs"]), 3),
        })
    results.sort(key=lambda x: x["count"], reverse=True)
    return results


def _time_patterns(entries):
    """
    entries: list of (name: str, severity: float, dt: datetime)
    Returns {name: {"dow": {day_name: avg}, "tod": {bucket: avg}}}
    Only buckets with >= 3 data points are included.
    """
    _DOW = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    by_dow = defaultdict(lambda: defaultdict(list))
    by_tod = defaultdict(lambda: defaultdict(list))
    for name, severity, dt in entries:
        by_dow[name][_DOW[dt.weekday()]].append(severity)
        hour = dt.hour
        if 6 <= hour < 12:
            tod = "Morning"
        elif 12 <= hour < 18:
            tod = "Afternoon"
        elif 18 <= hour < 22:
            tod = "Evening"
        else:
            tod = "Night"
        by_tod[name][tod].append(severity)
    results = {}
    for name in set(list(by_dow) + list(by_tod)):
        dow_avgs = {
            day: round(sum(vals) / len(vals), 1)
            for day, vals in by_dow[name].items()
            if len(vals) >= 3
        }
        tod_avgs = {
            tod: round(sum(vals) / len(vals), 1)
            for tod, vals in by_tod[name].items()
            if len(vals) >= 3
        }
        if dow_avgs or tod_avgs:
            results[name] = {"dow": dow_avgs, "tod": tod_avgs}
    return results


def _plain_med_correlations(med_names, symp_names, matrix, threshold=0.35):
    """
    Returns up to 5 correlation dicts {symptom, medication, r, sentence}
    for |r| >= threshold, sorted by |r| desc.
    """
    results = []
    for mi, med in enumerate(med_names):
        for si, symp in enumerate(symp_names):
            if mi >= len(matrix) or si >= len(matrix[mi]):
                continue
            r = matrix[mi][si]
            if r is None or abs(r) < threshold:
                continue
            if r < 0:
                sentence = (
                    f"Taking {med} regularly is associated with lower {symp} severity"
                )
            else:
                sentence = (
                    f"Days with more {med} doses correlate with higher {symp} severity"
                )
            results.append({"symptom": symp, "medication": med, "r": r, "sentence": sentence})
    results.sort(key=lambda x: abs(x["r"]), reverse=True)
    return results[:5]


def _compute_correlations(rows):
    # rows are already daily averages: (name, date, avg_severity)
    avg = {}
    dates_by_name = defaultdict(set)
    names_set = set()
    for row in rows:
        name, date, sev = row["name"], row["date"], row["avg_severity"]
        avg[(name, date)] = sev
        dates_by_name[name].add(date)
        names_set.add(name)
    names = sorted(names_set)
    n = len(names)
    matrix: list[list] = [[None] * n for _ in range(n)]
    for k in range(n):
        matrix[k][k] = 1.0
    for i in range(n):
        for j in range(i + 1, n):          # upper triangle only
            a, b = names[i], names[j]
            common = list(dates_by_name[a] & dates_by_name[b])
            r = _pearson([avg[(a, d)] for d in common], [avg[(b, d)] for d in common])
            matrix[i][j] = matrix[j][i] = r  # mirror — exploit symmetry
    return names, matrix
