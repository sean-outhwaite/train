"""Garmin export parsing. Export formats drift between years, so units and shapes are checked here.

All export data below is made up."""
import json
from datetime import datetime

import pytest

from app import db, garmin_import as gi


# --- activity summaries ------------------------------------------------------

def test_summary_in_normal_units():
    row = gi._summary_activity({
        "activityId": 1, "startTimeLocal": "2024-03-10 07:00:00", "activityType": {"typeKey": "trail_running"},
        "name": "Morning run", "distance": 10_000, "duration": 3000, "elevationGain": 120, "avgHr": 150,
    })
    assert row["source_id"] == "garmin-1"
    assert row["start_time"] == "2024-03-10T07:00:00"
    assert row["sport"] == "running"
    assert (row["distance_m"], row["duration_s"], row["elev_gain_m"]) == (10_000, 3000, 120)
    assert row["avg_hr"] == 150


def test_summary_converts_ms_and_cm():
    # Some exports give duration in milliseconds and distance/elevation in centimetres.
    row = gi._summary_activity({
        "activityId": 2, "beginTimestamp": 0, "activityType": "cycling",
        "distance": 3_000_000, "duration": 3_600_000, "elevationGain": 50_000,
    })
    assert row["duration_s"] == 3600
    assert row["distance_m"] == 30_000
    assert row["elev_gain_m"] == 500
    assert row["sport"] == "cycling"


def test_summary_epoch_start_is_local_time():
    ms = 1_710_054_000_000
    row = gi._summary_activity({"activityId": 3, "beginTimestamp": ms})
    assert row["start_time"] == datetime.fromtimestamp(ms / 1000).isoformat(timespec="seconds")


def test_summary_tolerates_missing_fields():
    row = gi._summary_activity({"activityId": 4})
    assert row["sport"] == "other"
    assert row["distance_m"] is None and row["duration_s"] is None


# --- wellness ----------------------------------------------------------------

def test_uds_row():
    row = gi._uds_row({
        "calendarDate": "2024-03-10", "totalSteps": 8000, "restingHeartRate": 52,
        "allDayStress": {"aggregatorList": [{"type": "ASLEEP", "averageStressLevel": 10},
                                            {"type": "TOTAL", "averageStressLevel": 30}]},
        "bodyBattery": {"bodyBatteryStatList": [{"bodyBatteryStatType": "HIGHEST", "statsValue": 90},
                                                {"bodyBatteryStatType": "LOWEST", "statsValue": 20}]},
    })
    assert row == {"date": "2024-03-10", "steps": 8000, "resting_hr": 52, "stress_avg": 30,
                   "body_battery_high": 90, "body_battery_low": 20}


def test_uds_negative_stress_means_not_recorded():
    row = gi._uds_row({"calendarDate": "2024-03-10",
                       "allDayStress": {"aggregatorList": [{"type": "TOTAL", "averageStressLevel": -1}]}})
    assert row["stress_avg"] is None


def test_sleep_row():
    row = gi._sleep_row({"calendarDate": "2024-03-10", "deepSleepSeconds": 3600, "lightSleepSeconds": 14_400,
                         "remSleepSeconds": 7200, "awakeSleepSeconds": 900, "sleepScores": {"overallScore": 80}})
    assert row == {"date": "2024-03-10", "sleep_s": 25_200, "sleep_score": 80}
    assert gi._sleep_row({"calendarDate": "2024-03-10"})["sleep_s"] is None


def test_partial_daily_rows_merge(conn):
    db.upsert_daily(conn, {"date": "2024-03-10", "sleep_s": 25_200, "sleep_score": 80})
    db.upsert_daily(conn, {"date": "2024-03-10", "steps": 8000, "sleep_score": None})
    row = conn.execute("SELECT * FROM daily").fetchone()
    assert (row["sleep_s"], row["sleep_score"], row["steps"]) == (25_200, 80, 8000)


# --- whole folder ------------------------------------------------------------

