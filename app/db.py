"""SQLite storage. All Garmin data lives here. Strava data is not stored, apart from figures Claude
quotes in saved insight reports."""
import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "health.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    source_id       TEXT PRIMARY KEY,
    start_time      TEXT NOT NULL,          -- local ISO datetime
    sport           TEXT NOT NULL,          -- running, cycling, swimming, strength, walking, other
    name            TEXT,
    distance_m      REAL,
    duration_s      REAL,
    elev_gain_m     REAL,
    avg_hr          REAL,
    max_hr          REAL,
    calories        REAL,
    avg_power       REAL,
    training_effect REAL,                   -- Garmin aerobic training effect (0-5)
    raw_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_activities_start ON activities(start_time);

CREATE TABLE IF NOT EXISTS daily (
    date              TEXT PRIMARY KEY,     -- YYYY-MM-DD
    steps             INTEGER,
    resting_hr        REAL,
    hrv_avg           REAL,                 -- overnight HRV, ms
    sleep_s           REAL,
    sleep_score       REAL,
    stress_avg        REAL,
    body_battery_high REAL,
    body_battery_low  REAL,
    vo2max            REAL
);

CREATE TABLE IF NOT EXISTS insights (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id                TEXT PRIMARY KEY,
    claude_session_id TEXT,
    title             TEXT,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per `claude` subprocess, written before it starts so killed runs still count.
CREATE TABLE IF NOT EXISTS claude_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    kind            TEXT NOT NULL,          -- chat, insights, discover
    conversation_id TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    input_tokens    INTEGER,                -- includes cache reads and writes
    output_tokens   INTEGER,
    tools_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_claude_runs_started ON claude_runs(started_at);
"""

ACTIVITY_COLS = [
    "source_id", "start_time", "sport", "name", "distance_m", "duration_s", "elev_gain_m",
    "avg_hr", "max_hr", "calories", "avg_power", "training_effect", "raw_json",
]
DAILY_COLS = [
    "date", "steps", "resting_hr", "hrv_avg", "sleep_s", "sleep_score", "stress_avg",
    "body_battery_high", "body_battery_low", "vo2max",
]


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if "tools_json" not in {r["name"] for r in conn.execute("PRAGMA table_info(claude_runs)")}:
        conn.execute("ALTER TABLE claude_runs ADD COLUMN tools_json TEXT")
    return conn


def connect_readonly() -> sqlite3.Connection:
    """Read-only connection used for anything Claude can query."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def upsert_activity(conn: sqlite3.Connection, row: dict) -> None:
    vals = [row.get(c) for c in ACTIVITY_COLS]
    conn.execute(
        f"INSERT OR REPLACE INTO activities ({','.join(ACTIVITY_COLS)}) "
        f"VALUES ({','.join('?' * len(ACTIVITY_COLS))})",
        vals,
    )


def upsert_daily(conn: sqlite3.Connection, row: dict) -> None:
    """Merge a partial daily row; existing non-null values survive a null update."""
    cols = [c for c in DAILY_COLS if c in row]
    updates = ",".join(f"{c}=COALESCE(excluded.{c}, daily.{c})" for c in cols if c != "date")
    conn.execute(
        f"INSERT INTO daily ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
        f"ON CONFLICT(date) DO UPDATE SET {updates}",
        [row[c] for c in cols],
    )


def start_run(kind: str, conversation_id: str | None = None) -> int:
    conn = connect()
    cur = conn.execute("INSERT INTO claude_runs (kind, conversation_id) VALUES (?, ?)", (kind, conversation_id))
    conn.commit()
    return cur.lastrowid


def finish_run(run_id: int, status: str, usage: dict | None = None, tools: list[str] | None = None) -> None:
    usage = usage or {}
    tokens_in = sum(usage.get(k) or 0 for k in
                    ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    conn = connect()
    conn.execute("UPDATE claude_runs SET status = ?, input_tokens = ?, output_tokens = ?, tools_json = ? WHERE id = ?",
                 (status, tokens_in, usage.get("output_tokens") or 0, json.dumps(tools or []), run_id))
    conn.commit()


def usage_today() -> dict:
    r = connect().execute(
        "SELECT COUNT(*) runs, COALESCE(SUM(input_tokens), 0) tokens_in, COALESCE(SUM(output_tokens), 0) tokens_out "
        "FROM claude_runs WHERE date(started_at, 'localtime') = date('now', 'localtime')"
    ).fetchone()
    return dict(r)


def conversation_turns(conversation_id: str) -> int:
    return connect().execute("SELECT COUNT(*) FROM claude_runs WHERE conversation_id = ?",
                             (conversation_id,)).fetchone()[0]


def recent_discover_attempt(minutes: int) -> bool:
    return connect().execute(
        "SELECT 1 FROM claude_runs WHERE kind = 'discover' AND started_at >= datetime('now', ?) LIMIT 1",
        (f"-{minutes} minutes",),
    ).fetchone() is not None


def latest_date(conn: sqlite3.Connection) -> str | None:
    r = conn.execute(
        "SELECT MAX(d) FROM (SELECT MAX(date) d FROM daily UNION SELECT MAX(substr(start_time,1,10)) FROM activities)"
    ).fetchone()
    return r[0]
