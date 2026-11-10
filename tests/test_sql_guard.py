"""Claude's database tools must stay read-only (CLAUDE.md). Two layers keep them that way:
validate_select() and a read-only connection. Each is tested on its own."""
import json

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from app import db, healthdb_mcp
from app.healthdb_mcp import validate_select


@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "select * from daily",
    "  SELECT date FROM daily;  ",
    "Select\n  date\nFROM daily",
    "WITH recent AS (SELECT * FROM daily) SELECT * FROM recent",
])
def test_allows_single_select(sql):
    assert validate_select(sql)


@pytest.mark.parametrize("sql", [
    "",
    "DELETE FROM daily",
    "DROP TABLE activities",
    "UPDATE daily SET steps = 0",
    "INSERT INTO daily (date) VALUES ('2020-01-01')",
    "PRAGMA query_only = OFF",
    "ATTACH DATABASE '/tmp/x.db' AS x",
    "VACUUM INTO '/tmp/copy.db'",
    "SELECT 1; DROP TABLE daily",
    "SELECT 1;DELETE FROM daily;",
    "-- harmless\nDELETE FROM daily",
    "selector FROM daily",
])
def test_rejects_anything_else(sql):
    with pytest.raises(ToolError):
        validate_select(sql)


@pytest.fixture
def seeded(conn):
    db.upsert_daily(conn, {"date": "2024-01-01", "steps": 1000})
    conn.commit()
    return conn


@pytest.mark.parametrize("sql", [
    "WITH x AS (SELECT 1) DELETE FROM daily",
    "WITH x AS (SELECT 1) UPDATE daily SET steps = 0",
    "WITH x AS (SELECT 1) INSERT INTO daily (date) VALUES ('2020-01-01')",
])
def test_writes_that_pass_the_guard_still_fail(seeded, sql):
    # A CTE can front a write, so validate_select alone isn't enough; the connection must refuse it.
    validate_select(sql)
    with pytest.raises(ToolError):
        healthdb_mcp.query_data(sql)
    assert [tuple(r) for r in seeded.execute("SELECT date, steps FROM daily")] == [("2024-01-01", 1000)]


def test_query_returns_rows(seeded):
    out = json.loads(healthdb_mcp.query_data("SELECT date, steps FROM daily"))
    assert out == {"rows": [{"date": "2024-01-01", "steps": 1000}], "truncated": False}


def test_query_caps_rows(conn):
    for i in range(healthdb_mcp.MAX_ROWS + 5):
        db.upsert_activity(conn, {"source_id": f"a{i}", "start_time": "2024-01-01T08:00:00", "sport": "running"})
    conn.commit()
    out = json.loads(healthdb_mcp.query_data("SELECT source_id FROM activities"))
    assert len(out["rows"]) == healthdb_mcp.MAX_ROWS
    assert out["truncated"] is True


def test_sql_errors_become_tool_errors(seeded):
    with pytest.raises(ToolError, match="SQL error"):
        healthdb_mcp.query_data("SELECT nope FROM daily")


def test_chart_rejects_metrics_outside_whitelist(seeded):
    with pytest.raises(ToolError, match="Unknown metrics"):
        healthdb_mcp.show_chart("x", [healthdb_mcp.SeriesRef(metric="raw_json")])


def test_chart_rejects_unrecorded_metric(seeded):
    with pytest.raises(ToolError, match="No data recorded"):
        healthdb_mcp.show_chart("x", [healthdb_mcp.SeriesRef(metric="hrv_avg")])
    assert healthdb_mcp.show_chart("x", [healthdb_mcp.SeriesRef(metric="steps")])
