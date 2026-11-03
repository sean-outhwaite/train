# Health Dash

A local dashboard for your Garmin data. It has charts, AI insights and a chat that can drill into them.
Claude runs through your logged-in **Claude Code** CLI (`claude -p`), so usage counts toward your Claude plan
and you don't need an API key.

- **Charts and insights** use your Garmin history, stored locally in `data/health.db`.
- **Chat** can query that database, draw charts inline, and use your claude.ai **Strava connector** for
  Strava-only detail (segments, kudos, Relative Effort, gear). Strava data is never stored, which keeps
  the app within Strava's API policy.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
claude            # then /login, if Claude Code isn't logged in
```

## Get data in (pick one)

```bash
python -m app.seed                 # demo data, to try the UI (remove with: python -m app.seed --clear)
```

- **Garmin export (official):** request your data at Garmin Connect → Account → Data Management →
  Export Your Data. Unzip it, then run `python -m app.garmin_import ~/Downloads/<export-folder>`
  or use **Import export…** in the UI.
- **Live Garmin sync (unofficial `garminconnect` library, a grey area under Garmin's terms):** copy
  `.env.example` to `.env`, fill in `GARMIN_EMAIL`/`GARMIN_PASSWORD`, and run
  `python -m app.garmin_sync --login` once in a terminal (this handles MFA). After that the **Sync Garmin**
  button works.

## Run

```bash
uvicorn app.main:app --reload      # http://localhost:8000
```

## How it fits together

| File | Role |
|---|---|
| `app/metrics.py` | Training load (Banister TRIMP), CTL/ATL/TSB, KPIs, the summary sent to Claude |
| `app/claude_cli.py` | Runs `claude -p --output-format stream-json` with built-in tools disabled. Only `healthdb` and Strava tools are allowed |
| `app/healthdb_mcp.py` | stdio MCP server: `query_data` (read-only, single SELECT), `get_metric_series`, `show_chart` |
| `app/insights.py` | Gets JSON insights from Claude via `--json-schema` and validates them with Pydantic |
| `app/main.py` | FastAPI routes; chat is streamed to the browser as server-sent events |

Set `CLAUDE_MODEL=sonnet` (or `opus`) in `.env` to choose the model. The default is your Claude Code default.

## Token safeguards

Only one Claude run happens at a time, and closing the page stops a chat run in progress. These limits apply,
and each can be changed in `.env`:

| Setting | Default | What happens |
|---|---|---|
| `CLAUDE_DAILY_RUN_LIMIT` | 30 | New runs are refused for the rest of the local day |
| `CLAUDE_MAX_TOOL_CALLS` | 15 | The run is stopped after passing this many tool calls |
| `CLAUDE_RUN_TIMEOUT_S` | 240 | The run is stopped after this many seconds |
| `CLAUDE_MAX_CHAT_TURNS` | 20 | That chat refuses new messages; start a new one |

`ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are removed from the environment passed to `claude`, so usage
can't move from your plan to an API bill. The header shows today's run count and tokens.
