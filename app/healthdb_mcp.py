"""stdio MCP server that gives headless Claude Code read-only access to the local health database.

Launched by `claude -p` via the generated data/mcp.json; not meant to be run by hand.
"""
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydantic import BaseModel, Field  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from app import db, metrics  # noqa: E402

MAX_ROWS = 200
server = MCPServer("healthdb")


def validate_select(sql: str) -> str:
    """Allow exactly one SELECT/WITH statement. The connection is read-only as well."""
    stmt = sql.strip().rstrip(";").strip()
    if ";" in stmt:
        raise ToolError("Only a single statement is allowed.")
    if not re.match(r"(?is)^(select|with)\b", stmt):
        raise ToolError("Only SELECT queries are allowed.")
    return stmt


@server.tool()
def query_data(sql: str) -> str:
    """Run one read-only SQLite SELECT against the user's Garmin data and return up to 200 rows as JSON.

    Tables:
      activities(source_id, start_time TEXT local ISO, sport [running|cycling|swimming|strength|walking|other],
                 name, distance_m, duration_s, elev_gain_m, avg_hr, max_hr, calories, avg_power,
                 training_effect [Garmin aerobic TE 0-5])
      daily(date YYYY-MM-DD, steps, resting_hr bpm, hrv_avg ms overnight, sleep_s, sleep_score 0-100,
            stress_avg 0-100, body_battery_high, body_battery_low, vo2max)
    Use substr(start_time,1,10) to join activities to daily.date.
    """
    stmt = validate_select(sql)
    conn = db.connect_readonly()
    conn.execute("PRAGMA query_only = ON")
    try:
        rows = conn.execute(stmt).fetchmany(MAX_ROWS + 1)
    except sqlite3.Error as e:
        raise ToolError(f"SQL error: {e}") from e
    truncated = len(rows) > MAX_ROWS
    out = {"rows": [dict(r) for r in rows[:MAX_ROWS]], "truncated": truncated}
    return json.dumps(out, default=str)


@server.tool()
def get_metric_series(metric: str, days: int = 90) -> str:
    """Daily values of one derived metric for the last `days` days (7-365), as JSON.

    Metrics: resting_hr, hrv_avg, sleep_h, sleep_score, stress_avg, steps, body_battery_high,
    body_battery_low, vo2max, load (daily TRIMP), distance_km, duration_min,
    ctl (42-day fitness), atl (7-day fatigue), tsb (form = ctl - atl).
    """
    conn = db.connect_readonly()
    try:
        return json.dumps(metrics.series(conn, metric, max(7, min(days, 365))))
    except ValueError as e:
        raise ToolError(str(e)) from e


@server.tool()
def get_data_coverage(days: int = 90) -> str:
    """How many of the last `days` days (7-365) have data for each watch-measured metric, as JSON.

    A metric with 0 days was not recorded. Training metrics (load, distance_km, duration_min, ctl, atl,
    tsb) come from activities and aren't listed.
    """
    return json.dumps(metrics.coverage(db.connect_readonly(), max(7, min(days, 365))))


class SeriesRef(BaseModel):
    metric: str = Field(description="One of the metric keys listed in get_metric_series")
    label: str | None = Field(default=None, description="Optional legend label")


@server.tool()
def show_chart(
    title: str,
    series: list[SeriesRef],
    type: Literal["line", "bar"] = "line",
    days: int = 90,
) -> str:
    """Display a chart to the user inline in the chat. The app fills in the real data.

    Put 1-3 series per chart. Series with different units are drawn as separate stacked panels
    sharing the time axis (never a dual axis). Use bar for daily load/distance/duration, line otherwise.
    """
    unknown = [s.metric for s in series if s.metric not in metrics.METRICS]
    if unknown:
        raise ToolError(f"Unknown metrics {unknown}. Valid: {sorted(metrics.METRICS)}")
    available = metrics.available_metrics(db.connect_readonly(), max(7, min(days, 365)))
    empty = [s.metric for s in series if s.metric not in available]
    if empty:
        raise ToolError(f"No data recorded for {empty} in this period. Available: {sorted(available)}")
    return "Chart displayed to the user."


if __name__ == "__main__":
    server.run()
