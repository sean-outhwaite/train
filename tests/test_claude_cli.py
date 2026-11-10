"""Token safeguards in claude_cli. A broken one fails silently as extra usage, so each gets a test.

Runs use a fake `claude` script that prints stream-json, so no real Claude call is made."""
import asyncio
import json
import stat
import sys

import pytest

from app import claude_cli, db

FAKE_CLAUDE = """\
import json, os, sys, time
sys.stdin.read()
mode = os.environ.get("FAKE_MODE", "ok")
def emit(ev):
    print(json.dumps(ev), flush=True)
emit({"type": "env", "has_key": any(k in os.environ for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"))})
if mode == "tools":
    for i in range(int(os.environ["FAKE_TOOLS"])):
        emit({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": f"t{i}", "name": "mcp__healthdb__query_data"}]}})
if mode == "slow":
    time.sleep(10)
if mode == "crash":
    print("boom", file=sys.stderr)
    sys.exit(2)
emit({"type": "result", "is_error": False,
      "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 7}})
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(claude_cli, "CLAUDE", str(script))
    monkeypatch.setattr(claude_cli, "MODEL", None)
    monkeypatch.setattr(claude_cli, "DAILY_RUN_LIMIT", 100)
    monkeypatch.setattr(claude_cli, "MAX_TOOL_CALLS", 15)
    monkeypatch.setattr(claude_cli, "RUN_TIMEOUT_S", 30)
    return script


def collect(**kwargs) -> list[dict]:
    async def go():
        return [ev async for ev in claude_cli.run("hi", kind="chat", **kwargs)]
    return asyncio.run(go())


def last_run() -> dict:
    return dict(db.connect().execute("SELECT * FROM claude_runs ORDER BY id DESC LIMIT 1").fetchone())


# --- credentials -------------------------------------------------------------

def test_env_strips_api_credentials(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "x")
    monkeypatch.setenv("SOMETHING_ELSE", "kept")
    env = claude_cli._env()
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["SOMETHING_ELSE"] == "kept"


def test_run_subprocess_never_sees_api_key(fake_claude, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    events = collect()
    assert events[0] == {"type": "env", "has_key": False}


# --- limits before a run -----------------------------------------------------

def test_daily_limit(monkeypatch):
    monkeypatch.setattr(claude_cli, "DAILY_RUN_LIMIT", 2)
    db.start_run("chat")
    claude_cli._check_limits(None)
    db.start_run("insights")
    with pytest.raises(claude_cli.RunRejected, match="Daily limit"):
        claude_cli._check_limits(None)


def test_daily_limit_ignores_earlier_days(monkeypatch):
    monkeypatch.setattr(claude_cli, "DAILY_RUN_LIMIT", 1)
    conn = db.connect()
    conn.execute("INSERT INTO claude_runs (kind, started_at) VALUES ('chat', datetime('now', '-2 days'))")
    conn.commit()
    claude_cli._check_limits(None)


def test_chat_turn_limit_is_per_conversation(monkeypatch):
    monkeypatch.setattr(claude_cli, "DAILY_RUN_LIMIT", 100)
    monkeypatch.setattr(claude_cli, "MAX_CHAT_TURNS", 3)
    for _ in range(3):
        db.start_run("chat", "c1")
    with pytest.raises(claude_cli.RunRejected, match="3 messages"):
        claude_cli._check_limits("c1")
    claude_cli._check_limits("c2")


def test_one_run_at_a_time(monkeypatch):
    monkeypatch.setattr(claude_cli, "DAILY_RUN_LIMIT", 100)

    async def go():
        async with claude_cli._run_lock:
            with pytest.raises(claude_cli.RunRejected, match="already working"):
                claude_cli._check_limits(None)
    asyncio.run(go())


def test_rejected_run_does_not_start_or_count(fake_claude, monkeypatch):
    monkeypatch.setattr(claude_cli, "DAILY_RUN_LIMIT", 0)
    with pytest.raises(claude_cli.RunRejected):
        collect()
    assert db.usage_today()["runs"] == 0


# --- limits during a run -----------------------------------------------------

def test_successful_run_is_logged_with_tokens(fake_claude):
    events = collect()
    assert events[-1]["type"] == "result"
    run = last_run()
    assert run["status"] == "ok"
    assert run["input_tokens"] == 15  # cache reads count as input
    assert run["output_tokens"] == 7


def test_tool_call_limit_kills_run(fake_claude, monkeypatch):
    monkeypatch.setattr(claude_cli, "MAX_TOOL_CALLS", 2)
    monkeypatch.setenv("FAKE_MODE", "tools")
    monkeypatch.setenv("FAKE_TOOLS", "5")
    events = collect()
    assert events[-1]["type"] == "error"
    assert "2 tool calls" in events[-1]["message"]
    assert sum(e["type"] == "assistant" for e in events) == 3
    run = last_run()
    assert run["status"] == "tool_limit"
    assert len(json.loads(run["tools_json"])) == 3


def test_timeout_kills_run(fake_claude, monkeypatch):
    monkeypatch.setattr(claude_cli, "RUN_TIMEOUT_S", 1)
    monkeypatch.setenv("FAKE_MODE", "slow")
    events = collect()
    assert events[-1]["type"] == "error"
    assert "over 1 seconds" in events[-1]["message"]
    assert last_run()["status"] == "timeout"


def test_crash_reports_stderr(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "crash")
    events = collect()
    assert events[-1] == {"type": "error", "message": "boom"}
    assert last_run()["status"] == "error"


def test_abandoned_run_is_still_logged(fake_claude, monkeypatch):
    # The browser going away closes the generator early; the run must still be recorded.
    monkeypatch.setenv("FAKE_MODE", "slow")

    async def go():
        gen = claude_cli.run("hi", kind="chat")
        await gen.__anext__()
        await gen.aclose()
    asyncio.run(go())
    assert last_run()["status"] == "killed"


# --- command line ------------------------------------------------------------

def test_cli_args_allow_only_read_only_tools(monkeypatch):
    monkeypatch.setattr(claude_cli, "MODEL", None)
    monkeypatch.delenv("STRAVA_MCP_SERVER", raising=False)
    args = claude_cli._base_args(None, None, persist=False)
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--allowedTools") + 1:args.index("--system-prompt")] == ["mcp__healthdb"]
    assert "--no-session-persistence" in args
    assert not any("dangerously" in a or "bypass" in a.lower() for a in args)


def test_runs_start_outside_the_project():
    # Claude Code loads CLAUDE.md from the working directory and its parents.
    real_run_dir = claude_cli.Path.home() / ".cache" / "train"
    assert db.ROOT not in [real_run_dir, *real_run_dir.parents]
