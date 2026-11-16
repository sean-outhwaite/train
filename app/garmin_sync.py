"""Live sync from Garmin Connect via the unofficial `garminconnect` library.

First login (handles MFA in the terminal, then caches tokens in data/garmin_tokens):
    python -m app.garmin_sync --login
Later syncs (also triggered by the dashboard's Sync button):
    python -m app.garmin_sync [--days 180]
"""
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from garminconnect import Garmin

from . import db

load_dotenv(db.ROOT / ".env")
TOKEN_DIR = db.DATA_DIR / "garmin_tokens"
TRACK_DIR = db.DATA_DIR / "tracks"
BACKFILL_DAYS = 180

SPORT_MAP = [
    ("run", "running"), ("cycl", "cycling"), ("bik", "cycling"), ("ride", "cycling"),
    ("swim", "swimming"), ("strength", "strength"), ("walk", "walking"), ("hik", "walking"),
]


def _sport(type_key: str) -> str:
    type_key = (type_key or "").lower()
    return next((s for k, s in SPORT_MAP if k in type_key), "other")


def client(interactive: bool = False) -> Garmin:
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    has_tokens = any(TOKEN_DIR.iterdir())
    if has_tokens:
        g = Garmin()
    else:
        email, password = os.getenv("GARMIN_EMAIL"), os.getenv("GARMIN_PASSWORD")
        if not (email and password):
            raise RuntimeError("No cached Garmin tokens. Set GARMIN_EMAIL/GARMIN_PASSWORD in .env and run "
                               "`python -m app.garmin_sync --login` once in a terminal.")
        if not interactive:
            raise RuntimeError("First Garmin login must run in a terminal (MFA): `python -m app.garmin_sync --login`.")
        g = Garmin(email, password, prompt_mfa=lambda: input("Garmin MFA code: "))
    g.login(str(TOKEN_DIR))
    return g


def _activity_row(a: dict) -> dict:
    return {
        "source_id": f"garmin-{a['activityId']}",
        "start_time": (a.get("startTimeLocal") or "").replace(" ", "T"),
        "sport": _sport((a.get("activityType") or {}).get("typeKey")),
        "name": a.get("activityName"),
        "distance_m": a.get("distance"),
        "duration_s": a.get("duration"),
        "elev_gain_m": a.get("elevationGain"),
        "avg_hr": a.get("averageHR"),
        "max_hr": a.get("maxHR"),
        "calories": a.get("calories"),
        "avg_power": a.get("avgPower"),
        "training_effect": a.get("aerobicTrainingEffect"),
        "raw_json": json.dumps(a),
    }


def _safe(fn, *args):
    try:
        return fn(*args) or {}
    except Exception as e:  # one missing day shouldn't abort the sync
        print(f"  ! {fn.__name__}{args}: {e}", file=sys.stderr)
        return {}


def _daily_row(g: Garmin, day: str) -> dict:
    stats = _safe(g.get_stats, day)
    sleep = (_safe(g.get_sleep_data, day).get("dailySleepDTO") or {})
    hrv = (_safe(g.get_hrv_data, day).get("hrvSummary") or {})
    maxm = _safe(g.get_max_metrics, day)
    if isinstance(maxm, list):
        maxm = maxm[0] if maxm else {}
    generic = (maxm or {}).get("generic") or {}
    return {
        "date": day,
        "steps": stats.get("totalSteps"),
        "resting_hr": stats.get("restingHeartRate"),
        "hrv_avg": hrv.get("lastNightAvg"),
        "sleep_s": sleep.get("sleepTimeSeconds"),
        "sleep_score": ((sleep.get("sleepScores") or {}).get("overall") or {}).get("value"),
        "stress_avg": stats.get("averageStressLevel") if (stats.get("averageStressLevel") or 0) >= 0 else None,
        "body_battery_high": stats.get("bodyBatteryHighestValue"),
        "body_battery_low": stats.get("bodyBatteryLowestValue"),
        "vo2max": generic.get("vo2MaxPreciseValue") or generic.get("vo2MaxValue"),
        "deep_sleep_s": sleep.get("deepSleepSeconds"),
        "light_sleep_s": sleep.get("lightSleepSeconds"),
        "rem_sleep_s": sleep.get("remSleepSeconds"),
        "awake_sleep_s": sleep.get("awakeSleepSeconds"),
        "respiration_avg": sleep.get("averageRespirationValue"),
        "spo2_avg": sleep.get("averageSpO2Value"),
        "intensity_moderate_min": stats.get("moderateIntensityMinutes"),
        "intensity_vigorous_min": stats.get("vigorousIntensityMinutes"),
        **_training_status_fields(_safe(g.get_training_status, day)),
    }


