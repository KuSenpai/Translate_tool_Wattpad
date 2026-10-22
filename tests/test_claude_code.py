"""Claude Code (subscription) provider: `claude -p` output handling, with subprocess faked."""
import json
import time
from types import SimpleNamespace

import pytest

from app.ai import claude_code
from app.ai.claude_code import ClaudeCodeJSON, LLMUsageLimit
from app.ai.llm_service import LLMAuthError
from app.ai.novel_translator import SCHEMA, LLMRefused


def fake_run(payload, seen=None):
    def run(cmd, input=None, env=None, **kw):
        if seen is not None:
            seen.update(cmd=cmd, input=input, env=env)
        if cmd[1:3] == ["auth", "status"]:
            return SimpleNamespace(stdout=json.dumps({"loggedIn": True, "authMethod": "claude.ai"}), stderr="", returncode=0)
        return SimpleNamespace(stdout=json.dumps(payload), stderr="", returncode=0)
    return run


def client(monkeypatch, payload, seen=None):
    monkeypatch.setattr(claude_code, "find_claude", lambda: "claude.exe")
    monkeypatch.setattr(claude_code.subprocess, "run", fake_run(payload, seen))
    claude_code._status_cache = (0.0, {})
    return ClaudeCodeJSON("claude-code:opus", "medium")


OK = {"type": "result", "subtype": "success", "is_error": False, "result": "",
      "structured_output": {"heading": "1920. Trăng Rằm Đen", "paragraphs": ["Một"], "new_terms": []},
      "usage": {"input_tokens": 12, "output_tokens": 34}, "modelUsage": {"claude-opus-5-5": {}}, "stop_reason": "end_turn"}


