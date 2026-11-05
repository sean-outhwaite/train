"""Live sync from Garmin Connect via the unofficial `garminconnect` library.

First login (handles MFA in the terminal, then caches tokens in data/garmin_tokens):
    python -m app.garmin_sync --login
Later syncs (also triggered by the dashboard's Sync button):
    python -m app.garmin_sync [--days 180]
"""
import json
import os
import sys
from datetime import date, timedelta

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
    }


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

    n = (today - start).days + 1
    for i in range(n):
        day = (start + timedelta(days=i)).isoformat()
        db.upsert_daily(conn, _daily_row(g, day))
        if i % 10 == 0:
            conn.commit()
            log(f"wellness: {i + 1}/{n} days")
    conn.commit()
    return {"activities": len(acts), "days": n, "from": start.isoformat()}


if __name__ == "__main__":
    days = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else None
    print(sync(days, interactive=True))