def status_name(phrase) -> str | None:
    """Garmin's status phrases carry a variant number, e.g. PRODUCTIVE_1. Keep the status itself."""
    if not isinstance(phrase, str) or not phrase:
        return None
    return phrase.rsplit("_", 1)[0] if phrase[-1].isdigit() else phrase


def _training_status_fields(ts: dict) -> dict:
    """Garmin training status and acute/chronic load, from the primary training device."""
    by_device = ((ts.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData") or {}).values()
    status = next((d for d in by_device if d.get("primaryTrainingDevice")), None) or next(iter(by_device), None)
    if not status:
        return {}
    load = status.get("acuteTrainingLoadDTO") or {}
    return {
        "training_status": status_name(status.get("trainingStatusFeedbackPhrase")),
        "acute_load": load.get("dailyTrainingLoadAcute"),
        "chronic_load": load.get("dailyTrainingLoadChronic"),
    }


def _readiness_by_day(entries: list) -> dict[str, dict]:
    """Garmin recalculates readiness through the day. Keep the morning entry, else the day's first."""
    out: dict[str, tuple] = {}
    for e in entries or []:
        day = e.get("calendarDate")
        if not day or e.get("score") is None:
            continue
        rank = (e.get("inputContext") != "AFTER_WAKEUP_RESET", e.get("timestampLocal") or e.get("timestamp") or "")
        if day not in out or rank < out[day][0]:
            out[day] = (rank, e)
    return {day: e for day, (_, e) in out.items()}


# readiness table column prefix -> Garmin's field prefix for <prefix>FactorPercent / <prefix>FactorFeedback
READINESS_FACTORS = {"sleep": "sleepScore", "recovery": "recoveryTime", "load": "acwr", "hrv": "hrv",
                     "stress": "stressHistory", "sleep_history": "sleepHistory"}


def readiness_row(e: dict) -> dict:
    """The parts of a readiness entry behind the score. Garmin sends a factor it had no data for as
    0% with feedback NONE, so those are left out rather than stored as zero."""
    row = {
        "date": e.get("calendarDate"),
        "level": e.get("level"),
        "feedback": e.get("feedbackShort") if e.get("feedbackShort") != "UNKNOWN" else None,
        "recovery_time_h": round(e["recoveryTime"] / 60, 1) if e.get("recoveryTime") is not None else None,
    }
    for col, prefix in READINESS_FACTORS.items():
        feedback = e.get(f"{prefix}FactorFeedback")
        if feedback and feedback != "NONE":
            row[f"{col}_pct"], row[f"{col}_feedback"] = e.get(f"{prefix}FactorPercent"), feedback
    return row


def _range_rows(g: Garmin, start: date, end: date) -> tuple[dict[str, dict], dict[str, dict]]:
    """Readiness, race predictions, endurance and hill score, which Garmin serves for a date range.

    Returns daily rows and readiness rows, both keyed by date."""
    rows: dict[str, dict] = {}
    readiness: dict[str, dict] = {}

    def put(day, **fields):
        fields = {k: v for k, v in fields.items() if v is not None}
        if day and fields:
            rows.setdefault(day, {"date": day}).update(fields)

    chunk = start
    while chunk <= end:  # month-sized requests keep each response small
        a, b = chunk.isoformat(), min(chunk + timedelta(days=27), end).isoformat()
        for day, e in _readiness_by_day(_safe(g.get_training_readiness_range, a, b) or []).items():
            put(day, training_readiness=e["score"])
            readiness[day] = readiness_row(e)
        for p in _safe(g.get_race_predictions, a, b, "daily") or []:
            put(p.get("calendarDate"), race_5k_s=p.get("time5K"), race_10k_s=p.get("time10K"),
                race_half_s=p.get("timeHalfMarathon"), race_marathon_s=p.get("timeMarathon"))
        endurance = _safe(g.get_endurance_score, a, b, "daily")
        for day, grp in (endurance.get("groupMap") or {}).items():
            put(day, endurance_score=(grp or {}).get("groupAverage"))
        for h in _safe(g.get_hill_score, a, b).get("hillScoreDTOList") or []:
            put(h.get("calendarDate"), hill_score=h.get("overallScore"))
        chunk += timedelta(days=28)
    return rows, readiness


def _f_to_c(f):
    return round((f - 32) * 5 / 9, 1) if f is not None else None


def weather_row(source_id: str, w: dict) -> dict:
    """Garmin's activity weather is in °F and mph. The station's position is left out."""
    return {
        "source_id": source_id,
        "temp_c": _f_to_c(w.get("temp")),
        "feels_like_c": _f_to_c(w.get("apparentTemp")),
        "dew_point_c": _f_to_c(w.get("dewPoint")),
        "humidity_pct": w.get("relativeHumidity"),
        "wind_kph": round(w["windSpeed"] * 1.609, 1) if w.get("windSpeed") is not None else None,
        "wind_dir": (w.get("windDirectionCompassPoint") or "").upper() or None,
        "conditions": (w.get("weatherTypeDTO") or {}).get("desc"),
    }


def lap_rows(source_id: str, splits: dict) -> list[dict]:
    """Garmin's laps, without their start and end positions."""
    return [{
        "source_id": source_id,
        "lap_index": lap.get("lapIndex", i + 1),
        "distance_m": lap.get("distance"),
        "duration_s": lap.get("duration"),
        "moving_s": lap.get("movingDuration"),
        "avg_speed": lap.get("averageSpeed"),
        "gap_speed": lap.get("avgGradeAdjustedSpeed"),
        "avg_hr": lap.get("averageHR"),
        "max_hr": lap.get("maxHR"),
        "avg_power": lap.get("averagePower"),
        "avg_cadence": lap.get("averageRunCadence") or lap.get("averageBikeCadence"),
        "elev_gain_m": lap.get("elevationGain"),
        "intensity": lap.get("intensityType"),
    } for i, lap in enumerate(splits.get("lapDTOs") or [])]


def fetch_activity_extras(g: Garmin, conn, source_id: str) -> None:
    """Laps and weather for one Garmin activity. The weather row is written even when empty, so an
    indoor activity isn't fetched again."""
    garmin_id = source_id.removeprefix("garmin-")
    db.replace_rows(conn, "activity_laps", lap_rows(source_id, _safe(g.get_activity_splits, garmin_id)),
                    where=("source_id", source_id))
    db.upsert_row(conn, "activity_weather", weather_row(source_id, _safe(g.get_activity_weather, garmin_id)))


def activity_extras(source_id: str) -> None:
    """Fetch laps and weather the first time an older activity is opened."""
    conn = db.connect()
    if conn.execute("SELECT 1 FROM activity_weather WHERE source_id = ?", (source_id,)).fetchone():
        return
    fetch_activity_extras(client(), conn, source_id)
    conn.commit()


# Garmin personal record types this app shows, as type id -> (label, sport, kind). kind says how to read the
# value: time in seconds, distance or elevation in metres, power in watts, days, or a count. Strength records
# are left out because their units aren't documented.
PR_TYPES = {
    1: ("1 km", "running", "time"), 2: ("1 mile", "running", "time"), 3: ("5K", "running", "time"),
    4: ("10K", "running", "time"), 5: ("Half marathon", "running", "time"), 6: ("Marathon", "running", "time"),
    7: ("Longest run", "running", "distance"), 8: ("Longest ride", "cycling", "distance"),
    9: ("Most climbing in a ride", "cycling", "elevation"), 10: ("Best 20-min power", "cycling", "power"),
    11: ("40 km ride", "cycling", "time"), 12: ("Most steps in a day", "steps", "count"),
    13: ("Most steps in a week", "steps", "count"), 14: ("Most steps in a month", "steps", "count"),
    15: ("Longest step-goal streak", "steps", "days"), 17: ("Longest pool swim", "swimming", "distance"),
    18: ("100 m swim", "swimming", "time"), 20: ("400 m swim", "swimming", "time"),
    22: ("750 m swim", "swimming", "time"), 23: ("1000 m swim", "swimming", "time"),
    25: ("1500 m swim", "swimming", "time"),
}


def pr_rows(records: list) -> list[dict]:
    rows = []
    for r in records or []:
        if (t := PR_TYPES.get(r.get("typeId"))) and r.get("value"):
            label, sport, kind = t
            when = r.get("activityStartDateTimeLocalFormatted") or r.get("prStartTimeGmtFormatted") or ""
            rows.append({"type_id": r["typeId"], "label": label, "sport": sport, "kind": kind, "value": r["value"],
                         "date": when[:10] or None,
                         "source_id": f"garmin-{r['activityId']}" if r.get("activityId") else None})
    return rows


def benchmark_rows(lt: dict, ftp: dict) -> list[dict]:
    """Lactate threshold history (Garmin gives speed in tenths of m/s) and the latest cycling FTP."""
    rows = []
    for series, metric, scale in (("heart_rate", "lt_hr", 1), ("speed", "lt_speed", 10), ("power", "lt_power", 1)):
        for p in (lt or {}).get(series) or []:
            if p.get("from") and p.get("value"):
                rows.append({"date": p["from"][:10], "metric": metric, "value": round(p["value"] * scale, 3)})
    if (ftp or {}).get("functionalThresholdPower") and ftp.get("calendarDate"):
        rows.append({"date": ftp["calendarDate"][:10], "metric": "ftp", "value": ftp["functionalThresholdPower"]})
    return rows


def sync_benchmarks(g: Garmin, conn) -> None:
    """A few calls each sync: they cover the full history, so there's nothing to backfill."""
    today = date.today()
    lt = _safe(lambda: g.get_lactate_threshold(latest=False, start_date=(today - timedelta(days=365)).isoformat(),
                                               end_date=today.isoformat()))
    for row in benchmark_rows(lt, _safe(g.get_cycling_ftp)):
        db.upsert_row(conn, "benchmarks", row)
    records = _safe(g.get_personal_record)
    if records:
        db.replace_rows(conn, "personal_records", pr_rows(records))


def gear_row(item: dict, stats: dict) -> dict:
    return {
        "uuid": item["uuid"],
        "name": item.get("displayName") or item.get("customMakeModel")
        or " ".join(filter(None, (item.get("gearMakeName"), item.get("gearModelName")))) or None,
        "type": item.get("gearTypeName"),
        "status": item.get("gearStatusName"),
        "date_begin": (item.get("dateBegin") or "")[:10] or None,
        "date_end": (item.get("dateEnd") or "")[:10] or None,
        "max_m": item.get("maximumMeters") or None,
        "distance_m": stats.get("totalDistance"),
        "activities": stats.get("totalActivities"),
    }


def sync_gear(g: Garmin, conn) -> None:
    """One call for the list and one per item for its distance."""
    items = _safe(g.get_gear, g.profile_id)
    if isinstance(items, list):
        db.replace_rows(conn, "gear", [gear_row(i, _safe(g.get_gear_stats, i["uuid"])) for i in items if i.get("uuid")])


def track(source_id: str) -> list[list[float]]:
    """An activity's GPS route as [lat, lon] pairs, fetched from Garmin the first time it's viewed.

    Saved as files rather than in the database, so Claude's query tool can't reach exact routes.
    """
    path = TRACK_DIR / f"{source_id}.json"
    if path.exists():
        return json.loads(path.read_text())
    details = client().get_activity_details(source_id.removeprefix("garmin-"), maxchart=1, maxpoly=4000)
    points = [[p["lat"], p["lon"]] for p in (details.get("geoPolylineDTO") or {}).get("polyline") or []
              if p.get("lat") is not None and p.get("lon") is not None]
    if points:  # an empty route may just not be processed yet, so try again next time
        TRACK_DIR.mkdir(exist_ok=True)
        path.write_text(json.dumps(points))
    return points


def sync(days: int | None = None, interactive: bool = False, log=print) -> dict:
    g = client(interactive)
    conn = db.connect()
    today = date.today()
    if days is None:
        last = conn.execute("SELECT MAX(date) FROM daily").fetchone()[0]
        # Re-pull the last 3 days: sleep/HRV for a day finalise the next morning.
        start = date.fromisoformat(last) - timedelta(days=3) if last else today - timedelta(days=BACKFILL_DAYS)
    else:
        start = today - timedelta(days=days)

    acts = g.get_activities_by_date(start.isoformat(), today.isoformat())
    for a in acts:
        db.upsert_activity(conn, _activity_row(a))
    conn.commit()
    log(f"activities: {len(acts)}")
    fetched = {r[0] for r in conn.execute("SELECT source_id FROM activity_weather")}
    for a in acts:
        if (sid := f"garmin-{a['activityId']}") not in fetched:
            fetch_activity_extras(g, conn, sid)
    conn.commit()
    log("activity laps and weather: done")
    sync_benchmarks(g, conn)
    conn.commit()
    sync_gear(g, conn)
    conn.commit()
    log("benchmarks, records and gear: done")

    # Range endpoints cover months in a few calls, so the first sync that has them backfills the full history.
    backfilled = db.get_meta(conn, "garmin_readiness_backfilled")
    ranged_start = start if backfilled else min(start, today - timedelta(days=BACKFILL_DAYS))
    rows, readiness = _range_rows(g, ranged_start, today)
    for row in rows.values():
        db.upsert_daily(conn, row)
    for row in readiness.values():
        db.upsert_readiness(conn, row)
    db.set_meta(conn, "garmin_readiness_backfilled", "1")
    conn.commit()
    log("readiness and race predictions: done")

    n = (today - start).days + 1
    for i in range(n):
        day = (start + timedelta(days=i)).isoformat()
        db.upsert_daily(conn, _daily_row(g, day))
        if i % 10 == 0:
            conn.commit()
            log(f"wellness: {i + 1}/{n} days")
    db.set_meta(conn, "garmin_synced_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    conn.commit()
    return {"activities": len(acts), "days": n, "from": start.isoformat()}


if __name__ == "__main__":
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else None
    print(sync(days, interactive=True))
