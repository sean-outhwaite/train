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
        return lambda *args: {}


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
