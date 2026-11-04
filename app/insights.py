"""Insight generation: metrics summary -> headless Claude -> validated InsightReport."""
import json
from contextlib import aclosing
from typing import AsyncIterator, Literal

from pydantic import BaseModel, ValidationError

from . import claude_cli, db, metrics


class SeriesRef(BaseModel):
    metric: str
    label: str | None = None


class ChartSpec(BaseModel):
    title: str
    type: Literal["line", "bar"] = "line"
    series: list[SeriesRef]
    days: int = 90


class Insight(BaseModel):
    title: str
    body: str
    category: Literal["training", "recovery", "sleep", "fitness", "health"]
    severity: Literal["positive", "info", "watch", "concern"]
    evidence: str
    chart: ChartSpec | None = None


class InsightReport(BaseModel):
    headline: str
    insights: list[Insight]


def schema(metric_keys: list[str]) -> dict:
    """Report schema; charts can only use metrics that have data."""
    _series = {
        "type": "object",
        "properties": {"metric": {"type": "string", "enum": sorted(metric_keys)}, "label": {"type": "string"}},
        "required": ["metric"],
        "additionalProperties": False,
    }
    _chart = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "type": {"type": "string", "enum": ["line", "bar"]},
            "series": {"type": "array", "items": _series, "minItems": 1, "maxItems": 3},
            "days": {"type": "integer"},
        },
        "required": ["title", "type", "series", "days"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "headline": {"type": "string"},
            "insights": {
                "type": "array",
                "minItems": 3,
                "maxItems": 7,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                        "category": {"type": "string", "enum": ["training", "recovery", "sleep", "fitness", "health"]},
                        "severity": {"type": "string", "enum": ["positive", "info", "watch", "concern"]},
                        "evidence": {"type": "string"},
                        "chart": _chart,
                    },
                    "required": ["title", "body", "category", "severity", "evidence"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["headline", "insights"],
        "additionalProperties": False,
    }


PROMPT = """Review my last {days} days of Garmin data (summary below) and give me 3-7 insights.

Look for: training load trend and ramp rate, current form (TSB), fitness direction (CTL, VO2 max),
consistency, and anything unusual in the last two weeks. Include recovery signals and how they respond to
load, using only the metrics that were recorded. You may call the healthdb tools to check details first.

If Strava tools are available, use get_athlete_best_efforts and get_athlete_zones where they add something
Garmin lacks. Per-activity Strava tools cost one call each, so use them for two or three notable sessions at
most, never to go through every activity. Stay under 10 tool calls in total. Say in the evidence when a number
comes from Strava.

data_coverage shows how complete each watch metric is. A gap means not recorded, not incomplete data, a zero
or a problem. Don't write insights about metrics under not_recorded, don't advise me to record more data, and
be cautious with metrics that only cover part of the period.

For each insight:
- title: short and specific
- body: 2-4 sentences - what is happening, why it matters, one concrete suggestion
- evidence: the specific numbers it rests on
- severity: positive | info | watch | concern
- chart: include when a picture helps, using only these metric keys: {metric_keys}

The final answer must be the JSON object only.

Summary (weekly rollups, last 14 days, recent activities):
{summary}"""


def _extract(result_event: dict) -> dict:
    if isinstance(result_event.get("structured_output"), dict):
        return result_event["structured_output"]
    text = result_event.get("result") or ""
    start, end = text.find("{"), text.rfind("}")
    if start < 0:
        raise ValueError("no JSON object in Claude's reply")
    return json.loads(text[start:end + 1])


async def generate(days: int = 90) -> AsyncIterator[tuple[str, dict]]:
    """Yield ("status", {"text": ...}) progress events, then ("report", report) once it's saved."""
    yield "status", {"text": f"Summarising your last {days} days…"}
    conn = db.connect()
    summary = metrics.summary_for_claude(conn, days)
    metric_keys = sorted(metrics.available_metrics(conn, days))
    prompt = PROMPT.format(days=days, metric_keys=", ".join(metric_keys),
                           summary=json.dumps(summary, separators=(",", ":")))
    last_error = None
    for attempt in range(2):
        if attempt:
            yield "status", {"text": "Claude's reply didn't fit the format, trying again…"}
        result = None
        async with aclosing(claude_cli.run(prompt, kind="insights", json_schema=schema(metric_keys),
                                               persist=False)) as events:
            async for ev in events:
                if ev.get("type") == "result":
                    result = ev
                elif ev.get("type") == "error":
                    last_error = ev["message"]
                elif ev.get("type") == "system" and ev.get("subtype") == "init":
                    yield "status", {"text": "Claude is reading your summary…"}
                elif ev.get("type") == "assistant":
                    for block in ev.get("message", {}).get("content", []):
                        if block.get("type") == "tool_use":
                            yield "status", {"text": claude_cli.tool_status(block.get("name", ""))}
        if result is None:
            raise RuntimeError(last_error or "Claude returned no result")
        if result.get("is_error"):
            raise RuntimeError(result.get("result") or result.get("subtype") or "Claude reported an error")
        try:
            report = InsightReport.model_validate(_extract(result)).model_dump()
            break
        except (ValueError, ValidationError) as e:
            last_error = str(e)
    else:
        raise RuntimeError(f"Insights did not match the expected format: {last_error}")

    for ins in report["insights"]:
        if ins.get("chart"):
            data = metrics.chart_data(conn, ins["chart"])
            if data["series"]:
                ins["chart_data"] = data
    report["period"] = summary["period"]
    conn.execute("INSERT INTO insights (payload_json) VALUES (?)", (json.dumps(report),))
    conn.commit()
    yield "report", report


def latest() -> dict | None:
    row = db.connect().execute("SELECT payload_json, created_at FROM insights ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    return {**json.loads(row["payload_json"]), "created_at": row["created_at"]}
