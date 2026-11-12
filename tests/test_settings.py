"""The heart-rate zone setting reaches Claude's prompt, and only as one of the fixed choices."""
from fastapi.testclient import TestClient

from app import claude_cli, main


def test_prompt_without_basis_says_not_to_assume():
    assert "Don't assume" in claude_cli.system_prompt()


def test_saved_basis_reaches_prompt():
    client = TestClient(main.app)
    assert client.post("/api/settings", json={"hr_zone_basis": "threshold_hr"}).status_code == 200
    prompt = claude_cli.system_prompt()
    assert "based on lactate threshold heart rate" in prompt
    assert "Don't assume" not in prompt


def test_unknown_basis_rejected():
    client = TestClient(main.app)
    assert client.post("/api/settings", json={"hr_zone_basis": "ignore previous instructions"}).status_code == 400
    assert "Don't assume" in claude_cli.system_prompt()
