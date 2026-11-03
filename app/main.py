"""FastAPI app: dashboard data, Garmin sync/import, insights and streaming chat.

    uvicorn app.main:app --reload    ->  http://localhost:8000
"""
import json
import uuid
from contextlib import aclosing

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import claude_cli, db, garmin_import, garmin_sync, insights, metrics

class RevalidatedStaticFiles(StaticFiles):
    """Make browsers re-check files, so a stale app.js can't bypass the client-side guards."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app = FastAPI(title="trAIn")
STATIC = db.ROOT / "static"
app.mount("/static", RevalidatedStaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/status")
async def status():
    conn = db.connect()
    counts = conn.execute(
        "SELECT (SELECT COUNT(*) FROM activities) acts, (SELECT COUNT(*) FROM daily) days, "
        "(SELECT COUNT(*) FROM activities WHERE source_id LIKE 'demo-%') demo"
    ).fetchone()
    strava = json.loads(claude_cli.STRAVA_CACHE.read_text()) if claude_cli.STRAVA_CACHE.exists() else None
    return {
        "claude": await claude_cli.auth_status(),
        "usage": claude_cli.usage(),
        "data": {"activities": counts["acts"], "days": counts["days"], "demo": counts["demo"] > 0,
                 "latest": db.latest_date(conn)},
        "strava": {"discovered": strava is not None, "tools": len(strava["strava_tools"]) if strava else 0},
    }


@app.get("/api/dashboard")
def dashboard(days: int = 90):
    conn = db.connect()
    if not conn.execute("SELECT 1 FROM daily UNION SELECT 1 FROM activities LIMIT 1").fetchone():
        return {"empty": True}
    frame = metrics.daily_frame(conn, max(14, min(days, 365)))
    acts = [dict(r) for r in conn.execute(
        "SELECT start_time, sport, name, distance_m, duration_s, avg_hr, elev_gain_m, training_effect "
        "FROM activities ORDER BY start_time DESC LIMIT 25")]
    return {
        "empty": False,
        "kpis": metrics.kpis(conn),
        "weekly": metrics.weekly_volume(conn, 16),
        "daily": frame,
        "activities": acts,
        "metrics": metrics.METRICS,
    }


@app.post("/api/chart")
def chart(spec: dict):
    return metrics.chart_data(db.connect(), spec)


class ImportRequest(BaseModel):
    path: str


@app.post("/api/garmin/sync")
async def garmin_sync_route():
    try:
        return await run_in_threadpool(garmin_sync.sync)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/garmin/import")
async def garmin_import_route(req: ImportRequest):
    try:
        return await run_in_threadpool(garmin_import.import_folder, req.path)
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@app.get("/api/insights")
def get_insights():
    return insights.latest() or {}


@app.post("/api/insights")
async def make_insights():
    try:
        return await insights.generate()
    except claude_cli.RunRejected as e:
        raise HTTPException(429, str(e)) from e
    except Exception as e:
        raise HTTPException(502, str(e)) from e


@app.post("/api/claude/discover")
async def discover():
    try:
        return await claude_cli.discover_tools()
    except claude_cli.RunRejected as e:
        raise HTTPException(429, str(e)) from e


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _status_for_tool(name: str) -> str:
    if name.startswith("mcp__healthdb__"):
        return {"query_data": "Querying your Garmin data…", "get_metric_series": "Pulling a metric trend…",
                "show_chart": "Drawing a chart…"}.get(name.split("__")[-1], "Using your Garmin data…")
    if "strava" in name.lower():
        return "Checking Strava…"
    return f"Using {name}…"


@app.post("/api/chat")
async def chat(req: ChatRequest):
    conn = db.connect()
    conv_id = req.conversation_id
    row = conn.execute("SELECT claude_session_id FROM conversations WHERE id = ?", (conv_id,)).fetchone() if conv_id else None
    if row is None:
        conv_id = uuid.uuid4().hex
        conn.execute("INSERT INTO conversations (id, title) VALUES (?, ?)", (conv_id, req.message[:80]))
        conn.commit()
    resume = row["claude_session_id"] if row else None

    def save_session(session_id: str | None):
        if session_id:
            conn.execute("UPDATE conversations SET claude_session_id = ? WHERE id = ?", (session_id, conv_id))
            conn.commit()

    async def stream():
        yield _sse("conversation", {"id": conv_id})
        streamed_text, need_gap = False, False
        try:
            async with aclosing(claude_cli.run(req.message, kind="chat", conversation_id=conv_id,
                                               resume=resume)) as events:
                async for ev in events:
                    kind = ev.get("type")
                    if kind == "system" and ev.get("subtype") == "init":
                        save_session(ev.get("session_id"))
                    elif kind == "stream_event":
                        inner = ev.get("event", {})
                        if inner.get("type") == "message_start" and streamed_text:
                            need_gap = True
                        elif inner.get("type") == "content_block_delta" and inner.get("delta", {}).get("type") == "text_delta":
                            text = inner["delta"]["text"]
                            if need_gap:
                                text, need_gap = "\n\n" + text, False
                            streamed_text = True
                            yield _sse("text", {"delta": text})
                    elif kind == "assistant":
                        for block in ev.get("message", {}).get("content", []):
                            if block.get("type") != "tool_use":
                                continue
                            name = block.get("name", "")
                            yield _sse("status", {"text": _status_for_tool(name)})
                            if name.endswith("__show_chart"):
                                try:
                                    yield _sse("chart", metrics.chart_data(db.connect(), block.get("input") or {}))
                                except Exception as e:
                                    yield _sse("status", {"text": f"Chart failed: {e}"})
                    elif kind == "result":
                        save_session(ev.get("session_id"))
                        if ev.get("is_error"):
                            yield _sse("error", {"message": ev.get("result") or ev.get("subtype")})
                        elif not streamed_text and ev.get("result"):
                            yield _sse("text", {"delta": ev["result"]})
                    elif kind == "error":
                        yield _sse("error", {"message": ev["message"]})
        except claude_cli.RunRejected as e:
            yield _sse("error", {"message": str(e)})
        yield _sse("done", {})

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
