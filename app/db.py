"""SQLite storage. All Garmin data lives here; Strava data is never stored."""
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


def latest_date(conn: sqlite3.Connection) -> str | None:
    r = conn.execute(
        "SELECT MAX(d) FROM (SELECT MAX(date) d FROM daily UNION SELECT MAX(substr(start_time,1,10)) FROM activities)"
    ).fetchone()
    return r[0]
