"""Activity summaries: what goes into the prompt, and the streamed route. Data here is made up."""
import asyncio
import json

import pytest
from fastapi import HTTPException

from app import activity_summary, db, main
from tests.test_claude_cli import fake_claude, last_run  # noqa: F401 - fixture


@pytest.fixture
def activity(conn):
    conn.execute(
        "INSERT INTO activities (source_id, start_time, sport, name, distance_m, duration_s, avg_hr, raw_json) "
        "VALUES ('demo-1', '2026-11-20T07:00:00', 'running', 'Morning run', 8000, 2700, 148, NULL)")
    conn.execute("INSERT INTO activity_weather (source_id, temp_c, humidity_pct) VALUES ('demo-1', 24, 80)")
    conn.commit()
    return "demo-1"


def _activity_json(prompt: str) -> dict:
    return json.loads(prompt.rsplit("Activity:\n", 1)[1])


def test_prompt_includes_recorded_fields_and_weather(activity):
    data = _activity_json(activity_summary.prompt(activity))
    assert data["name"] == "Morning run" and data["distance_m"] == 8000
    assert data["weather"] == {"temp_c": 24, "humidity_pct": 80}


def test_prompt_leaves_out_what_was_not_recorded(activity):
    data = _activity_json(activity_summary.prompt(activity))
    assert "avg_power" not in data and "elev_gain_m" not in data
    assert "laps" not in data and "fastest" not in data and "hr_zones" not in data
    assert "has_route" not in data
    assert None not in data.values()


def test_prompt_for_missing_activity_is_none():
    assert activity_summary.prompt("nope") is None


def test_route_404s_for_missing_activity():
    with pytest.raises(HTTPException) as e:
        asyncio.run(main.activity_summary_route("nope"))
    assert e.value.status_code == 404


def test_route_runs_claude_once_without_saving_a_session(activity, fake_claude):  # noqa: F811
    async def go():
        res = await main.activity_summary_route(activity)
        return "".join([chunk async for chunk in res.body_iterator])

    body = asyncio.run(go())
    assert body.rstrip().endswith('event: done\ndata: {}')
    run = last_run()
    assert run["kind"] == "activity" and run["status"] == "ok"
    assert db.connect().execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0
