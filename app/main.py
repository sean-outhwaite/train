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
                 "latest": db.latest_date(conn), "synced_at": db.get_meta(conn, "garmin_synced_at")},
        "strava": {"discovered": strava is not None, "tools": len(strava["strava_tools"]) if strava else 0},
        "settings": _settings(conn),
    }


class SettingsRequest(BaseModel):
    # Each field is optional: only the ones sent are changed. An empty string clears a setting.
    hr_zone_basis: str | None = None
    training_plan: str | None = None


@app.post("/api/settings")
def save_settings(req: SettingsRequest):
    if req.hr_zone_basis and req.hr_zone_basis not in claude_cli.HR_ZONE_BASES:
        raise HTTPException(400, "Unknown heart-rate zone basis.")
    plan = None
    if req.training_plan:
        try:
            plan = claude_cli.parse_plan_link(req.training_plan)
        except ValueError as e:
            raise HTTPException(400, str(e))
    conn = db.connect()
    if req.hr_zone_basis is not None:
        db.set_meta(conn, "hr_zone_basis", req.hr_zone_basis)
    if req.training_plan is not None:
        db.set_meta(conn, "training_plan_doc", plan or "")
    conn.commit()
    return _settings(conn)


def _settings(conn) -> dict:
    plan = db.get_meta(conn, "training_plan_doc")
    return {"hr_zone_basis": db.get_meta(conn, "hr_zone_basis") or "",
            "training_plan": f"https://claude.ai/artifact/{plan}" if plan else ""}


@app.get("/api/dashboard")
def dashboard(days: int = 90):
    conn = db.connect()
    if not conn.execute("SELECT 1 FROM daily UNION SELECT 1 FROM activities LIMIT 1").fetchone():
        return {"empty": True}
    frame = metrics.daily_frame(conn, max(14, min(days, 365)))
    acts = [dict(r) for r in conn.execute(
        "SELECT source_id, start_time, sport, name, distance_m, duration_s, avg_hr, elev_gain_m, training_effect "
        "FROM activities ORDER BY start_time DESC LIMIT 200")]
    return {
        "empty": False,
        "kpis": metrics.kpis(conn),
        "readiness": metrics.readiness_latest(conn),
        "benchmarks": metrics.benchmarks(conn),
        "gear": metrics.gear(conn),
        "year": metrics.year_stats(conn),
        "weekly": metrics.weekly_volume(conn, 16),
        "daily": frame,
        "activities": acts,
        "metrics": metrics.METRICS,
    }


@app.get("/api/activities/{source_id}")
def activity(source_id: str):
    detail = metrics.activity_detail(db.connect(), source_id)
    if detail is None:
        raise HTTPException(404, "Activity not found")
    return detail


@app.get("/api/activities/{source_id}/extras")
async def activity_extras_route(source_id: str):
    """Laps and weather. Older Garmin activities fetch them from Garmin the first time."""
    conn = db.connect()
    if metrics.activity_detail(conn, source_id) is None:
        raise HTTPException(404, "Activity not found")
    if source_id.startswith("garmin-"):
        try:
            await run_in_threadpool(garmin_sync.activity_extras, source_id)
        except Exception:
            pass  # offline or not logged in: show what's stored
    return metrics.activity_extras(db.connect(), source_id)


@app.get("/api/activities/{source_id}/track")
def activity_track(source_id: str):
    detail = metrics.activity_detail(db.connect(), source_id)
    if detail is None or not detail["has_route"]:
        raise HTTPException(404, "No route for this activity")
    try:
        return garmin_sync.track(source_id)
    except Exception as e:
        raise HTTPException(502, str(e)) from e


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


def _sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _sse_response(events) -> StreamingResponse:
    return StreamingResponse(events, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/insights")
async def make_insights():
    async def stream():
        try:
            async with aclosing(insights.generate()) as events:
                async for event, data in events:
                    yield _sse(event, data)
        except Exception as e:  # includes RunRejected; the message is shown to the user
            yield _sse("error", {"message": str(e)})
        yield _sse("done", {})

    return _sse_response(stream())


@app.post("/api/claude/discover")
async def discover():
    try:
        return await claude_cli.discover_tools()
    except claude_cli.RunRejected as e:
        raise HTTPException(429, str(e)) from e


class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


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
                            yield _sse("status", {"text": claude_cli.tool_status(name)})
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

    return _sse_response(stream())
