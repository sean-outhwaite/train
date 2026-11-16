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
    vo2max            REAL,
    training_readiness REAL,                -- Garmin training readiness, 0-100 (morning score)
    training_status   TEXT,                 -- Garmin training status phrase, e.g. PRODUCTIVE
    acute_load        REAL,                 -- Garmin 7-day acute training load
    chronic_load      REAL,                 -- Garmin 28-day chronic training load
    endurance_score   REAL,
    hill_score        REAL,
    race_5k_s         REAL,                 -- Garmin race predictions, seconds
    race_10k_s        REAL,
    race_half_s       REAL,
    race_marathon_s   REAL,
    deep_sleep_s      REAL,                 -- sleep stages, seconds
    light_sleep_s     REAL,
    rem_sleep_s       REAL,
    awake_sleep_s     REAL,
    respiration_avg   REAL,                 -- breaths/min while asleep
    spo2_avg          REAL,                 -- % while asleep
    intensity_moderate_min REAL,
    intensity_vigorous_min REAL
);

-- What made up each morning's training readiness score (score itself is daily.training_readiness).
-- Factor percentages: 100 is best. A factor Garmin had no data for stays NULL.
CREATE TABLE IF NOT EXISTS readiness (
    date              TEXT PRIMARY KEY,     -- YYYY-MM-DD
    level             TEXT,                 -- POOR, LOW, MODERATE, HIGH, PRIME
    feedback          TEXT,                 -- Garmin's short phrase, e.g. RECOVERED_AND_READY
    recovery_time_h   REAL,                 -- hours of recovery left when scored
    sleep_pct REAL, sleep_feedback TEXT,
    recovery_pct REAL, recovery_feedback TEXT,
    load_pct REAL, load_feedback TEXT,
    hrv_pct REAL, hrv_feedback TEXT,
    stress_pct REAL, stress_feedback TEXT,
    sleep_history_pct REAL, sleep_history_feedback TEXT
);

-- Per-lap splits, fetched from Garmin with the activity's weather. No lap positions are stored.
CREATE TABLE IF NOT EXISTS activity_laps (
    source_id    TEXT NOT NULL,
    lap_index    INTEGER NOT NULL,
    distance_m   REAL,
    duration_s   REAL,
    moving_s     REAL,
    avg_speed    REAL,                      -- m/s
    gap_speed    REAL,                      -- grade-adjusted, m/s
    avg_hr       REAL,
    max_hr       REAL,
    avg_power    REAL,
    avg_cadence  REAL,
    elev_gain_m  REAL,
    intensity    TEXT,                      -- Garmin's lap type, e.g. ACTIVE, REST, WARMUP
    PRIMARY KEY (source_id, lap_index)
);

-- Weather at the start of an activity, converted to metric. Every fetched activity gets a row, even
-- with no weather (indoor), so it isn't fetched again. The weather station's position isn't stored.
CREATE TABLE IF NOT EXISTS activity_weather (
    source_id     TEXT PRIMARY KEY,
    temp_c        REAL,
    feels_like_c  REAL,
    dew_point_c   REAL,
    humidity_pct  REAL,
    wind_kph      REAL,
    wind_dir      TEXT,
    conditions    TEXT
);

-- Garmin's lactate threshold and FTP over time. metric: lt_hr (bpm), lt_speed (m/s),
-- lt_power (W, running), ftp (W, cycling).
CREATE TABLE IF NOT EXISTS benchmarks (
    date    TEXT NOT NULL,
    metric  TEXT NOT NULL,
    value   REAL NOT NULL,
    PRIMARY KEY (date, metric)
);

-- Garmin personal records. kind says how to read value: time (s), distance (m), count, days, power (W),
-- elevation (m).
CREATE TABLE IF NOT EXISTS personal_records (
    type_id     INTEGER PRIMARY KEY,
    label       TEXT NOT NULL,
    sport       TEXT,
    kind        TEXT NOT NULL,
    value       REAL NOT NULL,
    date        TEXT,
    source_id   TEXT                        -- the activity it was set in, when there is one
);

