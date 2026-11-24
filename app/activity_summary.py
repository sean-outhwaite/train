"""One activity -> a short written summary from Claude, streamed as text. Not saved."""
import json

from . import db, metrics

PROMPT = """Summarise this one activity of mine (details below) in under 150 words.

Cover what kind of session it was, how it went (pacing, heart rate, and splits or laps if they show a
pattern), the conditions where they mattered, and one takeaway. Recorded weather is included where known:
allow for heat and humidity when reading heart rate. Use only the fields below. A missing field means it
wasn't recorded, so don't mention it.

You may make up to 3 healthdb calls to compare with my similar recent activities (same sport, similar
distance) or to see how rested I was beforehand. Don't use Strava or the training plan, and don't draw charts.

Write in plain prose or a few short bullets, without a heading.

Activity:
{activity}"""

# Already given to Claude by other fields, or only useful to the UI.
_SKIP = {"has_route", "raw_json"}


def prompt(source_id: str) -> str | None:
    """The prompt for one activity, or None if it doesn't exist."""
    conn = db.connect()
    try:
        detail = metrics.activity_detail(conn, source_id)
        if detail is None:
            return None
        extras = metrics.activity_extras(conn, source_id)
    finally:
        conn.close()
    activity = {k: v for k, v in metrics._present(detail).items() if k not in _SKIP and v not in ("", [])}
    activity.update({k: v for k, v in extras.items() if v})
    return PROMPT.format(activity=json.dumps(activity, separators=(",", ":")))
