from collections import defaultdict
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