-- Shoes, bikes and other gear from Garmin, with distance logged so far.
CREATE TABLE IF NOT EXISTS gear (
    uuid        TEXT PRIMARY KEY,
    name        TEXT,
    type        TEXT,                       -- Shoes, Bike, ...
    status      TEXT,                       -- active, retired
    date_begin  TEXT,
    date_end    TEXT,
    max_m       REAL,                       -- retirement distance set in Garmin, if any
    distance_m  REAL,
    activities  INTEGER
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

-- App bookkeeping, such as when Garmin last synced.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

ACTIVITY_COLS = [
    "source_id", "start_time", "sport", "name", "distance_m", "duration_s", "elev_gain_m",
    "avg_hr", "max_hr", "calories", "avg_power", "training_effect", "raw_json",
]
DAILY_COLS = [
    "date", "steps", "resting_hr", "hrv_avg", "sleep_s", "sleep_score", "stress_avg",
    "body_battery_high", "body_battery_low", "vo2max",
    "training_readiness", "training_status", "acute_load", "chronic_load", "endurance_score", "hill_score",
    "race_5k_s", "race_10k_s", "race_half_s", "race_marathon_s",
    "deep_sleep_s", "light_sleep_s", "rem_sleep_s", "awake_sleep_s", "respiration_avg", "spo2_avg",
    "intensity_moderate_min", "intensity_vigorous_min",
]
READINESS_COLS = [
    "date", "level", "feedback", "recovery_time_h",
    "sleep_pct", "sleep_feedback", "recovery_pct", "recovery_feedback", "load_pct", "load_feedback",
    "hrv_pct", "hrv_feedback", "stress_pct", "stress_feedback", "sleep_history_pct", "sleep_history_feedback",
]
# Columns from training_readiness on were added later; older databases gain them on connect.
_FIRST_ADDED_DAILY_COL = "training_readiness"


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if "tools_json" not in {r["name"] for r in conn.execute("PRAGMA table_info(claude_runs)")}:
        conn.execute("ALTER TABLE claude_runs ADD COLUMN tools_json TEXT")
    have = {r["name"] for r in conn.execute("PRAGMA table_info(daily)")}
    for col in DAILY_COLS[DAILY_COLS.index(_FIRST_ADDED_DAILY_COL):]:
        if col not in have:
            conn.execute(f"ALTER TABLE daily ADD COLUMN {col} {'TEXT' if col == 'training_status' else 'REAL'}")
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


def upsert_readiness(conn: sqlite3.Connection, row: dict) -> None:
    """Each row comes whole from one Garmin readiness entry, so it replaces the day's row."""
    conn.execute(
        f"INSERT OR REPLACE INTO readiness ({','.join(READINESS_COLS)}) "
        f"VALUES ({','.join('?' * len(READINESS_COLS))})",
        [row.get(c) for c in READINESS_COLS],
    )


def replace_rows(conn: sqlite3.Connection, table: str, rows: list[dict], where: tuple[str, object] | None = None) -> None:
    """Replace a table's rows (or those matching `where`) with `rows`. Only for this module's own tables."""
    assert table in {"activity_laps", "personal_records", "gear"}
    if where:
        conn.execute(f"DELETE FROM {table} WHERE {where[0]} = ?", (where[1],))
    else:
        conn.execute(f"DELETE FROM {table}")
    for row in rows:
        conn.execute(f"INSERT INTO {table} ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))


def upsert_row(conn: sqlite3.Connection, table: str, row: dict) -> None:
    assert table in {"activity_weather", "benchmarks"}
    conn.execute(f"INSERT OR REPLACE INTO {table} ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                 list(row.values()))


def upsert_daily(conn: sqlite3.Connection, row: dict) -> None:
    """Merge a partial daily row; existing non-null values survive a null update."""
    cols = [c for c in DAILY_COLS if c in row]
    updates = ",".join(f"{c}=COALESCE(excluded.{c}, daily.{c})" for c in cols if c != "date")
    conn.execute(
        f"INSERT INTO daily ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
        f"ON CONFLICT(date) DO {f'UPDATE SET {updates}' if updates else 'NOTHING'}",
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


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    r = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))


def latest_date(conn: sqlite3.Connection) -> str | None:
    r = conn.execute(
        "SELECT MAX(d) FROM (SELECT MAX(date) d FROM daily UNION SELECT MAX(substr(start_time,1,10)) FROM activities)"
    ).fetchone()
    return r[0]
