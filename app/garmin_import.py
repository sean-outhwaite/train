"""Import Garmin's official data export (garmin.com/account -> Data Management -> Export Your Data).

Unzip the export, then:
    python -m app.garmin_import /path/to/unzipped-export

Reads, wherever they sit in the folder tree:
  *summarizedActivities*.json   activity summaries
  *sleepData*.json              sleep duration + score
  UDSFile*.json                 steps, resting HR, stress, Body Battery
  *.fit                         activity files (used for activities missing from the summaries)
Export formats drift between years, so every field is read defensively.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

from . import db
from .garmin_sync import _sport


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception as e:
        print(f"  ! skip {path.name}: {e}", file=sys.stderr)
        return None


def _local_time(v) -> str:
    if isinstance(v, (int, float)):  # epoch ms
        return datetime.fromtimestamp(v / 1000).isoformat(timespec="seconds")
    return str(v or "").replace(" ", "T")[:19]


def _summary_activity(a: dict) -> dict:
    duration = a.get("duration") or a.get("elapsedDuration")
    if duration and duration > 86_400:  # exported in ms
        duration /= 1000
    distance = a.get("distance")
    if distance and duration and distance / duration > 30:  # >30 m/s means centimetres
        distance /= 100
    elev = a.get("elevationGain")
    if elev and distance and elev > distance:  # also centimetres
        elev /= 100
    type_key = a.get("activityType")
    type_key = type_key.get("typeKey") if isinstance(type_key, dict) else type_key
    return {
        "source_id": f"garmin-{a.get('activityId')}",
        "start_time": _local_time(a.get("startTimeLocal") or a.get("beginTimestamp")),
        "sport": _sport(type_key or a.get("sportType", "")),
        "name": a.get("name") or a.get("activityName"),
        "distance_m": distance,
        "duration_s": duration,
        "elev_gain_m": elev,
        "avg_hr": a.get("avgHr") or a.get("averageHR"),
        "max_hr": a.get("maxHr") or a.get("maxHR"),
        "calories": a.get("calories"),
        "avg_power": a.get("avgPower"),
        "training_effect": a.get("aerobicTrainingEffect"),
        "raw_json": json.dumps(a),
    }


def _fit_activity(path: Path) -> dict | None:
    from fitparse import FitFile

    try:
        fit = FitFile(str(path))
        session = next(iter(fit.get_messages("session")), None)
    except Exception as e:
        print(f"  ! skip {path.name}: {e}", file=sys.stderr)
        return None
    if session is None:
        return None
    v = {f.name: f.value for f in session}
    start = v.get("start_time")
    return {
        "source_id": f"fit-{path.stem}",
        "start_time": start.isoformat(timespec="seconds") if start else None,
        "sport": _sport(str(v.get("sport", ""))),
        "name": f"{str(v.get('sport', 'Activity')).title()} (FIT)",
        "distance_m": v.get("total_distance"),
        "duration_s": v.get("total_timer_time") or v.get("total_elapsed_time"),
        "elev_gain_m": v.get("total_ascent"),
        "avg_hr": v.get("avg_heart_rate"),
        "max_hr": v.get("max_heart_rate"),
        "calories": v.get("total_calories"),
        "avg_power": v.get("avg_power"),
        "training_effect": v.get("total_training_effect"),
        "raw_json": None,
    }


def _uds_row(d: dict) -> dict:
    stress = next((x.get("averageStressLevel") for x in (d.get("allDayStress") or {}).get("aggregatorList", [])
                   if x.get("type") == "TOTAL"), None)
    bb = {x.get("bodyBatteryStatType"): x.get("statsValue")
          for x in (d.get("bodyBattery") or {}).get("bodyBatteryStatList", [])}
    return {
        "date": d.get("calendarDate"),
        "steps": d.get("totalSteps"),
        "resting_hr": d.get("restingHeartRate"),
        "stress_avg": stress if (stress or 0) >= 0 else None,
        "body_battery_high": bb.get("HIGHEST"),
        "body_battery_low": bb.get("LOWEST"),
    }


def _sleep_row(s: dict) -> dict:
    total = sum(s.get(k) or 0 for k in ("deepSleepSeconds", "lightSleepSeconds", "remSleepSeconds")) or None
    return {
        "date": s.get("calendarDate"),
        "sleep_s": total,
        "sleep_score": (s.get("sleepScores") or {}).get("overallScore"),
    }


def import_folder(folder: str, log=print) -> dict:
    root = Path(folder).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a folder - unzip the Garmin export first")
    conn = db.connect()
    counts = {"activities": 0, "fit": 0, "wellness_days": 0, "sleep_days": 0}

    for f in root.rglob("*summarizedActivities*.json"):
        for block in _load(f) or []:
            for a in (block.get("summarizedActivitiesExport") or [block]) if isinstance(block, dict) else []:
                if a.get("activityId"):
                    db.upsert_activity(conn, _summary_activity(a))
                    counts["activities"] += 1
    conn.commit()

    known_starts = {r[0][:16] for r in conn.execute("SELECT start_time FROM activities")}
    for f in root.rglob("*.fit"):
        row = _fit_activity(f)
        if row and row["start_time"] and row["start_time"][:16] not in known_starts:
            db.upsert_activity(conn, row)
            counts["fit"] += 1
    conn.commit()

    for f in root.rglob("UDSFile*.json"):
        for d in _load(f) or []:
            if isinstance(d, dict) and d.get("calendarDate"):
                db.upsert_daily(conn, _uds_row(d))
                counts["wellness_days"] += 1
    for f in root.rglob("*sleepData*.json"):
        for s in _load(f) or []:
            if isinstance(s, dict) and s.get("calendarDate"):
                db.upsert_daily(conn, _sleep_row(s))
                counts["sleep_days"] += 1
    conn.commit()
    log(counts)
    return counts


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    import_folder(sys.argv[1])
