"""Run Claude through the user's logged-in Claude Code CLI (`claude -p`), so usage counts
against their Claude plan instead of an API key.

Claude gets no built-in tools (`--tools ""`) - only the local healthdb MCP server and, if
present, the user's claude.ai Strava connector. Both are read-only.
"""
import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import AsyncIterator

from dotenv import load_dotenv

from . import db

load_dotenv(db.ROOT / ".env")

CLAUDE = os.getenv("CLAUDE_BIN") or shutil.which("claude") or "claude"
MODEL = os.getenv("CLAUDE_MODEL")  # optional, e.g. "sonnet"; default = your Claude Code default
MCP_CONFIG = db.DATA_DIR / "mcp.json"
STRAVA_CACHE = db.DATA_DIR / "strava_tools.json"
# Claude Code loads CLAUDE.md from the working directory and its parents, so runs must start outside
# the project. The path has to stay fixed: --resume finds sessions by working directory.
RUN_DIR = Path.home() / ".cache" / "train"

# Token safeguards. Each can be overridden in .env.
DAILY_RUN_LIMIT = int(os.getenv("CLAUDE_DAILY_RUN_LIMIT", "30"))     # all runs, per local day
MAX_TOOL_CALLS = int(os.getenv("CLAUDE_MAX_TOOL_CALLS", "15"))       # per run
RUN_TIMEOUT_S = int(os.getenv("CLAUDE_RUN_TIMEOUT_S", "240"))        # per run
MAX_CHAT_TURNS = int(os.getenv("CLAUDE_MAX_CHAT_TURNS", "20"))       # per conversation
DISCOVER_RETRY_MINUTES = 60

_run_lock = asyncio.Lock()  # one Claude run at a time


class RunRejected(Exception):
    """A safeguard refused to start a run. The message is shown to the user."""


def _env() -> dict:
    """Keep API credentials away from the CLI so usage can't move from the plan to an API bill."""
    return {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}


def usage() -> dict:
    return {**db.usage_today(), "limit": DAILY_RUN_LIMIT}


def _check_limits(conversation_id: str | None) -> None:
    if _run_lock.locked():
        raise RunRejected("Claude is already working on something. Wait for it to finish.")
    if db.usage_today()["runs"] >= DAILY_RUN_LIMIT:
        raise RunRejected(f"Daily limit of {DAILY_RUN_LIMIT} Claude runs reached. "
                          "Raise CLAUDE_DAILY_RUN_LIMIT in .env to allow more.")
    if conversation_id and db.conversation_turns(conversation_id) >= MAX_CHAT_TURNS:
        raise RunRejected(f"This chat has reached {MAX_CHAT_TURNS} messages, and each one resends the whole "
                          "conversation. Start a new chat to keep usage down.")

SYSTEM_PROMPT = """You are a sports scientist and health coach analysing one person's own data.

Data sources:
- healthdb tools: the user's Garmin history (activities + daily wellness) in a local SQLite database.
  Prefer get_metric_series for trends and query_data for anything specific.
- Strava tools (if available): live read-only access to the user's Strava account. Use them for
  things Garmin lacks - segments, kudos, Strava's Relative Effort / Fitness & Freshness, gear, or
  activities not recorded on Garmin. Never claim Strava data came from Garmin or vice versa.

Some metrics may be partial or absent. A gap means not recorded, not incomplete data, a zero or a problem.
Call get_data_coverage before answering questions about wellness trends, and say plainly when something
can't be answered from what's recorded. Don't advise the user to record more data unless they ask.

How to answer:
- Ground every claim in numbers you actually retrieved; say when something is an estimate.
  Training load / CTL / ATL / TSB are computed by this app from heart rate (Banister TRIMP), not by Garmin.
- When a trend or comparison would be clearer as a picture, call show_chart (1-3 series).
- Be concise and practical. Use short paragraphs or bullets. Metric units.
- You are not a doctor. For symptoms or anything that looks medically concerning, say so plainly and
  suggest seeing a professional."""


def write_mcp_config() -> None:
    db.DATA_DIR.mkdir(exist_ok=True)
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    script = db.ROOT / "app" / "healthdb_mcp.py"
    MCP_CONFIG.write_text(json.dumps(
        {"mcpServers": {"healthdb": {"type": "stdio", "command": sys.executable, "args": [str(script)]}}},
        indent=2,
    ))


async def auth_status() -> dict:
    if not shutil.which(CLAUDE):
        return {"ok": False, "message": "Claude Code CLI not found. Install it from https://claude.com/claude-code"}
    proc = await asyncio.create_subprocess_exec(
        CLAUDE, "auth", "status", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=_env()
    )
    out, _ = await proc.communicate()
    try:
        status = json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "message": "Could not read `claude auth status`."}
    if not status.get("loggedIn"):
        return {"ok": False, "message": "Claude Code is logged out. In a terminal run `claude`, then `/login`."}
    return {"ok": True, "method": status.get("authMethod")}


