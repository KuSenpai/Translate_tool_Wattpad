"""Antigravity (Gemini via `agy` headless): stream-json I/O, model choice, error mapping, corrective retry."""
import json
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from app.ai import antigravity
from app.ai.antigravity import AntigravityJSON, AntigravityTranslator, build_message, pick_model, rules
from app.ai.claude_code import LLMUsageLimit
from app.ai.llm_service import LLMAuthError, LLMRateLimit
from app.ai.novel_translator import SCHEMA, LLMRefused, make_translator

MODELS_OUT = "Available models:\n  gemini-3.8-pro-high (default)\n  gemini-3.8-pro-low\n  gemini-3.8-flash-high\n" \
             "  gemini-3.8-flash-lite\n  gemini-3.5-pro-high\n  claude-sonnet-4-6\n"


def result_line(**res):
    base = {"conversation_id": "c1", "status": "SUCCESS", "response": "", "duration_seconds": 3.2, "num_turns": 1,
            "usage": {"input_tokens": 1000, "output_tokens": 500, "thinking_tokens": 100, "cache_read_tokens": 0}}
    base.update(res)
    return "\n".join([json.dumps({"event": "init", "conversation_id": "c1", "init": {"tools": ["run_command"]}}),
                      json.dumps({"event": "step_update", "step_update": {"state": "DONE"}}),
                      json.dumps({"event": "result", "result": base})])


def fake_agy(monkeypatch, replies, seen=None):
    """replies: list of (stdout, stderr, returncode) for successive chat calls."""
    queue = list(replies)

    def run(cmd, input=None, **kw):
        if cmd[1:] == ["models"]:
            return SimpleNamespace(stdout=MODELS_OUT, stderr="", returncode=0)
        if seen is not None:
            seen.append({"cmd": cmd, "input": input, "cwd": kw.get("cwd")})
        out, err, rc = queue.pop(0)
        return SimpleNamespace(stdout=out, stderr=err, returncode=rc)

    monkeypatch.setenv("TRANSLATE_PROVIDER", "anthropic")
    no_model_override(monkeypatch)
    monkeypatch.setattr(antigravity, "find_agy", lambda: "agy.exe")
    monkeypatch.setattr(antigravity.subprocess, "run", run)
    monkeypatch.setattr(antigravity, "_run_agy", lambda cmd, stdin, cwd, timeout: run(cmd, input=stdin, cwd=cwd))
    antigravity._cache = (0.0, {})


def no_model_override(monkeypatch):
    for k in ("ANTIGRAVITY_MODEL_PRO", "ANTIGRAVITY_MODEL_FLASH"):    # the real .env may set them
        monkeypatch.delenv(k, raising=False)


GOOD = {"heading": "1920. Trăng Đen", "paragraphs": ["Kim Soo-hyun mỉm cười.", "Dòng hai."], "new_terms": []}


def test_pick_model(monkeypatch):
    no_model_override(monkeypatch)
    avail = antigravity._SLUG.findall(MODELS_OUT)
    assert pick_model("pro", avail) == "gemini-3.8-pro-high"
    assert pick_model("pro", avail, "low") == "gemini-3.8-pro-low" and pick_model("pro", avail, "medium") == "gemini-3.8-pro-high"
    assert pick_model("flash", avail) == "gemini-3.8-flash-high"     # never the lite one
    assert pick_model("pro", []) is None                               # → CLI default model
    monkeypatch.setenv("ANTIGRAVITY_MODEL_PRO", "gemini-x")
    assert pick_model("pro", avail) == "gemini-x"


