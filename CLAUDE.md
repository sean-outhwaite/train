# trAIn

Local dashboard for personal Garmin data: FastAPI, SQLite and plain JS with Chart.js, with no build step.
Claude generates insights and answers chat questions. See README.md for setup.

## Commands

```bash
.venv/bin/uvicorn app.main:app --port 8000   # run the app at http://localhost:8000
.venv/bin/python -m app.garmin_sync          # sync Garmin (add --login the first time, for MFA)
.venv/bin/python -m app.garmin_import <dir>  # import an unzipped Garmin data export
.venv/bin/python -m app.seed                 # demo data, for trying the UI only
.venv/bin/python -m pytest                   # tests (pip install -r requirements-dev.txt first)
```

Tests run against a temporary database and a fake `claude` script. Keep test data made up, never real exports.

`--clear` on `app.seed` empties the whole `daily` table, including real data. Clear demo data before syncing.

## Rules

- **Strava:** never use the Strava API. Strava's API policy bans AI use of API data, so Strava is only reached
  live through the Strava MCP connector, in chat and insights. Don't store Strava data in the database. Saved
  insight reports are the one exception, because they can quote figures from it.
- **Claude access:** Claude runs through `claude -p` so usage stays on the Claude plan. Don't add the Anthropic
  API or an API key. Every call goes through `app/claude_cli.py` so the safeguards apply.
- **Run directory:** Claude runs start in `~/.cache/train`, outside the project, so this file doesn't reach
  the app's prompts. Keep that path fixed, because `--resume` depends on it.
- **Claude's data access:** database tools stay read-only, and chart metrics stay limited to `metrics.METRICS`.
  GPS routes stay in `data/tracks/`, not the database, so those tools can't read them.
- **Data:** a gap means not recorded, not incomplete data. Hide what's empty rather than showing blanks.
- **Charts:** no dual axes. Series with different units go in separate panels.
- **Privacy:** this repo is shared. Keep personal health details and habits out of code, docs, prompts and
  commit messages. Never commit `data/` or `.env`.
