"""Derived metrics shared by the dashboard, the insights prompt and Claude's chart tool."""
import math
import sqlite3
import statistics
from datetime import date, timedelta

# Whitelisted daily series. Claude's chart specs may only reference these keys.
METRICS: dict[str, dict] = {
    "resting_hr":        {"label": "Resting HR", "unit": "bpm"},
    "hrv_avg":           {"label": "Overnight HRV", "unit": "ms"},
    "sleep_h":           {"label": "Sleep", "unit": "h"},
    "sleep_score":       {"label": "Sleep score", "unit": ""},
    "stress_avg":        {"label": "Avg stress", "unit": ""},
    "steps":             {"label": "Steps", "unit": ""},
    "body_battery_high": {"label": "Body Battery high", "unit": ""},
    "body_battery_low":  {"label": "Body Battery low", "unit": ""},
    "vo2max":            {"label": "VO2 max", "unit": "ml/kg/min"},
    "load":              {"label": "Training load", "unit": "TRIMP"},
    "distance_km":       {"label": "Distance", "unit": "km"},
    "duration_min":      {"label": "Training time", "unit": "min"},
    "ctl":               {"label": "Fitness (CTL)", "unit": ""},
    "atl":               {"label": "Fatigue (ATL)", "unit": ""},
    "tsb":               {"label": "Form (TSB)", "unit": ""},
}


def _activity_load(a: sqlite3.Row, rest_hr: float, max_hr: float) -> float:
    """Banister TRIMP from average HR; falls back to a duration estimate."""
    minutes = (a["duration_s"] or 0) / 60
    if a["avg_hr"] and max_hr > rest_hr:
        ratio = min(max((a["avg_hr"] - rest_hr) / (max_hr - rest_hr), 0), 1)
        return minutes * ratio * 0.64 * math.exp(1.92 * ratio)
    return minutes * 0.6


def daily_frame(conn: sqlite3.Connection, days: int = 180, end: date | None = None) -> list[dict]:
    """One dict per calendar day with wellness + training metrics, oldest first.

    CTL/ATL are warmed up over an extra 84 days so the first visible values are meaningful.
    """
    end = end or _last_data_day(conn)
    warmup = 84
    start = end - timedelta(days=days + warmup - 1)

    wellness = {
        r["date"]: dict(r)
        for r in conn.execute("SELECT * FROM daily WHERE date BETWEEN ? AND ?", (start.isoformat(), end.isoformat()))
    }
    acts = conn.execute(
        "SELECT * FROM activities WHERE substr(start_time,1,10) BETWEEN ? AND ?",
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    rests = [w["resting_hr"] for w in wellness.values() if w.get("resting_hr")]
    rest_hr = statistics.median(rests) if rests else 55.0
    max_hr = max([a["max_hr"] for a in acts if a["max_hr"]] or [190.0])

    per_day: dict[str, dict] = {}
    for a in acts:
        d = per_day.setdefault(a["start_time"][:10], {"load": 0.0, "distance_km": 0.0, "duration_min": 0.0})
        d["load"] += _activity_load(a, rest_hr, max_hr)
        d["distance_km"] += (a["distance_m"] or 0) / 1000
        d["duration_min"] += (a["duration_s"] or 0) / 60

    out, ctl, atl = [], 0.0, 0.0
    for i in range(days + warmup):
        day = (start + timedelta(days=i)).isoformat()
        w = wellness.get(day, {})
        t = per_day.get(day, {"load": 0.0, "distance_km": 0.0, "duration_min": 0.0})
        tsb = ctl - atl  # form going into the day
        ctl += (t["load"] - ctl) / 42
        atl += (t["load"] - atl) / 7
        if i < warmup:
            continue
        out.append({
            "date": day,
            "resting_hr": w.get("resting_hr"),
            "hrv_avg": w.get("hrv_avg"),
            "sleep_h": round(w["sleep_s"] / 3600, 2) if w.get("sleep_s") else None,
            "sleep_score": w.get("sleep_score"),
            "stress_avg": w.get("stress_avg"),
            "steps": w.get("steps"),
            "body_battery_high": w.get("body_battery_high"),
            "body_battery_low": w.get("body_battery_low"),
            "vo2max": w.get("vo2max"),
            "load": round(t["load"], 1),
            "distance_km": round(t["distance_km"], 2),
            "duration_min": round(t["duration_min"], 1),
            "ctl": round(ctl, 1),
            "atl": round(atl, 1),
            "tsb": round(tsb, 1),
        })
    return out


def _last_data_day(conn: sqlite3.Connection) -> date:
    r = conn.execute(
        "SELECT MAX(d) FROM (SELECT MAX(date) d FROM daily UNION SELECT MAX(substr(start_time,1,10)) FROM activities)"
    ).fetchone()[0]
    return date.fromisoformat(r) if r else date.today()


def series(conn: sqlite3.Connection, metric: str, days: int = 90) -> dict:
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}; choose from {sorted(METRICS)}")
    frame = daily_frame(conn, days)
    return {"metric": metric, **METRICS[metric], "dates": [r["date"] for r in frame], "values": [r[metric] for r in frame]}


