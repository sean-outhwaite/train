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
from typing import AsyncIterator

from . import db

CLAUDE = os.getenv("CLAUDE_BIN") or shutil.which("claude") or "claude"
MODEL = os.getenv("CLAUDE_MODEL")  # optional, e.g. "sonnet"; default = your Claude Code default
MCP_CONFIG = db.DATA_DIR / "mcp.json"
STRAVA_CACHE = db.DATA_DIR / "strava_tools.json"

SYSTEM_PROMPT = """You are a sports scientist and health coach analysing one person's own data.

Data sources:
- healthdb tools: the user's Garmin history (activities + daily wellness) in a local SQLite database.
  Prefer get_metric_series for trends and query_data for anything specific.
- Strava tools (if available): live read-only access to the user's Strava account. Use them for
  things Garmin lacks - segments, kudos, Strava's Relative Effort / Fitness & Freshness, gear, or
  activities not recorded on Garmin. Never claim Strava data came from Garmin or vice versa.

How to answer:
- Ground every claim in numbers you actually retrieved; say when something is an estimate.
  Training load / CTL / ATL / TSB are computed by this app from heart rate (Banister TRIMP), not by Garmin.
- When a trend or comparison would be clearer as a picture, call show_chart (1-3 series).
- Be concise and practical. Use short paragraphs or bullets. Metric units.
- You are not a doctor. For symptoms or anything that looks medically concerning, say so plainly and
  suggest seeing a professional."""


def write_mcp_config() -> None:
    db.DATA_DIR.mkdir(exist_ok=True)
    script = db.ROOT / "app" / "healthdb_mcp.py"
    MCP_CONFIG.write_text(json.dumps(
        {"mcpServers": {"healthdb": {"type": "stdio", "command": sys.executable, "args": [str(script)]}}},
        indent=2,
    ))


async def auth_status() -> dict:
    if not shutil.which(CLAUDE):
        return {"ok": False, "message": "Claude Code CLI not found. Install it from https://claude.com/claude-code"}
    proc = await asyncio.create_subprocess_exec(
        CLAUDE, "auth", "status", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
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


async def run(prompt: str, *, resume: str | None = None, json_schema: dict | None = None,
              persist: bool = True) -> AsyncIterator[dict]:
    """Yield each stream-json event from one `claude -p` run. The prompt goes in on stdin."""
    write_mcp_config()
    proc = await asyncio.create_subprocess_exec(
        *_base_args(json_schema, resume, persist),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=db.DATA_DIR,  # keeps any project CLAUDE.md out of the context
        limit=16 * 1024 * 1024,
    )
    proc.stdin.write(prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()
    try:
        async for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
        await proc.wait()
        if proc.returncode:
            err = (await proc.stderr.read()).decode(errors="replace").strip()
            yield {"type": "error", "message": err or f"claude exited with code {proc.returncode}"}
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def discover_tools() -> dict:
    """Start a session just far enough to read its init event, then stop it.

    Records which Strava connector tools exist so they can be allow-listed.
    """
    write_mcp_config()
    proc = await asyncio.create_subprocess_exec(
        CLAUDE, "-p", "--output-format", "stream-json", "--verbose", "--mcp-config", str(MCP_CONFIG),
        "--tools", "", "--no-session-persistence", "--max-turns", "1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=db.DATA_DIR, limit=16 * 1024 * 1024,
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
