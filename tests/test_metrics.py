"""Derived metrics. Wrong numbers here still draw plausible charts, so they're checked by hand."""
import math

import pytest

from app import db, metrics


def act(conn, source_id, start, **kw):
    db.upsert_activity(conn, {"source_id": source_id, "start_time": start, "sport": kw.pop("sport", "running"), **kw})


# --- TRIMP -------------------------------------------------------------------

def test_load_without_hr_uses_duration():
    assert metrics._activity_load({"duration_s": 3600, "avg_hr": None}, 50, 190) == pytest.approx(36)


def test_load_banister_trimp():
    # ratio = (120 - 50) / (190 - 50) = 0.5
    expected = 60 * 0.5 * 0.64 * math.exp(1.92 * 0.5)
    assert metrics._activity_load({"duration_s": 3600, "avg_hr": 120}, 50, 190) == pytest.approx(expected)


@pytest.mark.parametrize("avg_hr, ratio", [(30, 0), (250, 1)])
def test_load_hr_ratio_is_clamped(avg_hr, ratio):
    expected = 60 * ratio * 0.64 * math.exp(1.92 * ratio)
    assert metrics._activity_load({"duration_s": 3600, "avg_hr": avg_hr}, 50, 190) == pytest.approx(expected)


def test_load_ignores_hr_when_max_not_above_rest():
    assert metrics._activity_load({"duration_s": 3600, "avg_hr": 120}, 60, 60) == pytest.approx(36)


# --- daily frame / CTL / ATL / TSB -------------------------------------------

def test_fitness_fatigue_and_form(conn):
    # One 100-minute session without HR = load 60, on 10 March. Wellness on 11 March extends the frame a day.
    act(conn, "a1", "2024-03-10T07:00:00", duration_s=6000, distance_m=12_345)
    db.upsert_daily(conn, {"date": "2024-03-11", "steps": 5000})
    conn.commit()
    frame = metrics.daily_frame(conn, 7)

    assert len(frame) == 7
    assert frame[-1]["date"] == "2024-03-11"
    day, after = frame[-2], frame[-1]

    assert day["load"] == 60
    assert day["distance_km"] == 12.35
    assert day["duration_min"] == 100
    assert day["ctl"] == round(60 / 42, 1)
    assert day["atl"] == round(60 / 7, 1)
    assert day["tsb"] == 0  # form going into the day, before the session

    ctl, atl = 60 / 42, 60 / 7
    assert after["load"] == 0
    assert after["ctl"] == round(ctl * 41 / 42, 1)
    assert after["atl"] == round(atl * 6 / 7, 1)
    assert after["tsb"] == round(ctl - atl, 1)


def test_ctl_converges_to_steady_load(conn):
    for i in range(1, 29):
        for month in ("01", "02", "03", "04", "05", "06", "07", "08", "09", "10"):
            act(conn, f"a{month}{i}", f"2024-{month}-{i:02d}T07:00:00", duration_s=6000)
    conn.commit()
    last = metrics.daily_frame(conn, 30)[-1]
    # Roughly 60/day with a few rest days at month ends; CTL and ATL should both sit near it.
    assert 50 < last["ctl"] < 62
    assert 45 < last["atl"] < 62


def test_gaps_are_none_not_zero(conn):
    # CLAUDE.md: a gap means not recorded, so missing wellness must not become 0.
    db.upsert_daily(conn, {"date": "2024-03-01", "resting_hr": 50, "sleep_s": 27_000})
    db.upsert_daily(conn, {"date": "2024-03-03", "steps": 4000})
    conn.commit()
    frame = {r["date"]: r for r in metrics.daily_frame(conn, 3)}
    assert frame["2024-03-01"]["sleep_h"] == 7.5
    assert frame["2024-03-02"]["resting_hr"] is None
    assert frame["2024-03-02"]["sleep_h"] is None
    assert frame["2024-03-03"]["resting_hr"] is None


def test_dates_are_contiguous(conn):
    db.upsert_daily(conn, {"date": "2024-03-01", "steps": 1})
    conn.commit()
    dates = [r["date"] for r in metrics.daily_frame(conn, 10, end=metrics.date(2024, 3, 5))]
    assert dates == [f"2024-02-{d}" for d in (25, 26, 27, 28, 29)] + [f"2024-03-0{d}" for d in range(1, 6)]


