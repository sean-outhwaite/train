"""Shared fixtures. Every test gets its own empty database, so nothing touches data/."""
import pytest

from app import claude_cli, db


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "health.db")
    # Fixed at import from the real data/ and ~/.cache paths, so point them at the temp folder too.
    monkeypatch.setattr(claude_cli, "MCP_CONFIG", data_dir / "mcp.json")
    monkeypatch.setattr(claude_cli, "STRAVA_CACHE", data_dir / "strava_tools.json")
    monkeypatch.setattr(claude_cli, "RUN_DIR", tmp_path / "run")
    return data_dir


@pytest.fixture
def conn():
    c = db.connect()
    yield c
    c.close()