def test_success_and_command(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-leak")
    seen = {}
    c = client(monkeypatch, OK, seen)
    data, usage, model = c.call(system="SYS", user="<chapter>김수현</chapter>", schema=SCHEMA)
    assert data["paragraphs"] == ["Một"] and usage["output_tokens"] == 34 and model == "claude-opus-5-5"
    cmd = seen["cmd"]
    assert cmd[:3] == ["claude.exe", "-p", "--output-format"] and "--json-schema" in cmd and "--system-prompt" in cmd
    assert cmd[cmd.index("--tools") + 1] == "" and cmd[cmd.index("--model") + 1] == "claude-opus-5-5"
    assert seen["input"] == "<chapter>김수현</chapter>"                  # prompt via stdin, not the command line
    assert "ANTHROPIC_API_KEY" not in seen["env"]                       # → subscription, never API billing


def test_result_text_json_fallback(monkeypatch):
    payload = dict(OK, structured_output=None, result='```json\n{"heading": "h", "paragraphs": ["x"], "new_terms": []}\n```')
    data, _, _ = client(monkeypatch, payload).call(system="s", user="u", schema=SCHEMA)
    assert data["paragraphs"] == ["x"]


@pytest.mark.parametrize("payload,exc,code", [
    ({"is_error": True, "result": "Claude AI usage limit reached|1790000000"}, LLMUsageLimit, "AI_USAGE_LIMIT"),
    ({"is_error": True, "result": "You've hit your limit · resets 5pm"}, LLMUsageLimit, "AI_USAGE_LIMIT"),
    ({"is_error": True, "result": "You've hit your session limit · resets 8:30pm (Asia/Bangkok)", "api_error_status": 429},
     LLMUsageLimit, "AI_USAGE_LIMIT"),
    ({"is_error": True, "result": "Not logged in · Please run /login"}, LLMAuthError, "CLAUDE_CODE_LOGIN"),
    ({"is_error": False, "result": "", "stop_reason": "refusal"}, LLMRefused, "AI_REFUSED"),
    ({"is_error": True, "result": "API Error: 400 Claude Code 2.1.150 does not support this model; Run 'claude update'"},
     LLMAuthError, "CLAUDE_CODE_UPDATE"),
])
def test_errors(monkeypatch, payload, exc, code):
    with pytest.raises(exc) as e:
        client(monkeypatch, payload).call(system="s", user="u", schema=SCHEMA)
    assert e.value.code == code


def test_not_logged_in_blocks_start(monkeypatch):
    monkeypatch.setattr(claude_code, "find_claude", lambda: "claude.exe")
    monkeypatch.setattr(claude_code.subprocess, "run", lambda cmd, **kw: SimpleNamespace(
        stdout=json.dumps({"loggedIn": False, "authMethod": "none"}), stderr="", returncode=0))
    claude_code._status_cache = (0.0, {})
    with pytest.raises(LLMAuthError) as e:
        ClaudeCodeJSON("claude-code:sonnet")
    assert "claude auth login --claudeai" in e.value.message


def test_usage_limit_pauses_job(client, monkeypatch, tmp_path):
    """Running out of subscription usage pauses the job; the chapter stays pending (not failed)."""
    from app.ai import novel_translator, translation_jobs

    class Limited:
        model = "claude-code:opus"

        def translate(self, **kw):
            raise LLMUsageLimit("Đã hết lượt dùng của gói Claude: usage limit reached")

    monkeypatch.setattr(translation_jobs, "make_translator", lambda m, e: Limited())
    meta = client.post("/api/translate/analyze", files={"file": ("cc.txt", "1. 제목\n김수현은 웃었다.\n둘째 줄.\n".encode())}).json()
    job = client.post("/api/translate/jobs", json={"sid": meta["sid"], "model": "claude-code:opus"}).json()
    for _ in range(100):
        j = client.get(f"/api/translate/jobs/{job['id']}").json()
        if not j["running"] and j["status"] != "running":
            break
        time.sleep(0.1)
    assert j["status"] == "paused" and "gói" in j["message"]
    assert j["counts"] == {"pending": 1} and j["cost_usd"] == 0


def test_reset_delay():
    from datetime import datetime
    from app.ai.translation_jobs import reset_delay
    now = datetime(2026, 10, 10, 13, 0).timestamp()
    assert reset_delay("Individual quota reached. Resets in 3h27m28s.", now) == 3 * 3600 + 27 * 60 + 28 + 120
    assert reset_delay("Claude AI usage limit reached|%d" % (now + 600), now) == 600 + 120
    assert reset_delay("You've hit your limit · resets 5pm (Asia/Saigon)", now) == 4 * 3600 + 120
    assert reset_delay("You've hit your limit · resets 9am", now) == 20 * 3600 + 120     # tomorrow morning
    assert reset_delay("limit reached", now) == 30 * 60                                  # no time given: try again later


def test_usage_limit_auto_resumes(client, monkeypatch):
    """Out of usage → paused with a resume time → resumes by itself and finishes."""
    from app.ai import translation_jobs
    from app.ai.novel_translator import ChapterResult
    calls = []

    class LimitedOnce:
        model = "claude-code:opus"

        def translate(self, *, paragraphs, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise LLMUsageLimit("Đã hết lượt dùng của gói Claude: You've hit your limit · resets 5pm")
            return ChapterResult(heading="1. Tiêu đề", paragraphs=["x"] * len(paragraphs), usage={})

    monkeypatch.setattr(translation_jobs, "make_translator", lambda m, e: LimitedOnce())
    monkeypatch.setattr(translation_jobs, "reset_delay", lambda text: 1.0)
    meta = client.post("/api/translate/analyze", files={"file": ("ar.txt", "1. 제목\n김수현은 웃었다.\n".encode())}).json()
    job = client.post("/api/translate/jobs", json={"sid": meta["sid"], "model": "claude-code:opus"}).json()
    seen = set()
    for _ in range(100):
        j = client.get(f"/api/translate/jobs/{job['id']}").json()
        seen.add(j["status"])
        if j["status"] == "paused":
            assert "tự động tiếp tục lúc" in j["message"]
        if j["status"] == "done":
            break
        time.sleep(0.1)
    assert "paused" in seen and j["status"] == "done" and j["counts"] == {"done": 1} and len(calls) == 2
