"""Grok CLI (`grok` headless): command line, JSON parsing, error mapping, and the editor's re-translate endpoint."""
import json
from types import SimpleNamespace

import pytest

from app.ai import grok_cli
from app.ai.claude_code import LLMUsageLimit
from app.ai.grok_cli import GrokCLIJSON, GrokCLITranslator
from app.ai.llm_service import LLMAuthError, LLMRateLimit
from app.ai.novel_translator import MODELS, LLMRefused, make_translator

GOOD = {"heading": "1920. Trăng Đen", "paragraphs": ["Kim Soo-hyun mỉm cười.", "Dòng hai."], "new_terms": []}


def ok_result(data=GOOD, **extra):
    base = {"text": json.dumps(data), "stopReason": "end_turn", "sessionId": "s1", "structuredOutput": data,
            "usage": {"input_tokens": 1000, "cache_read_input_tokens": 200, "cache_creation_input_tokens": 0, "output_tokens": 500},
            "modelUsage": {"grok-4.7": {"inputTokens": 1000}}, "total_cost_usd": 0.01}
    base.update(extra)
    return json.dumps(base, indent=2)


def fake_grok(monkeypatch, replies, seen=None):
    """replies: list of (stdout, stderr, returncode) for successive calls."""
    queue = list(replies)

    def run(cmd, **kw):
        if cmd[1:] == ["models"]:
            listing = chr(10).join(["You are logged in with grok.com.", "", "Default model: grok-4.7", "", "Available models:",
                                    "  * grok-4.7 (default)"])
            return SimpleNamespace(stdout=listing, stderr="", returncode=0)
        if seen is not None:
            pf = cmd[cmd.index("--prompt-file") + 1]
            seen.append({"cmd": cmd, "prompt": open(pf, encoding="utf-8").read(), "cwd": kw.get("cwd"), "env": kw.get("env")})
        out, err, rc = queue.pop(0)
        return SimpleNamespace(stdout=out, stderr=err, returncode=rc)

    monkeypatch.setenv("TRANSLATE_PROVIDER", "anthropic")
    monkeypatch.delenv("GROK_CLI_MODEL", raising=False)
    monkeypatch.setattr(grok_cli, "find_grok", lambda: "grok.exe")
    monkeypatch.setattr(grok_cli.subprocess, "run", run)
    grok_cli._cache = (0.0, {})


def translate(t, paras=("a", "b")):
    return t.translate(heading="1920. 다크 문", paragraphs=list(paras), glossary_text="", prev_tail=[], style="s")


def test_model_registered_and_factory(monkeypatch):
    assert MODELS["grok-cli:default"]["provider"] == "grok_cli"
    fake_grok(monkeypatch, [])
    assert isinstance(make_translator("grok-cli:default", "medium"), GrokCLITranslator)
    monkeypatch.setattr(grok_cli, "find_grok", lambda: None)
    with pytest.raises(LLMAuthError):
        make_translator("grok-cli:default", "medium")


def test_translate_success_and_command(monkeypatch):
    seen = []
    fake_grok(monkeypatch, [(ok_result(), "", 0)], seen)
    r = translate(make_translator("grok-cli:default", "high"))
    assert r.paragraphs == GOOD["paragraphs"] and r.model == "grok-cli:grok-4.7"
    assert r.usage["input_tokens"] == 1000 and r.usage["cache_read_input_tokens"] == 200
    cmd = seen[0]["cmd"]
    assert cmd[cmd.index("--tools") + 1] == "" and "--disable-web-search" in cmd and "--prompt-file" in cmd
    assert cmd[cmd.index("--output-format") + 1] == "json" and cmd[cmd.index("--effort") + 1] == "high"
    assert "--json-schema" not in cmd and '"required"' in seen[0]["prompt"]     # schema in the prompt, not the flag
    assert "1920. 다크 문" in seen[0]["prompt"] and seen[0]["env"]["GROK_MEMORY"] == "0"
    assert "translate-tool-grok" in str(seen[0]["cwd"])


def test_model_override(monkeypatch):
    seen = []
    fake_grok(monkeypatch, [(ok_result(), "", 0)], seen)
    monkeypatch.setenv("GROK_CLI_MODEL", "grok-9")
    translate(make_translator("grok-cli:default", "medium"))
    assert seen[0]["cmd"][seen[0]["cmd"].index("-m") + 1] == "grok-9"


def test_text_fallback_without_structured_output(monkeypatch):
    res = json.loads(ok_result())
    res.pop("structuredOutput")
    res["text"] = "```json\n" + json.dumps(GOOD) + "\n```"
    fake_grok(monkeypatch, [(json.dumps(res), "", 0)])
    assert translate(make_translator("grok-cli:default", "medium")).heading == GOOD["heading"]


