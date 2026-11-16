"""Parsing Garmin's readiness, training status and range endpoints. All responses below are made up."""
import sqlite3
from datetime import date

import pytest

from app import db, garmin_sync as gs


@pytest.mark.parametrize("phrase, expected", [
    ("PRODUCTIVE_1", "PRODUCTIVE"), ("NO_STATUS", "NO_STATUS"), ("", None), (None, None), (3, None),
])
def test_status_name(phrase, expected):
    assert gs.status_name(phrase) == expected


def test_readiness_prefers_morning_score():
    entries = [
        {"calendarDate": "2024-03-10", "score": 40, "inputContext": "UPDATE_REALTIME_VARIABLES",
         "timestampLocal": "2024-03-10T06:00:00"},
        {"calendarDate": "2024-03-10", "score": 70, "inputContext": "AFTER_WAKEUP_RESET",
         "timestampLocal": "2024-03-10T07:00:00"},
        # No morning entry: the day's first score.
        {"calendarDate": "2024-03-11", "score": 55, "timestampLocal": "2024-03-11T12:00:00"},
        {"calendarDate": "2024-03-11", "score": 60, "timestampLocal": "2024-03-11T07:30:00"},
        {"calendarDate": "2024-03-12", "score": None},
    ]
    assert {day: e["score"] for day, e in gs._readiness_by_day(entries).items()} == {"2024-03-10": 70, "2024-03-11": 60}


def test_readiness_row_drops_factors_without_data():
    row = gs.readiness_row({
        "calendarDate": "2024-03-10", "level": "MODERATE", "feedbackShort": "RECOVERY_IN_PROGRESS",
        "recoveryTime": 1500, "sleepScoreFactorPercent": 70, "sleepScoreFactorFeedback": "GOOD",
        "recoveryTimeFactorPercent": 30, "recoveryTimeFactorFeedback": "POOR",
        "hrvFactorPercent": 0, "hrvFactorFeedback": "NONE",
    })
    assert row == {"date": "2024-03-10", "level": "MODERATE", "feedback": "RECOVERY_IN_PROGRESS",
                   "recovery_time_h": 25.0, "sleep_pct": 70, "sleep_feedback": "GOOD",
                   "recovery_pct": 30, "recovery_feedback": "POOR"}
    assert gs.readiness_row({"calendarDate": "2024-03-10", "feedbackShort": "UNKNOWN"})["feedback"] is None


