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
    for i in range(DAYS):
        d = today - timedelta(days=DAYS - 1 - i)
        # A build block ~6-10 weeks ago, then a deload, then steady.
        block = 1.35 if 42 <= (today - d).days <= 70 else (0.6 if 35 <= (today - d).days < 42 else 1.0)
        dow = d.weekday()
        plan = {0: None, 1: ("running", 50), 2: ("cycling", 75), 3: ("running", 45),
                4: ("strength", 40), 5: ("running", 95), 6: ("cycling", 120)}[dow]
        day_load = 0.0
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
            day_load = mins * (hr - 50) / 100
        fatigue = fatigue * 0.8 + day_load * 0.2
        sleep_h = rng.gauss(7.3, 0.55) - (0.3 if dow in (4, 5) else 0)
        db.upsert_daily(conn, {
            "date": d.isoformat(),
            "steps": int(rng.gauss(9500, 2200) + (4000 if plan and plan[0] == "running" else 0)),
            "resting_hr": round(48 + fatigue * 0.09 + rng.gauss(0, 1.2) - i * 0.006, 1),
            "hrv_avg": round(62 - fatigue * 0.22 + (sleep_h - 7.3) * 4 + rng.gauss(0, 3.5) + i * 0.015, 1),
            "sleep_s": max(4.5, sleep_h) * 3600,
            "sleep_score": round(min(98, max(45, 78 + (sleep_h - 7.3) * 12 - fatigue * 0.08 + rng.gauss(0, 5)))),
            "stress_avg": round(max(10, 28 + fatigue * 0.12 + rng.gauss(0, 5))),
            "body_battery_high": round(min(100, max(40, 92 - fatigue * 0.3 + (sleep_h - 7.3) * 6 + rng.gauss(0, 4)))),
            "body_battery_low": round(max(5, rng.gauss(22, 7))),
            "vo2max": round(51.5 + 1.8 * (1 - math.exp(-i / 90)) + rng.gauss(0, 0.15), 1) if i % 3 == 0 else None,
        })
    conn.commit()
    print(f"Seeded {DAYS} days of demo data into {db.DB_PATH}")


def clear() -> None:
    conn = db.connect()
    conn.execute("DELETE FROM activities WHERE source_id LIKE 'demo-%'")
    conn.execute("DELETE FROM daily")  # demo wellness rows are indistinguishable; re-sync Garmin afterwards
    conn.execute("DELETE FROM insights")
    conn.commit()
    print("Removed demo data (wellness table cleared - re-run a Garmin sync).")


if __name__ == "__main__":
    clear() if "--clear" in sys.argv else seed()
