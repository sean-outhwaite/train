"""Derived metrics shared by the dashboard, the insights prompt and Claude's chart tool."""
import json
import math
import sqlite3
import statistics
from datetime import date, timedelta

# Fields shown from Garmin's activity summary. Named one by one so owner and GPS fields stay out of the API.
ACTIVITY_EXTRAS = {
    "location": "locationName",
    "moving_s": "movingDuration",
    "avg_speed": "averageSpeed",
    "max_speed": "maxSpeed",
    "gap_speed": "avgGradeAdjustedSpeed",
    "elev_loss_m": "elevationLoss",
    "min_elev_m": "minElevation",
    "max_elev_m": "maxElevation",
    "min_temp_c": "minTemperature",
    "max_temp_c": "maxTemperature",
    "steps": "steps",
    "avg_cadence_spm": "averageRunningCadenceInStepsPerMinute",
    "max_cadence_spm": "maxRunningCadenceInStepsPerMinute",
    "avg_cadence_rpm": "averageBikingCadenceInRevPerMinute",
    "max_cadence_rpm": "maxBikingCadenceInRevPerMinute",
    "stride_cm": "avgStrideLength",
    "ground_contact_ms": "avgGroundContactTime",
    "vertical_osc_cm": "avgVerticalOscillation",
    "vertical_ratio": "avgVerticalRatio",
    "max_power": "maxPower",
    "norm_power": "normPower",
    "training_load": "activityTrainingLoad",
    "anaerobic_te": "anaerobicTrainingEffect",
    "te_label": "trainingEffectLabel",
    "vo2max": "vO2MaxValue",
    "body_battery_change": "differenceBodyBattery",
}
FASTEST_SPLITS = {"1 km": "fastestSplit_1000", "1 mile": "fastestSplit_1609",
                  "5 km": "fastestSplit_5000", "10 km": "fastestSplit_10000"}

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
    "sleep_deep_h":      {"label": "Deep sleep", "unit": "h"},
    "sleep_light_h":     {"label": "Light sleep", "unit": "h"},
    "sleep_rem_h":       {"label": "REM sleep", "unit": "h"},
    "sleep_awake_h":     {"label": "Awake in bed", "unit": "h"},
    "respiration_avg":   {"label": "Sleeping breathing rate", "unit": "brpm"},
    "spo2_avg":          {"label": "Sleeping SpO2", "unit": "%"},
    "intensity_min":     {"label": "Intensity minutes", "unit": "min"},
    "training_readiness": {"label": "Training readiness", "unit": ""},
    "acute_load":        {"label": "Garmin acute load", "unit": "Garmin load"},
    "chronic_load":      {"label": "Garmin chronic load", "unit": "Garmin load"},
    "endurance_score":   {"label": "Endurance score", "unit": ""},
    "hill_score":        {"label": "Hill score", "unit": ""},
    "race_5k_min":       {"label": "5K prediction", "unit": "min"},
    "race_10k_min":      {"label": "10K prediction", "unit": "min"},
    "race_half_min":     {"label": "Half marathon prediction", "unit": "min"},
    "race_marathon_min": {"label": "Marathon prediction", "unit": "min"},
    "load":              {"label": "Training load", "unit": "TRIMP"},
    "distance_km":       {"label": "Distance", "unit": "km"},
    "duration_min":      {"label": "Training time", "unit": "min"},
    "ctl":               {"label": "Fitness (CTL)", "unit": ""},
    "atl":               {"label": "Fatigue (ATL)", "unit": ""},
    "tsb":               {"label": "Form (TSB)", "unit": ""},
}


# From the watch or Garmin, so these only exist for times it was worn or Garmin scored. The rest are
# derived from activities.
WELLNESS_METRICS = ["resting_hr", "hrv_avg", "sleep_h", "sleep_score", "stress_avg", "steps",
                    "body_battery_high", "body_battery_low", "vo2max", "sleep_deep_h", "sleep_light_h",
                    "sleep_rem_h", "sleep_awake_h", "respiration_avg", "spo2_avg", "intensity_min",
                    "training_readiness",
                    "acute_load", "chronic_load", "endurance_score", "hill_score",
                    "race_5k_min", "race_10k_min", "race_half_min", "race_marathon_min"]