def test_translate_success_and_command(monkeypatch, tmp_path):
    seen = []
    fake_agy(monkeypatch, [(result_line(structured_output=GOOD), "", 0)], seen)
    t = make_translator("antigravity:pro", "high")
    assert isinstance(t, AntigravityTranslator)
    r = t.translate(heading="1920. 다크 문", paragraphs=["김수현은 웃었다.", "둘째 줄."], glossary_text="- 김수현 → Kim Soo-hyun",
                    prev_tail=[], style="Không dùng mày–tao.")
    assert r.paragraphs == GOOD["paragraphs"] and r.model == "antigravity:gemini-3.8-pro-high"
    assert r.usage["output_tokens"] == 600                               # thinking counted with output
    call = seen[0]
    cmd = call["cmd"]
    assert cmd[cmd.index("--input-format") + 1] == "stream-json" and cmd[cmd.index("--output-format") + 1] == "stream-json"
    assert cmd[cmd.index("--model") + 1] == "gemini-3.8-pro-high" and "--effort" not in cmd   # slug carries the effort
    assert "--dangerously-skip-permissions" not in cmd and "--sandbox" in cmd
    msg = json.loads(call["input"])                                       # prompt goes in via stdin, not argv
    content = msg["message"]["content"]
    assert msg["event"] == "user" and "김수현은 웃었다." in content and "N = 2 đoạn" in content
    assert "# QUY TẮC BẮT BUỘC" in content and "Không dùng mày–tao." in content and "{style}" not in content
    wd = call["cwd"]
    assert (wd / "GEMINI.md").read_text(encoding="utf-8").startswith("# Workspace chỉ dùng để dịch truyện")
    schema = json.loads((wd / cmd[cmd.index("--json-schema") + 1]).read_text(encoding="utf-8"))
    assert schema["required"] == ["heading", "paragraphs", "new_terms"]


def test_response_text_fallback(monkeypatch):
    fake_agy(monkeypatch, [(result_line(response="```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"), "", 0)])
    data, _, _ = AntigravityJSON("antigravity:flash").call(system="s", user="u", schema=SCHEMA)
    assert data["heading"] == "1920. Trăng Đen"


def test_corrective_retry_on_paragraph_mismatch(monkeypatch):
    seen = []
    merged = dict(GOOD, paragraphs=["Kim Soo-hyun mỉm cười. Dòng hai."])
    fake_agy(monkeypatch, [(result_line(structured_output=merged), "", 0), (result_line(structured_output=GOOD), "", 0)], seen)
    r = make_translator("antigravity:pro", "medium").translate(heading="1920. 다크 문", paragraphs=["김수현은 웃었다.", "둘째 줄."],
                                                               glossary_text="", prev_tail=[], style="")
    assert len(r.paragraphs) == 2 and len(seen) == 2
    assert "Bạn trả về 1 đoạn nhưng đầu vào có 2 đoạn" in json.loads(seen[1]["input"])["message"]["content"]
    assert r.usage["input_tokens"] == 2000


def test_fix_pass_is_one_short_call(monkeypatch):
    seen = []
    merged = dict(GOOD, paragraphs=["Kim Soo-hyun mỉm cười. Dòng hai."])
    fake_agy(monkeypatch, [(result_line(structured_output=merged), "", 0)], seen)
    t = make_translator("antigravity:pro", "medium")
    t.max_attempts, t.timeout_min = 1, 8                                  # what retranslate sets for its corrective pass
    t.translate(heading="1920. 다크 문", paragraphs=["김수현은 웃었다.", "둘째 줄."], glossary_text="", prev_tail=[], style="")
    cmd = seen[0]["cmd"]
    assert len(seen) == 1 and cmd[cmd.index("--print-timeout") + 1] == "8m"


def test_run_agy_returns_at_result_even_if_agy_hangs(tmp_path):
    result = json.dumps({"event": "result", "result": {"status": "SUCCESS"}})
    hang = f"import sys,time; sys.stdin.read(); print({result!r}, flush=True); time.sleep(60)"   # result printed, never exits
    t = time.time()
    out = antigravity._run_agy([sys.executable, "-c", hang], "x" * 200000, tmp_path, timeout=30)
    assert time.time() - t < 15 and antigravity._result_event(out.stdout)["status"] == "SUCCESS"
    with pytest.raises(subprocess.TimeoutExpired):
        antigravity._run_agy([sys.executable, "-c", "import time; time.sleep(60)"], "", tmp_path, timeout=1)


def test_interrupted_stream_is_asked_again_once(monkeypatch):
    cut = result_line(status="ERROR", error="The stream was interrupted. Please continue the task you were working on.",
                      response='{"heading": "Chương 1", "paragraphs": ["nửa')
    seen = []
    fake_agy(monkeypatch, [(cut, "", 1), (result_line(structured_output=GOOD), "", 0)], seen)
    data, _, _ = AntigravityJSON("antigravity:pro").call(system="s", user="u", schema=SCHEMA)
    assert data == GOOD and len(seen) == 2
    fake_agy(monkeypatch, [(cut, "", 1), (cut, "", 1)])                     # twice in a row: reported, not looped
    with pytest.raises(Exception) as e:
        AntigravityJSON("antigravity:pro").call(system="s", user="u", schema=SCHEMA)
    assert e.value.code == "ANTIGRAVITY_ERROR"


