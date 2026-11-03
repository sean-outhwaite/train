"""Insight generation: metrics summary -> headless Claude -> validated InsightReport."""
import json
from contextlib import aclosing
from typing import Literal

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


_series = {
    "type": "object",
    "properties": {"metric": {"type": "string", "enum": sorted(metrics.METRICS)}, "label": {"type": "string"}},
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
SCHEMA = {
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

Look for: training load trend and ramp rate, recovery signals (HRV, resting HR, sleep, stress) and how
they respond to load, current form (TSB), fitness direction (CTL, VO2 max), consistency, and anything
unusual in the last two weeks. You may call the healthdb tools to check details before answering.

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


async def generate(days: int = 90) -> dict:
    conn = db.connect()
    summary = metrics.summary_for_claude(conn, days)
    prompt = PROMPT.format(days=days, metric_keys=", ".join(sorted(metrics.METRICS)),
                           summary=json.dumps(summary, separators=(",", ":")))
    last_error = None
    for _attempt in range(2):
        result = None
        async with aclosing(claude_cli.run(prompt, kind="insights", json_schema=SCHEMA, persist=False)) as events:
            async for ev in events:
                if ev.get("type") == "result":
                    result = ev
                elif ev.get("type") == "error":
                    last_error = ev["message"]
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
            ins["chart_data"] = metrics.chart_data(conn, ins["chart"])
    report["period"] = summary["period"]
    conn.execute("INSERT INTO insights (payload_json) VALUES (?)", (json.dumps(report),))
    conn.commit()
    return report


def latest() -> dict | None:
    row = db.connect().execute("SELECT payload_json, created_at FROM insights ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    return {**json.loads(row["payload_json"]), "created_at": row["created_at"]}