SLEEP_STAGES = {"sleep_deep_h": "deep_sleep_s", "sleep_light_h": "light_sleep_s",
                "sleep_rem_h": "rem_sleep_s", "sleep_awake_h": "awake_sleep_s"}
RACES = {"race_5k_min": "race_5k_s", "race_10k_min": "race_10k_s",
         "race_half_min": "race_half_s", "race_marathon_min": "race_marathon_s"}


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
            **{k: round(w[col] / 3600, 2) if w.get(col) is not None else None for k, col in SLEEP_STAGES.items()},
            "respiration_avg": w.get("respiration_avg"),
            "spo2_avg": w.get("spo2_avg"),
            # Garmin counts vigorous minutes double towards the weekly goal, as the WHO guideline does.
            "intensity_min": None if w.get("intensity_moderate_min") is None and w.get("intensity_vigorous_min") is None
            else (w.get("intensity_moderate_min") or 0) + 2 * (w.get("intensity_vigorous_min") or 0),
            "training_readiness": w.get("training_readiness"),
            "training_status": w.get("training_status"),
            "acute_load": w.get("acute_load"),
            "chronic_load": w.get("chronic_load"),
            "endurance_score": w.get("endurance_score"),
            "hill_score": w.get("hill_score"),
            **{k: round(w[col] / 60, 2) if w.get(col) else None for k, col in RACES.items()},
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


def coverage(conn: sqlite3.Connection, days: int = 90) -> dict:
    """How complete each watch-measured metric is over the last `days` days."""
    frame = daily_frame(conn, days)
    out = {}
    for m in WELLNESS_METRICS:
        dates = [r["date"] for r in frame if r[m] is not None]
        out[m] = {"days_with_data": len(dates), "last_date": dates[-1] if dates else None}
    return {"days": len(frame), "metrics": out}


def available_metrics(conn: sqlite3.Connection, days: int = 90) -> list[str]:
    """Metric keys with something to show. Derived training metrics are always available."""
    cov = coverage(conn, days)["metrics"]
    return [m for m in METRICS if m not in cov or cov[m]["days_with_data"] > 0]


def series(conn: sqlite3.Connection, metric: str, days: int = 90) -> dict:
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}; choose from {sorted(METRICS)}")
    frame = daily_frame(conn, days)
    return {"metric": metric, **METRICS[metric], "dates": [r["date"] for r in frame], "values": [r[metric] for r in frame]}