@pytest.fixture
def export(tmp_path):
    root = tmp_path / "export"
    fitness = root / "DI_CONNECT" / "DI-Connect-Fitness"
    wellness = root / "DI_CONNECT" / "DI-Connect-Wellness"
    aggregator = root / "DI_CONNECT" / "DI-Connect-Aggregator"
    for d in (fitness, wellness, aggregator):
        d.mkdir(parents=True)
    (fitness / "user_0_summarizedActivities.json").write_text(json.dumps([{"summarizedActivitiesExport": [
        {"activityId": 1, "startTimeLocal": "2024-03-10 07:00:00", "activityType": "running", "distance": 5000,
         "duration": 1500},
        {"activityId": 2, "startTimeLocal": "2024-03-11 18:00:00", "activityType": "strength_training",
         "duration": 2400},
        {"name": "no id, skipped"},
    ]}]))
    (aggregator / "UDSFile_2024-03-01_2024-03-31.json").write_text(json.dumps([
        {"calendarDate": "2024-03-10", "totalSteps": 8000, "restingHeartRate": 52},
        {"totalSteps": 1},  # no date, skipped
    ]))
    (wellness / "2024-03-01_2024-03-31_0_sleepData.json").write_text(json.dumps([
        {"calendarDate": "2024-03-10", "deepSleepSeconds": 3600, "lightSleepSeconds": 18_000},
    ]))
    (wellness / "broken_sleepData.json").write_text("{not json")
    metrics_dir = root / "DI_CONNECT" / "DI-Connect-Metrics"
    metrics_dir.mkdir()
    (metrics_dir / "TrainingReadinessDTO_20240301_20240331.json").write_text(json.dumps([
        {"calendarDate": "2024-03-10", "score": 45, "inputContext": "UPDATE_REALTIME_VARIABLES",
         "timestampLocal": "2024-03-10T05:00:00"},
        {"calendarDate": "2024-03-10", "score": 66, "inputContext": "AFTER_WAKEUP_RESET",
         "timestampLocal": "2024-03-10T06:30:00", "level": "MODERATE", "recoveryTimeFactorPercent": 40,
         "recoveryTimeFactorFeedback": "MODERATE"},
    ]))
    (metrics_dir / "MetricsAcuteTrainingLoad_20240301_20240331.json").write_text(json.dumps([
        {"calendarDate": "2024-03-10", "dailyTrainingLoadAcute": 410, "dailyTrainingLoadChronic": 380},
    ]))
    (metrics_dir / "RunRacePredictions_20240301_20240331.json").write_text(json.dumps([
        {"calendarDate": "2024-03-10", "raceTime5K": 1290, "raceTime10K": 2690},
    ]))
    return root


def test_import_folder(export):
    counts = gi.import_folder(str(export), log=lambda *_: None)
    assert counts == {"activities": 2, "fit": 0, "wellness_days": 1, "sleep_days": 1, "garmin_metric_days": 1}
    conn = db.connect()
    sports = dict(conn.execute("SELECT source_id, sport FROM activities").fetchall())
    assert sports == {"garmin-1": "running", "garmin-2": "strength"}
    day = conn.execute("SELECT steps, resting_hr, sleep_s FROM daily WHERE date = '2024-03-10'").fetchone()
    assert tuple(day) == (8000, 52, 21_600)
    day = conn.execute("SELECT training_readiness, acute_load, chronic_load, race_5k_s, race_10k_s, race_half_s "
                       "FROM daily WHERE date = '2024-03-10'").fetchone()
    assert tuple(day) == (66, 410, 380, 1290, 2690, None)
    assert tuple(conn.execute("SELECT level, recovery_pct FROM readiness").fetchone()) == ("MODERATE", 40)


def test_metrics_row_handles_epoch_dates_and_status_phrases():
    row = gi._metrics_row({"calendarDate": datetime(2024, 3, 10, 12).timestamp() * 1000,
                           "trainingStatusFeedbackPhrase": "PRODUCTIVE_3", "time5K": 1300})
    assert row == {"date": "2024-03-10", "training_status": "PRODUCTIVE", "race_5k_s": 1300}


def test_import_is_idempotent(export):
    gi.import_folder(str(export), log=lambda *_: None)
    gi.import_folder(str(export), log=lambda *_: None)
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM daily").fetchone()[0] == 1


def test_import_missing_folder(tmp_path):
    with pytest.raises(FileNotFoundError):
        gi.import_folder(str(tmp_path / "nope"))