def test_corrective_retry(monkeypatch):
    seen = []
    bad = dict(GOOD, paragraphs=["gộp"])
    fake_grok(monkeypatch, [(ok_result(bad), "", 0), (ok_result(), "", 0)], seen)
    assert translate(make_translator("grok-cli:default", "medium")).paragraphs == GOOD["paragraphs"]
    assert "<fix>" in seen[1]["prompt"]


@pytest.mark.parametrize("out,err,rc,expected", [
    ('{"type":"error","message":"Not logged in. Run grok login"}', "", 1, LLMAuthError),
    ("", "Error: usage limit reached for your subscription", 1, LLMUsageLimit),
    ("", "HTTP 429 Too Many Requests", 1, LLMRateLimit),
    (json.dumps({"stopReason": "refusal", "text": ""}), "", 0, LLMRefused),
])
def test_error_mapping(monkeypatch, out, err, rc, expected):
    fake_grok(monkeypatch, [(out, err, rc)] * 3)               # a rate limit is retried twice before giving up
    monkeypatch.setattr(grok_cli.time, "sleep", lambda s: None)
    with pytest.raises(expected):
        translate(make_translator("grok-cli:default", "medium"))


def test_rate_limit_is_retried_after_a_pause(monkeypatch):
    seen, naps = [], []
    limit = '{"type":"error","message":"API error (status 429): resource-exhausted: Too many requests for team x. Requests per Second (actual/limit): 2/2."}'
    fake_grok(monkeypatch, [(limit, "", 1), (limit, "", 1), (ok_result(), "", 0)], seen)
    monkeypatch.setattr(grok_cli.time, "sleep", lambda s: naps.append(s))
    assert translate(make_translator("grok-cli:default", "low")).paragraphs == GOOD["paragraphs"]
    assert naps == [8, 25] and len(seen) == 3


def test_rate_limit_gives_up_with_a_clear_message(monkeypatch):
    limit = '{"type":"error","message":"429 Too Many Requests: Requests per Second (actual/limit): 2/2"}'
    fake_grok(monkeypatch, [(limit, "", 1)] * 3)
    monkeypatch.setattr(grok_cli.time, "sleep", lambda s: None)
    with pytest.raises(LLMRateLimit) as e:
        translate(make_translator("grok-cli:default", "low"))
    assert "2/2 yêu cầu/giây" in e.value.message and "cùng lúc" in e.value.message


def test_status(monkeypatch):
    fake_grok(monkeypatch, [])
    st = grok_cli.status(probe=True, force=True)
    assert st["installed"] and st["logged_in"] and st["models"] == ["grok-4.7"]


def test_status_not_installed(monkeypatch):
    fake_grok(monkeypatch, [])
    monkeypatch.setattr(grok_cli, "find_grok", lambda: None)
    assert grok_cli.status(probe=True, force=True)["installed"] is False