def activity_detail(conn: sqlite3.Connection, source_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM activities WHERE source_id = ?", (source_id,)).fetchone()
    if row is None:
        return None
    a = dict(row)
    raw = json.loads(a.pop("raw_json") or "{}")
    a["has_route"] = source_id.startswith("garmin-") and bool(raw.get("hasPolyline"))
    # Data exports reuse some of these keys in other units. Only synced rows have a text start time.
    if not isinstance(raw.get("startTimeLocal"), str):
        raw = {}
    a.update({k: raw.get(field) for k, field in ACTIVITY_EXTRAS.items()})
    for key, prefix in (("hr_zones", "hrTimeInZone"), ("power_zones", "powerTimeInZone")):
        zones = [raw.get(f"{prefix}_{i}") or 0 for i in range(1, 6)]
        a[key] = zones if any(zones) else None
    a["fastest"] = [{"label": label, "seconds": raw[field]}
                    for label, field in FASTEST_SPLITS.items() if raw.get(field)]
    return a


# Garmin's readiness factors, in the order Garmin lists them.
READINESS_FACTORS = {"sleep": "Sleep score", "recovery": "Recovery time", "load": "Acute load",
                     "hrv": "HRV status", "stress": "Stress history", "sleep_history": "Sleep history"}


def readiness_latest(conn: sqlite3.Connection) -> dict | None:
    """The most recent morning readiness score and what made it up. Factors with no data are left out."""
    r = conn.execute(
        "SELECT r.*, d.training_readiness score FROM readiness r JOIN daily d USING (date) "
        "WHERE d.training_readiness IS NOT NULL ORDER BY date DESC LIMIT 1"
    ).fetchone()
    if r is None:
        return None
    factors = [{"key": k, "label": label, "percent": r[f"{k}_pct"], "feedback": r[f"{k}_feedback"]}
               for k, label in READINESS_FACTORS.items() if r[f"{k}_feedback"]]
    return _present({"date": r["date"], "score": r["score"], "level": r["level"], "feedback": r["feedback"],
                     "recovery_time_h": r["recovery_time_h"], "factors": factors})


def activity_extras(conn: sqlite3.Connection, source_id: str) -> dict:
    laps = [_present(dict(r)) for r in conn.execute(
        "SELECT lap_index, distance_m, duration_s, moving_s, avg_speed, gap_speed, avg_hr, max_hr, avg_power, "
        "avg_cadence, elev_gain_m, intensity FROM activity_laps WHERE source_id = ? ORDER BY lap_index", (source_id,))]
    w = conn.execute("SELECT * FROM activity_weather WHERE source_id = ?", (source_id,)).fetchone()
    weather = _present({k: w[k] for k in w.keys() if k != "source_id"}) if w else {}
    # A single lap is just the whole activity again.
    return {"laps": laps if len(laps) > 1 else [], "weather": weather or None}


BENCHMARKS = {"lt_hr": ("Threshold heart rate", "bpm"), "lt_speed": ("Threshold pace", "m/s"),
              "lt_power": ("Threshold running power", "W"), "ftp": ("Cycling FTP", "W")}


def benchmarks(conn: sqlite3.Connection, days: int = 365) -> dict:
    """Each benchmark's latest value against its earliest in the last `days`, plus personal records."""
    since = (date.today() - timedelta(days=days)).isoformat()
    thresholds = []
    for metric, (label, unit) in BENCHMARKS.items():
        rows = conn.execute("SELECT date, value FROM benchmarks WHERE metric = ? ORDER BY date", (metric,)).fetchall()
        if not rows:
            continue
        recent = [r for r in rows if r["date"] >= since] or rows[-1:]
        thresholds.append({"metric": metric, "label": label, "unit": unit,
                           "latest": rows[-1]["value"], "latest_date": rows[-1]["date"],
                           "first": recent[0]["value"], "first_date": recent[0]["date"]})
    records = [_present(dict(r)) for r in conn.execute(
        "SELECT p.type_id, p.label, p.sport, p.kind, p.value, p.date, a.source_id FROM personal_records p "
        "LEFT JOIN activities a USING (source_id) ORDER BY p.type_id")]
    return {"thresholds": thresholds, "records": records}


RETIRE_WARN = 0.9  # warn once gear reaches this share of its retirement distance


def gear(conn: sqlite3.Connection) -> dict:
    """Active gear, most used first, and how many items are retired."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM gear ORDER BY distance_m DESC")]
    active = [_present({**r, "worn": round(r["distance_m"] / r["max_m"], 3) if r["max_m"] and r["distance_m"] else None,
                        "warn": bool(r["max_m"] and (r["distance_m"] or 0) >= RETIRE_WARN * r["max_m"])})
              for r in rows if r["status"] != "retired"]
    return {"active": active, "retired": sum(r["status"] == "retired" for r in rows)}


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


def _sum(vals: list) -> float | None:
    vals = [v for v in vals if v is not None]
    return round(sum(vals)) if vals else None


def _latest(rows: list, key: str):
    return next((r[key] for r in reversed(rows) if r[key] is not None), None)


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
        ("intensity_min", "Intensity minutes (7d)", "of 150", lambda rows: _sum([r["intensity_min"] for r in rows]), "higher"),
        ("training_readiness", "Training readiness", "/100", lambda rows: _latest(rows, "training_readiness"), "higher"),
    ]
    tiles = [
        {"key": k, "label": label, "unit": unit, "current": fn(cur), "previous": fn(prev), "better": better}
        for k, label, unit, fn, better in spec
    ]
    return [t for t in tiles if t["current"] is not None or t["previous"] is not None]


def _present(row: dict) -> dict:
    return {k: v for k, v in row.items() if v is not None}


def summary_for_claude(conn: sqlite3.Connection, days: int = 90) -> dict:
    """Compact, token-cheap snapshot used as context for insight generation.

    Missing values are left out rather than sent as nulls.
    """
    frame = daily_frame(conn, days)
    cov = coverage(conn, days)
    weekly = []
    for i in range(0, len(frame), 7):
        chunk = frame[i:i + 7]
        weekly.append(_present({
            "week_start": chunk[0]["date"],
            "hours": round(sum(r["duration_min"] for r in chunk) / 60, 1),
            "km": round(sum(r["distance_km"] for r in chunk), 1),
            "load": round(sum(r["load"] for r in chunk)),
            "rhr": _mean([r["resting_hr"] for r in chunk]),
            "hrv": _mean([r["hrv_avg"] for r in chunk]),
            "sleep_h": _mean([r["sleep_h"] for r in chunk]),
            "sleep_score": _mean([r["sleep_score"] for r in chunk]),
            "stress": _mean([r["stress_avg"] for r in chunk]),
            "readiness": _mean([r["training_readiness"] for r in chunk]),
            "intensity_min": _sum([r["intensity_min"] for r in chunk]),
            "deep_sleep_h": _mean([r["sleep_deep_h"] for r in chunk]),
            "rem_sleep_h": _mean([r["sleep_rem_h"] for r in chunk]),
            "breathing_brpm": _mean([r["respiration_avg"] for r in chunk]),
            "spo2_pct": _mean([r["spo2_avg"] for r in chunk]),
            "ctl_end": chunk[-1]["ctl"],
            "tsb_end": chunk[-1]["tsb"],
        }))
    recent = [
        _present({k: r[k] for k in ("date", "load", "resting_hr", "hrv_avg", "sleep_h", "sleep_score", "stress_avg",
                                    "tsb", "training_readiness", "training_status", "acute_load", "chronic_load",
                                    "sleep_deep_h", "sleep_rem_h", "respiration_avg", "intensity_min")})
        for r in frame[-14:]
    ]
    acts = [
        _present(dict(r))
        for r in conn.execute(
            "SELECT substr(start_time,1,10) date, sport, name, round(distance_m/1000,2) km, "
            "round(duration_s/60) min, avg_hr, max_hr, round(elev_gain_m) elev_m, training_effect, "
            "w.temp_c, w.humidity_pct FROM activities LEFT JOIN activity_weather w USING (source_id) "
            "ORDER BY start_time DESC LIMIT 30"
        )
    ]

    def first_latest(key: str) -> dict | None:
        vals = [r[key] for r in frame if r[key] is not None]
        return {"first": vals[0], "latest": vals[-1]} if vals else None

    return {
        "period": {"start": frame[0]["date"], "end": frame[-1]["date"]},
        "data_coverage": {
            "days_in_period": cov["days"],
            "days_with_data": {m: c["days_with_data"] for m, c in cov["metrics"].items() if c["days_with_data"]},
            "not_recorded": [m for m, c in cov["metrics"].items() if not c["days_with_data"]],
        },
        "weekly": weekly,
        "last_14_days": recent,
        "recent_activities": acts,
        **{k: v for k in ("vo2max", "endurance_score", "hill_score", *RACES) if (v := first_latest(k))},
        **({"readiness_latest": r} if (r := readiness_latest(conn)) else {}),
        **({"garmin_benchmarks": b["thresholds"]} if (b := benchmarks(conn))["thresholds"] else {}),
    }


def chart_data(conn: sqlite3.Connection, spec: dict) -> dict:
    """Resolve a Claude-authored chart spec into plottable data. Unknown metrics are dropped."""
    days = int(spec.get("days") or 90)
    days = max(7, min(days, 365))
    frame = daily_frame(conn, days)
    out_series = []
    for s in spec.get("series", []):
        m = s.get("metric")
        if m in METRICS and any(r[m] is not None for r in frame):
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