@pytest.mark.parametrize("out,err,rc,exc,code", [
    (result_line(status="ERROR", error={"message": "RESOURCE_EXHAUSTED: quota exceeded"}), "", 1, LLMUsageLimit, "AI_USAGE_LIMIT"),
    ("", "Error: not signed in. Run agy to authenticate.", 1, LLMAuthError, "ANTIGRAVITY_LOGIN"),
    (result_line(status="ERROR", error={"message": "Response blocked: PROHIBITED_CONTENT"}), "", 1, LLMRefused, "AI_REFUSED"),
    (result_line(status="ERROR", error={"message": "503 model overloaded"}), "", 1, LLMRateLimit, "AI_RATE_LIMIT"),
    ("", "Unknown model: gemini-9", 2, Exception, "AI_BAD_MODEL"),
])
def test_errors(monkeypatch, out, err, rc, exc, code):
    fake_agy(monkeypatch, [(out, err, rc)])
    with pytest.raises(exc) as e:
        AntigravityJSON("antigravity:pro").call(system="s", user="u", schema=SCHEMA)
    assert e.value.code == code


def test_missing_cli(monkeypatch):
    monkeypatch.setattr(antigravity, "find_agy", lambda: None)
    with pytest.raises(LLMAuthError) as e:
        AntigravityJSON("antigravity:pro")
    assert e.value.code == "ANTIGRAVITY_MISSING" and "install.ps1" in e.value.message


def test_rules_and_message():
    r = rules("- Văn phong A")
    assert "- Văn phong A" in r and "KHÔNG DÙNG CÔNG CỤ" in r
    m = build_message("1. 제목", ["가", "나", "다"], "- a → b", ["đoạn cuối"], notes="Kim là nam chính")
    assert m.index("GHI CHÚ TRUYỆN") < m.index("THUẬT NGỮ") < m.index("ĐOẠN CUỐI CHƯƠNG TRƯỚC") < m.index("CHƯƠNG CẦN DỊCH")
    assert m.rstrip().endswith("không lời dẫn.") and "ĐÚNG 3 phần tử" in m


def test_status_does_not_probe_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr(antigravity, "find_agy", lambda: "agy.exe")
    monkeypatch.setattr(antigravity.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or SimpleNamespace(
        stdout=MODELS_OUT, stderr="", returncode=0))
    antigravity._cache = (0.0, {})
    assert antigravity.status()["checked"] is False and not calls       # page load: no `agy` run (it may open a browser)
    st = antigravity.status(probe=True)
    assert st["checked"] and "gemini-3.8-pro-high" in st["models"] and len(calls) == 1


def test_info_and_check_endpoints(client, monkeypatch):
    fake_agy(monkeypatch, [])
    from app.ai import claude_code
    monkeypatch.setattr(claude_code, "status", lambda force=False: {"installed": False})
    info =client.get("/api/translate/info").json()
    ag = [m for m in info["models"] if m["provider"] == "antigravity"]
    assert {m["id"] for m in ag} == {"antigravity:pro", "antigravity:flash"} and all(m["subscription"] for m in ag)
    st = client.get("/api/translate/antigravity").json()
    assert st["picked"] == {"pro": "gemini-3.8-pro-high", "flash": "gemini-3.8-flash-high"}


def test_quota_pauses_job(client, monkeypatch):
    from app.ai import translation_jobs

    class Limited:
        model = "antigravity:pro"

        def translate(self, **kw):
            raise LLMUsageLimit("Đã hết hạn mức Gemini của Antigravity: RESOURCE_EXHAUSTED")

    monkeypatch.setattr(translation_jobs, "make_translator", lambda m, e: Limited())
    meta = client.post("/api/translate/analyze", files={"file": ("ag.txt", "1. 제목\n김수현은 웃었다.\n".encode())}).json()
    job = client.post("/api/translate/jobs", json={"sid": meta["sid"], "model": "antigravity:pro"}).json()
    for _ in range(100):
        j = client.get(f"/api/translate/jobs/{job['id']}").json()
        if not j["running"] and j["status"] != "running":
            break
        time.sleep(0.1)
    assert j["status"] == "paused" and "hạn mức" in j["message"] and j["counts"] == {"pending": 1}