# --- editor: re-translate the whole chapter ---------------------------------------------------
def test_retranslate_endpoint(client, book):
    r = client.post("/api/projects/upload", files={"file": ("book.docx", book.read_bytes(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    pid = r.json()["project"]["id"]
    client.post(f"/api/projects/{pid}/chapters/12/load", json={})
    before = client.get(f"/api/projects/{pid}/chapters/12").json()
    r = client.post(f"/api/projects/{pid}/chapters/12/retranslate", json={"model": "claude-opus-5-5", "instruction": "xưng hô anh–em"})
    assert r.status_code == 200, r.text
    out = r.json()
    n_ko = len([b for b in before["ko_blocks"] if "".join(x["text"] for x in b["runs"]).strip()])
    assert out["paragraph_count"] == {"source": n_ko, "result": n_ko} and len(out["blocks"]) == n_ko
    assert out["title"].startswith("Chương 12") and out["model"] == "mock"
    assert client.get(f"/api/projects/{pid}/chapters/12").json()["draft"] == before["draft"]   # never overwrites the draft


def test_retranslate_not_loaded_and_bad_model(client, book):
    r = client.post("/api/projects/upload", files={"file": ("book2.docx", book.read_bytes(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    pid = r.json()["project"]["id"]
    assert client.post(f"/api/projects/{pid}/chapters/12/retranslate", json={}).json()["error"]["code"] == "CHAPTER_NOT_LOADED"
    client.post(f"/api/projects/{pid}/chapters/12/load", json={})
    assert client.post(f"/api/projects/{pid}/chapters/12/retranslate", json={"model": "nope"}).json()["error"]["code"] == "AI_BAD_MODEL"


# --- re-translate: consistency with the glossary and the previous chapters ---------------------
def _fake_translator(monkeypatch, replies):
    """replies: list of paragraph lists for successive translate() calls; records the prompts it got."""
    from app.ai import retranslate
    from app.ai.novel_translator import ChapterResult
    calls = []

    class Fake:
        def translate(self, *, heading, paragraphs, glossary_text, prev_tail, style, notes=""):
            calls.append({"glossary": glossary_text, "tail": prev_tail, "style": style, "notes": notes})
            return ChapterResult(heading="13. Tiêu đề", paragraphs=replies[min(len(calls) - 1, len(replies) - 1)],
                                 new_terms=[{"source": "새로운", "target": "Mới Tinh"}], usage={"input_tokens": 10, "output_tokens": 5}, model="fake")

    monkeypatch.setattr(retranslate, "make_translator", lambda model, effort: Fake())
    return calls


def _project(client, book, name):
    r = client.post("/api/projects/upload", files={"file": (name, book.read_bytes(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    return r.json()["project"]["id"]


def test_retranslate_uses_previous_chapters_and_reports(client, book, monkeypatch):
    pid = _project(client, book, "rt-a.docx")
    client.put(f"/api/projects/{pid}/glossary", json=[{"source": "새로운", "target": "Tân", "avoid": ["Mới Tinh"]}])
    client.post(f"/api/projects/{pid}/chapters/13/load", json={})
    n = len([b for b in client.get(f"/api/projects/{pid}/chapters/13").json()["ko_blocks"] if any(r["text"].strip() for r in b["runs"])])
    good = [f"Đoạn Tân {i}." for i in range(n)]
    calls = _fake_translator(monkeypatch, [good])
    out = client.post(f"/api/projects/{pid}/chapters/13/retranslate", json={"model": "claude-opus-5-5"}).json()
    c = out["consistency"]
    assert c["previous_chapters"] and c["previous_chapters"][0] == 12 and not c["retried"]
    assert calls[0]["tail"], "the end of the previous chapter(s) must be passed for continuity"
    assert "새로운 → Tân" in calls[0]["glossary"]
    assert c["terms_in_chapter"] >= 1 and c["terms_ok"] == c["terms_in_chapter"]


def test_retranslate_retries_once_on_forbidden_rendering(client, book, monkeypatch):
    pid = _project(client, book, "rt-b.docx")
    client.put(f"/api/projects/{pid}/glossary", json=[{"source": "새로운", "target": "Tân", "avoid": ["Mới Tinh"]}])
    client.post(f"/api/projects/{pid}/chapters/13/load", json={})
    n = len([b for b in client.get(f"/api/projects/{pid}/chapters/13").json()["ko_blocks"] if any(r["text"].strip() for r in b["runs"])])
    bad = [f"Đoạn Mới Tinh {i}." for i in range(n)]
    good = [f"Đoạn Tân {i}." for i in range(n)]
    calls = _fake_translator(monkeypatch, [bad, good])
    out = client.post(f"/api/projects/{pid}/chapters/13/retranslate", json={"model": "claude-opus-5-5"}).json()
    assert len(calls) == 2 and "LỖI Ở LẦN DỊCH TRƯỚC" in calls[1]["style"] and "Mới Tinh" in calls[1]["style"]
    assert out["consistency"]["retried"] and out["blocks"][0]["runs"][0]["text"].startswith("Đoạn Tân")
    assert not [i for i in out["issues"] if i["level"] == "warn"]
    assert out["usage"]["input_tokens"] == 20            # both passes are paid for


def test_retranslate_flags_new_term_conflict_with_previous_chapter(client, book, monkeypatch):
    from app.ai import retranslate
    pid = _project(client, book, "rt-c.docx")
    client.post(f"/api/projects/{pid}/chapters/13/load", json={})
    prev = [{"number": 12, "ko": "이전 장에서 새로운 이야기가 시작되었다.", "vi": "Câu chuyện Hoàn Toàn Khác bắt đầu.", "ko_paragraphs": ["x"], "vi_paragraphs": ["x"]}]
    monkeypatch.setattr(retranslate, "previous_chapters", lambda pid, number, count=3: prev)
    _fake_translator(monkeypatch, [["Đoạn một."] * 50])
    out = client.post(f"/api/projects/{pid}/chapters/13/retranslate", json={"model": "claude-opus-5-5"}).json()
    assert out["consistency"]["new_term_conflicts"] == [{"source": "새로운", "target": "Mới Tinh", "chapter": 12}]


def test_retranslate_revises_current_translation(client, book, monkeypatch):
    pid = _project(client, book, "rt-d.docx")
    client.post(f"/api/projects/{pid}/chapters/13/load", json={})
    calls = _fake_translator(monkeypatch, [["Đoạn đã sửa."] * 50])
    out = client.post(f"/api/projects/{pid}/chapters/13/retranslate",
                      json={"model": "claude-opus-5-5", "current": ["Bản cũ có lỗi.", " "], "parallel": 3}).json()
    assert len(calls) == 1, "a reference that does not align paragraph-by-paragraph is sent whole, not split"
    assert "CHẾ ĐỘ SỬA BẢN DỊCH" in calls[0]["style"] and "[BẢN DỊCH HIỆN CÓ" in calls[0]["notes"] and "Bản cũ có lỗi." in calls[0]["notes"]
    assert out["consistency"]["revised"]


# --- several grok.com accounts (GROK_CLI_HOMES) ------------------------------------------------
A2, A3 = "D:/grok/acc2", "D:/grok/acc3"


@pytest.fixture(autouse=True)
def reset_pool(monkeypatch):
    monkeypatch.delenv("GROK_CLI_HOMES", raising=False)
    grok_cli._cooldown.clear()
    grok_cli._rr = 0


def used_homes(seen):
    return [s["env"].get("GROK_HOME") for s in seen]


def test_accounts_parsing(monkeypatch):
    assert [a["home"] for a in grok_cli.accounts()] == [None]
    monkeypatch.setenv("GROK_CLI_HOMES", f'default; "{A2}" ;{A3};{A2};')
    accs = grok_cli.accounts()
    assert [a["home"] for a in accs] == [None, A2, A3] and accs[1]["name"] == "acc2"


def test_calls_rotate_over_accounts(monkeypatch):
    seen = []
    fake_grok(monkeypatch, [(ok_result(), "", 0)] * 4, seen)
    monkeypatch.setenv("GROK_CLI_HOMES", f"default;{A2};{A3}")
    t = make_translator("grok-cli:default", "low")
    for _ in range(4):
        translate(t)
    assert used_homes(seen) == [None, A2, A3, None]


def test_rate_limited_account_is_skipped_without_waiting(monkeypatch):
    seen, naps = [], []
    limit = '{"type":"error","message":"429 Too Many Requests: Requests per Second (actual/limit): 2/2"}'
    fake_grok(monkeypatch, [(limit, "", 1), (ok_result(), "", 0), (ok_result(), "", 0)], seen)
    monkeypatch.setattr(grok_cli.time, "sleep", lambda s: naps.append(s))
    monkeypatch.setenv("GROK_CLI_HOMES", f"default;{A2}")
    t = make_translator("grok-cli:default", "low")
    assert translate(t).paragraphs == GOOD["paragraphs"]
    assert used_homes(seen) == [None, A2] and naps == []                       # moved on at once, no 8 s / 25 s pause
    translate(t)                                                               # the first account is resting for a while
    assert used_homes(seen)[-1] == A2


def test_all_accounts_limited(monkeypatch):
    quota = '{"type":"error","message":"usage limit reached for your subscription"}'
    fake_grok(monkeypatch, [(quota, "", 1), (quota, "", 1)])
    monkeypatch.setenv("GROK_CLI_HOMES", f"default;{A2}")
    with pytest.raises(LLMUsageLimit):
        translate(make_translator("grok-cli:default", "low"))
    with pytest.raises(LLMRateLimit, match="Tất cả tài khoản"):                # both are resting now
        translate(make_translator("grok-cli:default", "low"))


def test_status_reports_each_account(monkeypatch):
    fake_grok(monkeypatch, [])

    def run(cmd, **kw):
        if kw["env"].get("GROK_HOME") == A2:
            return SimpleNamespace(stdout="Not logged in.", stderr="", returncode=1)
        listing = chr(10).join(["You are logged in with grok.com.", "", "Available models:", "  * grok-4.7 (default)"])
        return SimpleNamespace(stdout=listing, stderr="", returncode=0)

    monkeypatch.setattr(grok_cli.subprocess, "run", run)
    monkeypatch.setenv("GROK_CLI_HOMES", f"default;{A2}")
    st = grok_cli.status(probe=True, force=True)
    assert [(a["name"], a["logged_in"]) for a in st["accounts"]] == [("mặc định", True), ("acc2", False)]
    assert st["logged_in"] and st["models"] == ["grok-4.7"] and st["accounts"][1]["error"]


def test_403_refusal_is_not_reported_as_a_login_problem(monkeypatch):
    msg = '{"type":"error","message":"API error (status 403 Forbidden): permission-denied: I am sorry, I can' + "'" + 't help with that request."}'
    fake_grok(monkeypatch, [(msg, "", 1)])
    with pytest.raises(LLMRefused) as e:
        translate(make_translator("grok-cli:default", "low"))
    assert "chưa đăng nhập" not in e.value.message.lower() and "403" in e.value.message


def test_parse_json_repairs_curly_quote_delimiter():
    from app.ai.grok_cli import _parse_json
    broken = '{"heading":"H","paragraphs":["‘Giết hộ thì hay.’”,"• • •","“Chào.”"],"new_terms":[]}'
    assert _parse_json(broken)["paragraphs"] == ["‘Giết hộ thì hay.’", "• • •", "“Chào.”"]