def _strava_allow() -> list[str]:
    """Allow-rules for the Strava connector, discovered once by discover_tools()."""
    if os.getenv("STRAVA_MCP_SERVER"):
        return [f"mcp__{os.getenv('STRAVA_MCP_SERVER')}"]
    if STRAVA_CACHE.exists():
        return json.loads(STRAVA_CACHE.read_text()).get("allow", [])
    return []


def _base_args(json_schema: dict | None, resume: str | None, persist: bool) -> list[str]:
    args = [
        CLAUDE, "-p",
        "--output-format", "stream-json", "--verbose", "--include-partial-messages",
        "--mcp-config", str(MCP_CONFIG),
        "--tools", "",
        "--allowedTools", "mcp__healthdb", *_strava_allow(),
        "--system-prompt", SYSTEM_PROMPT,
    ]
    if MODEL:
        args += ["--model", MODEL]
    if json_schema:
        args += ["--json-schema", json.dumps(json_schema)]
    if resume:
        args += ["--resume", resume]
    if not persist:
        args.append("--no-session-persistence")
    return args


async def run(prompt: str, *, kind: str, conversation_id: str | None = None, resume: str | None = None,
              json_schema: dict | None = None, persist: bool = True) -> AsyncIterator[dict]:
    """Yield each stream-json event from one `claude -p` run. The prompt goes in on stdin.

    Raises RunRejected before starting if a safeguard says no. A run that passes its tool-call
    or time limit is killed and ends with an `error` event. Callers should wrap this in
    contextlib.aclosing so an abandoned run is killed straight away.
    """
    _check_limits(conversation_id)
    async with _run_lock:
        write_mcp_config()
        run_id = db.start_run(kind, conversation_id)
        status, run_usage, tool_calls = "killed", None, 0
        deadline = time.monotonic() + RUN_TIMEOUT_S
        proc = await asyncio.create_subprocess_exec(
            *_base_args(json_schema, resume, persist),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=RUN_DIR,
            limit=16 * 1024 * 1024, env=_env(),
        )
        proc.stdin.write(prompt.encode())
        await proc.stdin.drain()
        proc.stdin.close()
        try:
            while True:
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), deadline - time.monotonic())
                except TimeoutError:
                    status = "timeout"
                    yield {"type": "error", "message": f"Stopped: Claude ran for over {RUN_TIMEOUT_S} seconds."}
                    return
                if not line:
                    break
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("type") == "result":
                    run_usage = ev.get("usage")
                    status = "error" if ev.get("is_error") else "ok"
                yield ev
                if ev.get("type") == "assistant":
                    tool_calls += sum(b.get("type") == "tool_use" for b in ev.get("message", {}).get("content", []))
                    if tool_calls > MAX_TOOL_CALLS:
                        status = "tool_limit"
                        yield {"type": "error", "message": f"Stopped: Claude passed the limit of {MAX_TOOL_CALLS} "
                                                           "tool calls for one run."}
                        return
            await proc.wait()
            if proc.returncode:
                status = "error"
                err = (await proc.stderr.read()).decode(errors="replace").strip()
                yield {"type": "error", "message": err or f"claude exited with code {proc.returncode}"}
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            db.finish_run(run_id, status, run_usage)


async def discover_tools() -> dict:
    """Start a session just far enough to read its init event, then stop it.

    Records which Strava connector tools exist so they can be allow-listed.
    """
    if db.recent_discover_attempt(DISCOVER_RETRY_MINUTES):
        return {"ok": False, "message": f"Discovery already tried in the last {DISCOVER_RETRY_MINUTES} minutes."}
    _check_limits(None)
    write_mcp_config()
    run_id = db.start_run("discover")
    proc = await asyncio.create_subprocess_exec(
        CLAUDE, "-p", "--output-format", "stream-json", "--verbose", "--mcp-config", str(MCP_CONFIG),
        "--tools", "", "--no-session-persistence", "--max-turns", "1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=RUN_DIR, limit=16 * 1024 * 1024, env=_env(),
    )
    proc.stdin.write(b"Reply with OK.")
    proc.stdin.close()
    init = None
    try:
        async for line in proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                init = ev
                break
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        db.finish_run(run_id, "ok" if init else "error")
    if not init:
        err = (await proc.stderr.read()).decode(errors="replace").strip()
        return {"ok": False, "message": err or "No init event from claude"}
    tools = init.get("tools", [])
    strava = [t for t in tools if "strava" in t.lower()]
    servers = sorted({t.split("__")[1] for t in strava if t.count("__") >= 2})
    result = {
        "ok": True,
        "healthdb": [t for t in tools if t.startswith("mcp__healthdb__")],
        "strava_tools": strava,
        "allow": [f"mcp__{s}" for s in servers],
        "mcp_servers": init.get("mcp_servers", []),
    }
    STRAVA_CACHE.write_text(json.dumps(result, indent=2))
    return result