# --- coverage and the chart whitelist ----------------------------------------

@pytest.fixture
def partial(conn):
    """Steps on two days, resting HR on one, nothing else recorded."""
    db.upsert_daily(conn, {"date": "2024-03-01", "steps": 1000, "resting_hr": 52})
    db.upsert_daily(conn, {"date": "2024-03-05", "steps": 2000})
    conn.commit()
    return conn


def test_coverage(partial):
    cov = metrics.coverage(partial, 7)
    assert cov["days"] == 7
    assert cov["metrics"]["steps"] == {"days_with_data": 2, "last_date": "2024-03-05"}
    assert cov["metrics"]["resting_hr"] == {"days_with_data": 1, "last_date": "2024-03-01"}
    assert cov["metrics"]["hrv_avg"] == {"days_with_data": 0, "last_date": None}


def test_available_metrics_hides_unrecorded(partial):
    available = metrics.available_metrics(partial, 7)
    assert {"steps", "resting_hr", "load", "ctl", "tsb"} <= set(available)
    assert "hrv_avg" not in available
    assert "vo2max" not in available


def test_series_rejects_unknown_metric(partial):
    with pytest.raises(ValueError):
        metrics.series(partial, "raw_json")


def test_chart_data_drops_unknown_and_empty_series(partial):
    out = metrics.chart_data(partial, {"days": 7, "type": "pie", "series": [
        {"metric": "steps"}, {"metric": "hrv_avg"}, {"metric": "raw_json"}, {"metric": "resting_hr", "label": "RHR"},
    ]})
    assert out["type"] == "line"
    assert [s["metric"] for s in out["series"]] == ["steps", "resting_hr"]
    assert out["series"][1]["label"] == "RHR"
    assert len(out["dates"]) == 7


@pytest.mark.parametrize("days, expected", [(1, 7), (10_000, 365), (None, 90)])
def test_chart_data_clamps_days(partial, days, expected):
    assert len(metrics.chart_data(partial, {"days": days, "series": []})["dates"]) == expected


# --- dashboard summaries -----------------------------------------------------

def test_weekly_volume_groups_by_iso_week(conn):
    act(conn, "mon", "2024-03-04T07:00:00", duration_s=3600)                  # Monday
    act(conn, "sun", "2024-03-10T07:00:00", duration_s=1800)                  # Sunday, same week
    act(conn, "next", "2024-03-11T07:00:00", duration_s=7200, sport="cycling")  # next Monday
    conn.commit()
    out = metrics.weekly_volume(conn, 2)
    assert out["weeks"] == ["2024-03-04", "2024-03-11"]
    by_sport = {s["sport"]: s["hours"] for s in out["sports"]}
    assert by_sport == {"cycling": [0, 2], "running": [1.5, 0]}
    assert out["sports"][0]["sport"] == "cycling"  # most hours first


def test_kpis_hide_tiles_without_data(conn):
    act(conn, "a1", "2024-03-10T07:00:00", duration_s=3600, distance_m=10_000)
    conn.commit()
    keys = {t["key"] for t in metrics.kpis(conn)}
    assert {"duration_min", "distance_km", "tsb"} <= keys
    assert not keys & {"resting_hr", "hrv_avg", "sleep_h"}


def test_activity_detail_keeps_owner_fields_out(conn):
    raw = '{"startTimeLocal": "2024-03-10 07:00:00", "locationName": "Park", "ownerFullName": "Someone", ' \
          '"hrTimeInZone_2": 600, "fastestSplit_1000": 240.5, "hasPolyline": true}'
    act(conn, "garmin-1", "2024-03-10T07:00:00", raw_json=raw)
    conn.commit()
    a = metrics.activity_detail(conn, "garmin-1")
    assert a["location"] == "Park"
    assert "ownerFullName" not in a and "raw_json" not in a
    assert a["hr_zones"] == [0, 600, 0, 0, 0]
    assert a["power_zones"] is None
    assert a["fastest"] == [{"label": "1 km", "seconds": 240.5}]
    assert a["has_route"] is True
    assert metrics.activity_detail(conn, "missing") is None