def test_training_status_uses_primary_device():
    ts = {"mostRecentTrainingStatus": {"latestTrainingStatusData": {
        "1": {"primaryTrainingDevice": False, "trainingStatusFeedbackPhrase": "DETRAINING_2",
              "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 1}},
        "2": {"primaryTrainingDevice": True, "trainingStatusFeedbackPhrase": "PRODUCTIVE_1",
              "acuteTrainingLoadDTO": {"dailyTrainingLoadAcute": 400, "dailyTrainingLoadChronic": 350}},
    }}}
    assert gs._training_status_fields(ts) == {"training_status": "PRODUCTIVE", "acute_load": 400, "chronic_load": 350}
    assert gs._training_status_fields({}) == {}


class FakeGarmin:
    def __init__(self):
        self.calls = []

    def get_training_readiness_range(self, a, b):
        self.calls.append((a, b))
        return [{"calendarDate": "2024-03-10", "score": 70, "inputContext": "AFTER_WAKEUP_RESET", "level": "HIGH",
                 "acwrFactorPercent": 90, "acwrFactorFeedback": "VERY_GOOD"}]

    def get_race_predictions(self, a, b, _type):
        return [{"calendarDate": "2024-03-10", "time5K": 1300, "time10K": 2700,
                 "timeHalfMarathon": 6000, "timeMarathon": None}]

    def get_endurance_score(self, a, b, aggregation):
        return {"groupMap": {"2024-03-10": {"groupAverage": 6500}, "2024-03-11": {"groupAverage": None}}}

    def get_hill_score(self, a, b):
        raise RuntimeError("not supported by this watch")


def test_range_rows_merge_by_day_and_skip_failures():
    g = FakeGarmin()
    rows, readiness = gs._range_rows(g, date(2024, 3, 1), date(2024, 4, 15))
    assert rows == {"2024-03-10": {
        "date": "2024-03-10", "training_readiness": 70, "race_5k_s": 1300, "race_10k_s": 2700,
        "race_half_s": 6000, "endurance_score": 6500,
    }}
    assert readiness["2024-03-10"]["load_feedback"] == "VERY_GOOD"
    assert g.calls == [("2024-03-01", "2024-03-28"), ("2024-03-29", "2024-04-15")]


def test_old_database_gains_new_columns(isolated_db):
    isolated_db.mkdir()
    old = sqlite3.connect(db.DB_PATH)
    old.execute("CREATE TABLE daily (date TEXT PRIMARY KEY, steps INTEGER)")
    old.execute("INSERT INTO daily VALUES ('2024-03-10', 8000)")
    old.commit()
    old.close()
    conn = db.connect()
    cols = {r["name"]: r["type"] for r in conn.execute("PRAGMA table_info(daily)")}
    assert cols["training_readiness"] == "REAL" and cols["training_status"] == "TEXT"
    db.upsert_daily(conn, {"date": "2024-03-10", "training_readiness": 70})
    assert tuple(conn.execute("SELECT steps, training_readiness FROM daily").fetchone()) == (8000, 70)


class FakeClient(FakeGarmin):
    """Everything sync() calls; endpoints not set up here return nothing."""

    def get_activities_by_date(self, a, b):
        return []

    def __getattr__(self, name):
        return lambda *args, **kwargs: {}


def test_range_metrics_backfill_once(conn, monkeypatch):
    g = FakeClient()
    monkeypatch.setattr(gs, "client", lambda interactive=False: g)
    gs.sync(days=3, log=lambda *_: None)
    first_start = g.calls[0][0]
    g.calls.clear()
    gs.sync(days=3, log=lambda *_: None)
    assert (date.today() - date.fromisoformat(first_start)).days == gs.BACKFILL_DAYS
    assert len(g.calls) == 1 and (date.today() - date.fromisoformat(g.calls[0][0])).days == 3
    assert conn.execute("SELECT training_readiness FROM daily WHERE date = '2024-03-10'").fetchone()[0] == 70
    assert conn.execute("SELECT level, load_pct FROM readiness WHERE date = '2024-03-10'").fetchone()[:] == ("HIGH", 90)



# --- activities, benchmarks and gear ----------------------------------------

def test_weather_is_metric_and_has_no_position():
    row = gs.weather_row("garmin-1", {"temp": 68, "apparentTemp": 72, "dewPoint": 50, "relativeHumidity": 60,
                                      "windSpeed": 10, "windDirectionCompassPoint": "sw", "latitude": 1.0,
                                      "longitude": 2.0, "weatherTypeDTO": {"desc": "Fair"}})
    assert row == {"source_id": "garmin-1", "temp_c": 20.0, "feels_like_c": 22.2, "dew_point_c": 10.0,
                   "humidity_pct": 60, "wind_kph": 16.1, "wind_dir": "SW", "conditions": "Fair"}
    assert gs.weather_row("garmin-2", {})["temp_c"] is None


def test_laps_leave_out_positions():
    laps = gs.lap_rows("garmin-1", {"lapDTOs": [
        {"lapIndex": 1, "distance": 1000.0, "duration": 300.0, "averageHR": 150.0, "startLatitude": 1.0,
         "endLongitude": 2.0, "averageRunCadence": 170.0, "intensityType": "ACTIVE"},
        {"distance": 500.0, "averageBikeCadence": 90.0},
    ]})
    assert [lap["lap_index"] for lap in laps] == [1, 2]
    assert laps[0]["avg_cadence"] == 170.0 and laps[1]["avg_cadence"] == 90.0
    assert not any("lat" in k.lower() or "lon" in k.lower() for lap in laps for k in lap)


def test_personal_records_keep_known_types_only():
    rows = gs.pr_rows([
        {"typeId": 3, "value": 1300.0, "activityId": 9, "activityStartDateTimeLocalFormatted": "2024-03-10T07:00:00.0"},
        {"typeId": 15, "value": 0.0},          # no streak yet
        {"typeId": 30, "value": 80_000.0},     # strength: units unknown
        {"typeId": 12, "value": 25_000.0, "prStartTimeGmtFormatted": "2024-02-01T00:00:00.0"},
    ])
    assert rows == [
        {"type_id": 3, "label": "5K", "sport": "running", "kind": "time", "value": 1300.0, "date": "2024-03-10",
         "source_id": "garmin-9"},
        {"type_id": 12, "label": "Most steps in a day", "sport": "steps", "kind": "count", "value": 25_000.0,
         "date": "2024-02-01", "source_id": None},
    ]


def test_threshold_speed_is_scaled_to_metres_per_second():
    lt = {"speed": [{"from": "2024-03-01T00:00:00.0", "value": 0.32}],
          "heart_rate": [{"from": "2024-03-01T00:00:00.0", "value": 175.0}], "power": []}
    rows = gs.benchmark_rows(lt, {"functionalThresholdPower": None})
    assert rows == [{"date": "2024-03-01", "metric": "lt_hr", "value": 175.0},
                    {"date": "2024-03-01", "metric": "lt_speed", "value": 3.2}]
    assert gs.benchmark_rows({}, {"functionalThresholdPower": 250, "calendarDate": "2024-03-02"}) == [
        {"date": "2024-03-02", "metric": "ftp", "value": 250}]


def test_gear_row_names_and_distance():
    item = {"uuid": "u1", "displayName": None, "customMakeModel": None, "gearMakeName": "Make",
            "gearModelName": "Model", "gearTypeName": "Shoes", "gearStatusName": "active",
            "dateBegin": "2024-01-01T00:00:00.0", "maximumMeters": 0.0}
    row = gs.gear_row(item, {"totalDistance": 123_000.0, "totalActivities": 20})
    assert (row["name"], row["max_m"], row["distance_m"], row["date_begin"]) == ("Make Model", None, 123_000.0, "2024-01-01")
