"""Quality metrics calculated from daily database summaries.

Keep statistical calculations independent of HTTP handlers and database access.
The input size depends on reporting days and defect categories, not audit count.
"""
from __future__ import annotations

import math
from statistics import NormalDist
from typing import Any


def sigma_level(dpmo: float) -> float:
    if dpmo <= 0:
        return 6.0
    yield_rate = min(0.999999999, max(0.000000001, 1 - dpmo / 1_000_000))
    return max(0.0, min(6.0, NormalDist().inv_cdf(yield_rate) + 1.5))


def summarize_quality(days: list[dict[str, Any]], categories: list[dict[str, Any]]) -> dict[str, Any]:
    units = sum(day["n"] for day in days)
    defect_count = sum(day["defects"] for day in days)
    defective = sum(day["defective"] for day in days)
    opportunities = sum(day["opportunities"] for day in days)
    critical = sum(day["critical"] for day in days)
    pbar = defective / units if units else 0
    ubar = defect_count / units if units else 0
    dpmo = defect_count / opportunities * 1_000_000 if opportunities else 0
    chart = []
    for day in days:
        n = day["n"]
        se = math.sqrt(pbar * (1 - pbar) / n) if pbar else 0
        use = math.sqrt(ubar / n) if ubar else 0
        chart.append({
            "label": day["label"], "n": n, "defective": day["defective"], "defects": day["defects"],
            "p": day["defective"] / n, "p_cl": pbar,
            "p_ucl": min(1, pbar + 3 * se), "p_lcl": max(0, pbar - 3 * se),
            "u": day["defects"] / n, "u_cl": ubar,
            "u_ucl": ubar + 3 * use, "u_lcl": max(0, ubar - 3 * use),
        })
    pareto, cumulative = [], 0
    for category in sorted(categories, key=lambda row: (-row["count"], row["category"].casefold())):
        count = category["count"]
        cumulative += count
        pareto.append({
            "category": category["category"], "count": count,
            "percent": count / defect_count * 100 if defect_count else 0,
            "cumulative_percent": cumulative / defect_count * 100 if defect_count else 0,
        })
    stability = "stable" if len(chart) >= 20 else "provisional" if len(chart) >= 5 else "insufficient"
    return {
        "metrics": {
            "units": units, "defects": defect_count, "defective": defective,
            "yield": round((units - defective) / units * 100, 2) if units else 0,
            "dpu": round(ubar, 4), "dpmo": round(dpmo, 0), "sigma": round(sigma_level(dpmo), 2),
            "critical_defects": critical, "opportunities": opportunities,
        },
        "control_chart": chart, "stability": stability, "pareto": pareto,
    }