def weekly_volume(conn: sqlite3.Connection, weeks: int = 16) -> dict:
    """Training hours per ISO week (Monday start), split by sport."""
    end = _last_data_day(conn)
    start = end - timedelta(days=end.weekday()) - timedelta(weeks=weeks - 1)
    week_starts = [(start + timedelta(weeks=i)).isoformat() for i in range(weeks)]
    by_sport: dict[str, list[float]] = {}
    for a in conn.execute(
        "SELECT sport, substr(start_time,1,10) d, duration_s FROM activities WHERE substr(start_time,1,10) >= ?",
        (start.isoformat(),),
    ):
        d = date.fromisoformat(a["d"])
        idx = (d - start).days // 7
        if 0 <= idx < weeks:
            by_sport.setdefault(a["sport"], [0.0] * weeks)[idx] += (a["duration_s"] or 0) / 3600
    # Largest sports first so colour slots follow volume, then fixed for the session.
    ordered = sorted(by_sport.items(), key=lambda kv: -sum(kv[1]))
    return {"weeks": week_starts, "sports": [{"sport": s, "hours": [round(h, 2) for h in v]} for s, v in ordered]}


def _mean(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(statistics.fmean(vals), 1) if vals else None


def kpis(conn: sqlite3.Connection) -> list[dict]:
    frame = daily_frame(conn, 14)
    prev, cur = frame[:7], frame[7:]
    spec = [
        ("duration_min", "Training time (7d)", "h", lambda rows: round(sum(r["duration_min"] for r in rows) / 60, 1), "neutral"),
        ("distance_km", "Distance (7d)", "km", lambda rows: round(sum(r["distance_km"] for r in rows), 1), "neutral"),
        ("resting_hr", "Resting HR", "bpm", lambda rows: _mean([r["resting_hr"] for r in rows]), "lower"),
        ("hrv_avg", "Overnight HRV", "ms", lambda rows: _mean([r["hrv_avg"] for r in rows]), "higher"),
        ("sleep_h", "Sleep", "h/night", lambda rows: _mean([r["sleep_h"] for r in rows]), "higher"),
        ("tsb", "Form (TSB)", "", lambda rows: rows[-1]["tsb"] if rows else None, "neutral"),
    ]
    return [
        {"key": k, "label": label, "unit": unit, "current": fn(cur), "previous": fn(prev), "better": better}
        for k, label, unit, fn, better in spec
    ]


def summary_for_claude(conn: sqlite3.Connection, days: int = 90) -> dict:
    """Compact, token-cheap snapshot used as context for insight generation."""
    frame = daily_frame(conn, days)
    weekly = []
    for i in range(0, len(frame), 7):
        chunk = frame[i:i + 7]
        weekly.append({
            "week_start": chunk[0]["date"],
            "hours": round(sum(r["duration_min"] for r in chunk) / 60, 1),
            "km": round(sum(r["distance_km"] for r in chunk), 1),
            "load": round(sum(r["load"] for r in chunk)),
            "rhr": _mean([r["resting_hr"] for r in chunk]),
            "hrv": _mean([r["hrv_avg"] for r in chunk]),
            "sleep_h": _mean([r["sleep_h"] for r in chunk]),
            "sleep_score": _mean([r["sleep_score"] for r in chunk]),
            "stress": _mean([r["stress_avg"] for r in chunk]),
            "ctl_end": chunk[-1]["ctl"],
            "tsb_end": chunk[-1]["tsb"],
        })
    recent = [
        {k: r[k] for k in ("date", "load", "resting_hr", "hrv_avg", "sleep_h", "sleep_score", "stress_avg", "tsb")}
        for r in frame[-14:]
    ]
    acts = [
        dict(r)
        for r in conn.execute(
            "SELECT substr(start_time,1,10) date, sport, name, round(distance_m/1000,2) km, "
            "round(duration_s/60) min, avg_hr, max_hr, round(elev_gain_m) elev_m, training_effect "
            "FROM activities ORDER BY start_time DESC LIMIT 30"
        )
    ]
    vo2 = [r["vo2max"] for r in frame if r["vo2max"]]
    return {
        "period": {"start": frame[0]["date"], "end": frame[-1]["date"]},
        "weekly": weekly,
        "last_14_days": recent,
        "recent_activities": acts,
        "vo2max": {"first": vo2[0], "latest": vo2[-1]} if vo2 else None,
    }


def chart_data(conn: sqlite3.Connection, spec: dict) -> dict:
    """Resolve a Claude-authored chart spec into plottable data. Unknown metrics are dropped."""
    days = int(spec.get("days") or 90)
    days = max(7, min(days, 365))
    frame = daily_frame(conn, days)
    out_series = []
    for s in spec.get("series", []):
        m = s.get("metric")
        if m in METRICS:
            out_series.append({
                "metric": m,
                "label": s.get("label") or METRICS[m]["label"],
                "unit": METRICS[m]["unit"],
                "values": [r[m] for r in frame],
            })
    return {
        "type": spec.get("type") if spec.get("type") in ("line", "bar") else "line",
        "title": spec.get("title") or ", ".join(s["label"] for s in out_series),
        "dates": [r["date"] for r in frame],
        "series": out_series,
    }
