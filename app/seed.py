"""Fill the database with ~200 days of plausible fake data so the UI works before setup.

    python -m app.seed          # adds demo rows (ids prefixed demo-)
    python -m app.seed --clear  # removes demo rows
"""
import json
import math
import random
import sys
from datetime import date, datetime, timedelta

from . import db

DAYS = 200


def seed() -> None:
    rng = random.Random(42)
    conn = db.connect()
    today = date.today()
    fatigue = 0.0
    acute = chronic = 0.0
    for i in range(DAYS):
        d = today - timedelta(days=DAYS - 1 - i)
        # A build block ~6-10 weeks ago, then a deload, then steady.
        block = 1.35 if 42 <= (today - d).days <= 70 else (0.6 if 35 <= (today - d).days < 42 else 1.0)
        dow = d.weekday()
        plan = {0: None, 1: ("running", 50), 2: ("cycling", 75), 3: ("running", 45),
                4: ("strength", 40), 5: ("running", 95), 6: ("cycling", 120)}[dow]
        day_load = 0.0
        mins_today = 0.0
        if plan and rng.random() > 0.12:
            sport, base_min = plan
            mins = base_min * block * rng.uniform(0.85, 1.15)
            hr = {"running": 148, "cycling": 136, "strength": 112}[sport] + rng.uniform(-6, 8) + (4 if block > 1 else 0)
            speed_kmh = {"running": 11.5, "cycling": 27, "strength": 0}[sport] * rng.uniform(0.92, 1.06)
            start = datetime.combine(d, datetime.min.time()) + timedelta(hours=rng.choice([6, 7, 17, 18]), minutes=rng.randint(0, 50))
            name = {"running": ["Easy run", "Tempo run", "Long run"][min(2, dow // 2)],
                    "cycling": "Ride", "strength": "Strength"}[sport]
            db.upsert_activity(conn, {
                "source_id": f"demo-{d.isoformat()}",
                "start_time": start.isoformat(timespec="seconds"),
                "sport": sport, "name": name,
                "distance_m": speed_kmh * mins / 60 * 1000 if speed_kmh else None,
                "duration_s": mins * 60,
                "elev_gain_m": rng.uniform(40, 400) if sport != "strength" else None,
                "avg_hr": round(hr), "max_hr": round(hr + rng.uniform(15, 30)),
                "calories": round(mins * rng.uniform(9, 12)),
                "avg_power": round(rng.uniform(180, 230)) if sport == "cycling" else None,
                "training_effect": round(min(5, 1.5 + mins / 40 * block), 1),
                "raw_json": json.dumps({"demo": True}),
            })
            sid = f"demo-{d.isoformat()}"
            season = math.cos((d.timetuple().tm_yday - 15) / 365 * 2 * math.pi)  # warmest mid-January
            temp = round(14 + 7 * season + rng.gauss(0, 3), 1)
            db.upsert_row(conn, "activity_weather", {
                "source_id": sid, "temp_c": temp, "feels_like_c": temp, "dew_point_c": round(temp - rng.uniform(3, 9), 1),
                "humidity_pct": round(rng.uniform(45, 90)), "wind_kph": round(rng.uniform(3, 30)),
                "wind_dir": rng.choice(["N", "NW", "W", "SW", "S"]), "conditions": rng.choice(["Fair", "Mostly Cloudy", "Light Rain"])
                if sport != "strength" else None,
            } if sport != "strength" else {"source_id": sid})
            if sport == "running":
                km = int(speed_kmh * mins / 60)
                db.replace_rows(conn, "activity_laps", [{
                    "source_id": sid, "lap_index": k + 1, "distance_m": 1000.0,
                    "duration_s": round(3600 / (speed_kmh * rng.uniform(0.95, 1.05)), 1),
                    "avg_hr": round(hr - 8 + k * 16 / max(km, 1) + rng.gauss(0, 2)), "avg_cadence": round(rng.uniform(164, 176)),
                    "elev_gain_m": round(rng.uniform(0, 25)), "intensity": "ACTIVE",
                } for k in range(km)], where=("source_id", sid))
            day_load = mins * (hr - 50) / 100
            mins_today = mins
        fatigue = fatigue * 0.8 + day_load * 0.2
        acute += (day_load * 10 - acute) / 7
        chronic += (day_load * 10 - chronic) / 28
        sleep_h = rng.gauss(7.3, 0.55) - (0.3 if dow in (4, 5) else 0)
        ratio = acute / chronic if chronic > 1 else 1.0
        status = ("STRAINED" if ratio > 1.5 else "PRODUCTIVE" if ratio > 1.1 else "MAINTAINING" if ratio > 0.8
                  else "RECOVERY")
        fitness = 1 - math.exp(-i / 90)  # demo runner slowly getting fitter
        readiness = round(min(100, max(5, 72 - fatigue * 0.35 + (sleep_h - 7.3) * 10 + rng.gauss(0, 7))))
        factors = {"sleep": 75 + (sleep_h - 7.3) * 25, "recovery": 95 - fatigue * 0.9, "load": 130 - ratio * 60,
                   "hrv": 70 + rng.gauss(0, 12), "stress": 80 + rng.gauss(0, 8), "sleep_history": 72 + rng.gauss(0, 6)}
        row = {"date": d.isoformat(), "recovery_time_h": round(max(0, fatigue * 0.6), 1),
               "level": next(lv for cut, lv in ((95, "PRIME"), (75, "HIGH"), (50, "MODERATE"), (25, "LOW"), (0, "POOR"))
                             if readiness >= cut),
               "feedback": "RECOVERED_AND_READY" if readiness >= 75 else "RECOVERY_IN_PROGRESS"}
        for k, pct in factors.items():
            pct = round(min(100, max(1, pct)))
            row[f"{k}_pct"] = pct
            row[f"{k}_feedback"] = "VERY_GOOD" if pct >= 85 else "GOOD" if pct >= 65 else "MODERATE" if pct >= 35 else "POOR"
        db.upsert_readiness(conn, row)
        race_5k = 1320 - 60 * fitness + rng.gauss(0, 4)
        night = max(4.5, sleep_h) * 3600
        deep, rem = round(night * rng.uniform(0.14, 0.22)), round(night * rng.uniform(0.18, 0.25))
        db.upsert_daily(conn, {
            "date": d.isoformat(),
            "steps": int(rng.gauss(9500, 2200) + (4000 if plan and plan[0] == "running" else 0)),
            "resting_hr": round(48 + fatigue * 0.09 + rng.gauss(0, 1.2) - i * 0.006, 1),
            "hrv_avg": round(62 - fatigue * 0.22 + (sleep_h - 7.3) * 4 + rng.gauss(0, 3.5) + i * 0.015, 1),
            "sleep_s": night,
            "deep_sleep_s": deep, "rem_sleep_s": rem, "light_sleep_s": night - deep - rem,
            "sleep_score": round(min(98, max(45, 78 + (sleep_h - 7.3) * 12 - fatigue * 0.08 + rng.gauss(0, 5)))),
            "stress_avg": round(max(10, 28 + fatigue * 0.12 + rng.gauss(0, 5))),
            "body_battery_high": round(min(100, max(40, 92 - fatigue * 0.3 + (sleep_h - 7.3) * 6 + rng.gauss(0, 4)))),
            "body_battery_low": round(max(5, rng.gauss(22, 7))),
            "vo2max": round(51.5 + 1.8 * fitness + rng.gauss(0, 0.15), 1) if i % 3 == 0 else None,
            "training_readiness": readiness,
            "awake_sleep_s": round(rng.uniform(5, 40) * 60),
            "respiration_avg": round(14 + fatigue * 0.01 + rng.gauss(0, 0.4), 1),
            "spo2_avg": round(min(99, rng.gauss(95.5, 1.0))),
            "intensity_moderate_min": round(rng.uniform(5, 30) + (mins_today * 0.3 if day_load else 0)),
            "intensity_vigorous_min": round(mins_today * 0.4 if day_load else rng.uniform(0, 5)),
            "training_status": status if i > 28 else None,
            "acute_load": round(acute) if i > 7 else None,
            "chronic_load": round(chronic) if i > 28 else None,
            "endurance_score": round(6200 + 900 * fitness + rng.gauss(0, 25)),
            "race_5k_s": round(race_5k),
            "race_10k_s": round(race_5k * 2.09),
            "race_half_s": round(race_5k * 4.63),
            "race_marathon_s": round(race_5k * 9.8),
        })
    for k, days_ago in enumerate((190, 120, 60, 20)):  # threshold detected four times, improving
        when = (today - timedelta(days=days_ago)).isoformat()
        for metric, value in (("lt_hr", 171 + k), ("lt_speed", round(3.45 + 0.04 * k, 3)), ("lt_power", 290 + 4 * k)):
            db.upsert_row(conn, "benchmarks", {"date": when, "metric": metric, "value": value})
    db.upsert_row(conn, "benchmarks", {"date": (today - timedelta(days=45)).isoformat(), "metric": "ftp", "value": 248})
    runs = conn.execute("SELECT source_id, substr(start_time,1,10) d FROM activities "
                        "WHERE source_id LIKE 'demo-%' AND sport = 'running' ORDER BY distance_m DESC").fetchall()
    db.replace_rows(conn, "personal_records", [
        {"type_id": t, "label": label, "sport": sport, "kind": kind, "value": value,
         "date": runs[n]["d"], "source_id": runs[n]["source_id"]}
        for n, (t, label, sport, kind, value) in enumerate([
            (1, "1 km", "running", "time", 228), (3, "5K", "running", "time", 1262),
            (4, "10K", "running", "time", 2640), (5, "Half marathon", "running", "time", 5890),
            (7, "Longest run", "running", "distance", 21_400), (12, "Most steps in a day", "steps", "count", 31_250),
        ])
    ])
    db.replace_rows(conn, "gear", [
        {"uuid": "demo-shoe-1", "name": "Daily trainers", "type": "Shoes", "status": "active",
         "date_begin": (today - timedelta(days=230)).isoformat(), "max_m": 700_000, "distance_m": 655_000, "activities": 88},
        {"uuid": "demo-shoe-2", "name": "Race shoes", "type": "Shoes", "status": "active",
         "date_begin": (today - timedelta(days=90)).isoformat(), "max_m": 500_000, "distance_m": 140_000, "activities": 19},
        {"uuid": "demo-bike", "name": "Road bike", "type": "Bike", "status": "active",
         "date_begin": (today - timedelta(days=400)).isoformat(), "distance_m": 4_120_000, "activities": 160},
        {"uuid": "demo-shoe-0", "name": "Old trainers", "type": "Shoes", "status": "retired", "distance_m": 810_000},
    ])
    conn.commit()
    print(f"Seeded {DAYS} days of demo data into {db.DB_PATH}")


def clear() -> None:
    conn = db.connect()
    conn.execute("DELETE FROM activities WHERE source_id LIKE 'demo-%'")
    for table in ("activity_laps", "activity_weather", "personal_records"):
        conn.execute(f"DELETE FROM {table} WHERE source_id LIKE 'demo-%'")
    conn.execute("DELETE FROM gear WHERE uuid LIKE 'demo-%'")
    # Demo wellness and benchmark rows look like real ones, so these go whole; re-sync Garmin afterwards.
    for table in ("daily", "readiness", "benchmarks"):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("DELETE FROM insights")
    conn.commit()
    print("Removed demo data (wellness table cleared - re-run a Garmin sync).")


if __name__ == "__main__":
    clear() if "--clear" in sys.argv else seed()
