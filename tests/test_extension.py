"""End-to-end test of the browser-extension publishing path.

Real Chromium with the real `extension/` scripts injected (manifest patched out: content.js + page_hook.js),
the real app served over HTTP, and the fake Wattpad site from tests/fake_wattpad.py in its "modern" flow, which
mirrors the real site: /myworks/<story> lists the parts (its "+ New Part" opens nothing), the editor has the
parts menu with "New Part", and Publish asks for the browser's own confirm().
"""
import json
import socket
import threading
import time
from urllib.parse import quote

import httpx
import pytest
import uvicorn

from tests.fake_wattpad import FakeWattpad

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(scope="module")
def env(tmp_root):
    from app import config
    from app.main import app
    fake = FakeWattpad()
    fake.flow = "modern"
    saved = config.WATTPAD_BASE_URL, config.WATTPAD_MODE
    config.WATTPAD_BASE_URL, config.WATTPAD_MODE = fake.base, "extension"

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    app_url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            httpx.get(app_url + "/api/ai/status", timeout=1)
            break
        except httpx.HTTPError:
            time.sleep(0.1)

    # Chromium here cannot load unpacked extensions from the command line (it hangs even with an empty
    # manifest), so the real extension scripts are injected into every page (page_hook.js is the one the manifest
    # runs in the page's own world; init scripts run there too) and chrome.runtime is replaced by a bridge that
    # does exactly what extension/background.js does: call /api/ext/* on the app with the extension's headers.
    version = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))["version"]
    routes = {"config": ("GET", "/api/ext/config"), "task": ("GET", "/api/ext/task"),
              "stories": ("POST", "/api/ext/stories"), "diag": ("POST", "/api/ext/diag")}

    def bridge(raw: str) -> str:
        msg = json.loads(raw)
        method, path = routes.get(msg["type"], ("POST", f"/api/ext/task/{msg.get('id')}/event"))
        if msg["type"] == "task":
            path += "?tab=" + quote(msg.get("tab") or "")
        try:
            r = httpx.request(method, app_url + path, headers={"X-NT-Extension": "1", "X-NT-Version": version},
                              json=msg.get("data") if method == "POST" else None, timeout=10)
            body = r.json()
            return json.dumps({"ok": True, "data": body} if r.status_code < 400 else
                              {"ok": False, "error": body.get("error", {}).get("message", str(r.status_code))})
        except Exception as e:
            return json.dumps({"ok": False, "error": str(e)})

    shim = """window.chrome = { runtime: {
        sendMessage: (msg, cb) => { window.__ntBridge(JSON.stringify(msg)).then((r) => cb && cb(JSON.parse(r))); },
        onMessage: { addListener() {} }, lastError: null } };"""
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    ctx = pw.chromium.launch_persistent_context(str(tmp_root / "ext_profile"), headless=True)
    ctx.expose_function("__ntBridge", bridge)
    ctx.add_init_script(path=str(ROOT / "extension" / "page_hook.js"))
    ctx.add_init_script(shim)
    ctx.add_init_script(path=str(ROOT / "extension" / "content.js"))
    page = ctx.new_page()
    page.goto(fake.base + "/dev-login")          # the user is logged in to Wattpad in their browser
    yield {"fake": fake, "app": app_url, "page": page, "ctx": ctx}
    ctx.close()
    pw.stop()
    server.should_exit = True
    fake.close()
    config.WATTPAD_BASE_URL, config.WATTPAD_MODE = saved


@pytest.fixture
def fake(env):
    """The fake account, wiped clean for each test."""
    env["fake"].reset()
    yield env["fake"]
    env["fake"].reset()


@pytest.fixture
def fast_batch(monkeypatch):
    from app.publishing import publish_service
    monkeypatch.setattr(publish_service, "BATCH_DELAY", 1)
    monkeypatch.setattr(publish_service, "BATCH_COUNTDOWN", 1)


def wait_job(client, job_id, page, timeout=90):
    # page.wait_for_timeout (not time.sleep) keeps Playwright dispatching the page -> Python bridge calls
    end = time.time() + timeout
    while time.time() < end:
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["state"] != "running":
            return j
        page.wait_for_timeout(500)
    raise AssertionError("job did not finish: " + json.dumps(j, ensure_ascii=False))


def upload(client, book, name):
    with open(book, "rb") as f:
        return client.post("/api/projects/upload", files={"file": (name, f)}).json()["project"]["id"]


def ready(client, book, number, name):
    pid = upload(client, book, name)
    client.post(f"/api/projects/{pid}/chapters/{number}/load", json={})
    client.post(f"/api/projects/{pid}/chapters/{number}/status", json={"event": "review"})
    return pid


def publish_one(client, pid, number, story, title, mode="publish"):
    return client.post(f"/api/projects/{pid}/chapters/{number}/publish",
                       json={"story_id": story, "title": title, "mode": mode, "confirm": True}).json()


def status_of(client, pid, number):
    return client.get(f"/api/projects/{pid}/chapters/{number}").json()["status"]


def start_batch(client, pid, story, chapters, mode="publish", title=""):
    return client.post(f"/api/projects/{pid}/publish-batch",
                       json={"story_id": story, "story_title": title, "chapters": chapters, "mode": mode, "confirm": True})


def wait_batch(client, page, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        b = client.get("/api/wattpad/batch").json()["batch"]
        if b["state"] != "running":
            return b
        page.wait_for_timeout(500)
    raise AssertionError("batch did not finish: " + json.dumps(b, ensure_ascii=False))


# ---------------------------------------------------------------------------------------------------------
def test_ext_api_rejects_web_pages(env):
    r = httpx.get(env["app"] + "/api/ext/task")
    assert r.status_code == 403
    r = httpx.get(env["app"] + "/api/ext/task", headers={"X-NT-Extension": "1", "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_extension_sends_story_list(env, client, fake):
    env["page"].goto(fake.base + "/myworks")
    end = time.time() + 30
    while time.time() < end:
        st = client.get("/api/wattpad/status").json()
        if {"111", "222", "333"} <= {s["id"] for s in st["stories"]}:
            break
        env["page"].wait_for_timeout(500)
    titles = {s["id"]: s["title"] for s in st["stories"]}
    assert titles["111"] == "Truyện Thử Nghiệm" and titles["222"] == "Second Story"
    assert st["mode"] == "extension" and st["extension_connected"]
    assert st["extension_version"] == st["extension_expected"]        # the tool can tell a stale extension apart


@pytest.mark.parametrize("confirm_style", ["native", "layer", "two_step"])
def test_publish_from_latest_published_chapter(env, client, book, fake, confirm_style):
    """The route done by hand: story page -> latest chapter (published) -> parts menu -> New Part -> fill ->
    Publish -> confirmation (the browser's own confirm(), or an in-page popup, one or two steps)."""
    page = env["page"]
    fake.confirm_style = confirm_style
    old_id = fake.existing_part("111")
    pid = ready(client, book, 12, f"ext-route-{confirm_style}.docx")
    before = set(fake.parts)
    job = publish_one(client, pid, 12, "111", "Chương 12: Gặp gỡ")
    assert job["open_url"] == fake.base + "/myworks/111"
    page.goto(job["open_url"])                    # what the app's window.open does
    j = wait_job(client, job["id"], page)
    assert j["state"] == "done", j
    (part_id,) = set(fake.parts) - before
    part = fake.parts[part_id]
    assert part["published"] and part["title"] == "Chương 12: Gặp gỡ"
    assert "<b>in đậm</b>" in part["html"] and "Hết chương 12." in part["text"]
    assert fake.old[old_id]["text"] == fake.OLD_TEXT and fake.old[old_id]["title"] == "Chương cũ"   # untouched
    assert fake.trap_clicks == 0                  # the story page's "+ New Part" opens nothing: never pressed
    assert fake.order["111"][-1] == part_id
    ch = client.get(f"/api/projects/{pid}/chapters/12").json()
    assert ch["status"] == "PUBLISHED"
    assert ch["publish_history"][-1]["part_id"] == part_id and ch["publish_history"][-1]["docx"].endswith("Chapter_12.docx")


def test_publish_fills_the_empty_latest_draft(env, client, book, fake):
    """When the newest chapter is an empty draft (e.g. left by an earlier attempt) it is filled, not duplicated."""
    page = env["page"]
    blank = fake.add_blank_draft("222")
    pid = ready(client, book, 11, "ext-blank-draft.docx")
    before = set(fake.parts)
    job = publish_one(client, pid, 11, "222", "Chương 11")
    page.goto(job["open_url"])
    j = wait_job(client, job["id"], page)
    assert j["state"] == "done", j
    assert set(fake.parts) == before                            # no new part was created
    assert fake.old[blank]["title"] == "Chương 11" and fake.old[blank]["published"] and "Hết chương 11." in fake.old[blank]["text"]
    assert fake.trap_clicks == 0
    assert client.get(f"/api/projects/{pid}/chapters/11").json()["publish_history"][-1]["part_id"] == blank


def test_title_reaches_wattpads_own_model(env, client, book, fake):
    """Wattpad keeps the title in its own model (key/blur events), so text that only sits in the page used to be
    saved and published as "Untitled Part N"."""
    page = env["page"]
    fake.title_model = True
    blank = fake.add_blank_draft("222")
    pid = ready(client, book, 11, "ext-title-model.docx")
    job = publish_one(client, pid, 11, "222", "Chương 11: Tiêu đề thật")
    page.goto(job["open_url"])
    j = wait_job(client, job["id"], page)
    assert j["state"] == "done", j
    assert fake.old[blank]["published"] and fake.old[blank]["title"] == "Chương 11: Tiêu đề thật"


def test_title_not_saved_stops_before_publish(env, client, book, fake):
    page = env["page"]
    fake.title_locked = True
    blank = fake.add_blank_draft("222")
    pid = ready(client, book, 11, "ext-title-locked.docx")
    job = publish_one(client, pid, 11, "222", "Chương 11")
    page.goto(job["open_url"])
    j = wait_job(client, job["id"], page)
    assert j["state"] == "error" and j["error_code"] == "WATTPAD_TITLE_NOT_SAVED" and "Untitled Part" in j["message"], j
    assert not fake.old[blank]["published"]                   # never published under the wrong title


def test_latest_draft_with_text_is_never_overwritten(env, client, book, fake):
    page = env["page"]
    fake.existing_part("333")
    mine = fake.add_old("333", "2278. Bản nháp dở dang", "Tác giả đang viết dở chương này.", published=False)
    pid = ready(client, book, 10, "ext-real-draft.docx")
    before = set(fake.parts)
    job = publish_one(client, pid, 10, "333", "Chương 10")
    page.goto(job["open_url"])
    j = wait_job(client, job["id"], page)
    assert j["state"] == "error" and j["error_code"] == "WATTPAD_LATEST_DRAFT_NOT_EMPTY", j
    assert fake.old[mine]["text"] == "Tác giả đang viết dở chương này." and not fake.old[mine]["published"]
    assert set(fake.parts) == before and fake.trap_clicks == 0
    assert status_of(client, pid, 10) == "READY_TO_PUBLISH"


def test_starting_on_an_older_chapter_goes_to_the_latest(env, client, book, fake):
    """The tab sits on some older chapter's editor: the extension must move to the newest chapter first."""
    page = env["page"]
    first = fake.existing_part("111")
    fake.add_old("111", "Chương cũ 2", "Chương cũ thứ hai.", published=True)
    pid = ready(client, book, 13, "ext-older-chapter.docx")
    before = set(fake.parts)
    job = publish_one(client, pid, 13, "111", "Chương 13")
    page.goto(f"{fake.base}/myworks/111/write/{first}")        # not the story page: an old chapter's editor
    j = wait_job(client, job["id"], page)
    assert j["state"] == "done", j
    (part_id,) = set(fake.parts) - before
    assert fake.parts[part_id]["published"] and fake.old[first]["text"] == fake.OLD_TEXT
    assert fake.order["111"][-1] == part_id


def test_extension_draft_mode(env, client, book, fake):
    page = env["page"]
    fake.existing_part("222")
    pid = ready(client, book, 11, "ext-book2.docx")
    before = set(fake.parts)
    job = publish_one(client, pid, 11, "222", "Chương 11", mode="draft")
    page.goto(job["open_url"])
    j = wait_job(client, job["id"], page)
    assert j["state"] == "done", j
    (part_id,) = set(fake.parts) - before
    assert not fake.parts[part_id]["published"] and fake.parts[part_id]["title"] == "Chương 11"
    assert client.get(f"/api/projects/{pid}/chapters/11").json()["status"] == "READY_TO_PUBLISH"


def test_cancel_from_app(env, client, book, fake):
    pid = ready(client, book, 10, "ext-book3.docx")
    job = publish_one(client, pid, 10, "111", "Chương 10")
    j = client.post(f"/api/jobs/{job['id']}/cancel").json()          # tab never opened: user cancels
    assert j["state"] == "error" and j["error_code"] == "CANCELLED"
    assert client.get(f"/api/projects/{pid}/chapters/10").json()["status"] == "READY_TO_PUBLISH"
    assert httpx.get(env["app"] + "/api/ext/task", headers={"X-NT-Extension": "1"}).json() == {"task": None}


def test_cancel_in_tool_stops_extension_before_publish_click(env, client, book, fake):
    page = env["page"]
    fake.existing_part("111")
    pid = ready(client, book, 13, "ext-cancel-late.docx")
    before = set(fake.parts)
    job = publish_one(client, pid, 13, "111", "Chương 13")
    page.goto(job["open_url"])
    end = time.time() + 60
    while time.time() < end and client.get(f"/api/jobs/{job['id']}").json()["message"] != "Đã điền và lưu.":
        page.wait_for_timeout(300)
    assert client.get(f"/api/jobs/{job['id']}").json()["message"] == "Đã điền và lưu."
    client.post(f"/api/jobs/{job['id']}/cancel")             # the extension is now in its countdown
    page.wait_for_timeout(9000)
    (part_id,) = set(fake.parts) - before
    assert fake.parts[part_id]["title"] == "Chương 13" and not fake.parts[part_id]["published"]


def test_two_tabs_do_not_both_run_the_task(env, client, book, fake):
    page = env["page"]
    fake.existing_part("222")
    other = env["ctx"].new_page()                            # a second Wattpad tab, e.g. a forgotten My Works tab
    other.goto(fake.base + "/myworks/222")
    pid = ready(client, book, 15, "ext-two-tabs.docx")
    before = set(fake.parts)
    job = publish_one(client, pid, 15, "222", "Chương 15")
    page.goto(job["open_url"])
    j = wait_job(client, job["id"], page)
    other.close()
    assert j["state"] == "done", j
    assert len(set(fake.parts) - before) == 1                # one part, not two
    assert fake.trap_clicks == 0


def test_new_part_button_that_never_becomes_visible(env, client, book, fake):
    """In a background tab Wattpad's menu transitions freeze, so the editor's New Part button stays
    visibility:hidden even with the menu open; the extension must still manage (the button itself works)."""
    page = env["page"]
    fake.frozen_menu = True
    old_id = fake.existing_part("111")
    pid = ready(client, book, 13, "ext-frozen-menu.docx")
    before = set(fake.parts)
    job = publish_one(client, pid, 13, "111", "Chương 13", mode="draft")
    page.goto(job["open_url"])
    j = wait_job(client, job["id"], page)
    assert j["state"] == "done", j
    (part_id,) = set(fake.parts) - before
    assert fake.parts[part_id]["title"] == "Chương 13" and fake.old[old_id]["text"] == fake.OLD_TEXT


def test_page_hook_only_answers_dialogs_while_publishing(env, fake):
    """Outside a Publish click the browser's own dialogs reach the user untouched."""
    page = env["page"]
    page.goto(fake.base + "/myworks/111")
    seen = []
    page.once("dialog", lambda d: (seen.append(d.message), d.dismiss()))
    assert page.evaluate("confirm('Really?')") is False and seen == ["Really?"]       # dismissed by the "user"
    page.evaluate("document.documentElement.setAttribute('data-nt-auto-confirm', '1')")
    assert page.evaluate("confirm('Publish?')") is True                                # answered by the hook
    assert "Publish?" in page.evaluate("document.documentElement.getAttribute('data-nt-last-dialog')")
    page.evaluate("document.documentElement.removeAttribute('data-nt-auto-confirm')")


def test_pasted_editor_link_identifies_the_story(env, client):
    """The link a user copies from the Wattpad editor (…/myworks/<story>/write/<part>) identifies the story."""
    story = client.post("/api/wattpad/stories/manual",
                        json={"link": "https://www.wattpad.com/myworks/415652111/write/1661937000", "title": "Truyện Demo"}).json()
    from app.publishing import publish_service as ps
    try:
        assert story == {"id": "415652111", "title": "Truyện Demo", "manual": True}
    finally:  # the story list is shared with the other test modules
        ps._save_state(stories=[s for s in ps._state().get("stories", []) if s["id"] != "415652111"])


# ---------------------------------------------------------------------------------------------------------
# Batch publishing
# ---------------------------------------------------------------------------------------------------------
def test_batch_publishes_chapters_in_order(env, client, book, fake, fast_batch):
    page = env["page"]
    old_id = fake.existing_part("222")
    pid = upload(client, book, "ext-batch.docx")
    before = set(fake.parts)
    r = start_batch(client, pid, "222", [12, 10, 11], title="Second Story")   # given out of order
    assert r.status_code == 200, r.text
    b = r.json()
    assert [i["number"] for i in b["items"]] == [10, 11, 12] and b["open_url"] == fake.base + "/myworks/222"
    page.goto(b["open_url"])                                # the tab the app opens
    b = wait_batch(client, page)
    assert b["state"] == "done", b
    assert [i["state"] for i in b["items"]] == ["done"] * 3 and "3/3" in b["message"]
    new_ids = fake.order["222"][1:]                          # creation order == publishing order
    assert len(new_ids) == 3 and set(new_ids) == set(fake.parts) - before
    for pid_, number in zip(new_ids, (10, 11, 12)):
        assert fake.parts[pid_]["published"] and fake.parts[pid_]["title"].startswith(f"Chương {number}")
    assert fake.old[old_id]["text"] == fake.OLD_TEXT and fake.trap_clicks == 0
    assert [status_of(client, pid, n) for n in (10, 11, 12)] == ["PUBLISHED"] * 3
    # a finished chapter is skipped when the same range is submitted again
    again = start_batch(client, pid, "222", [10, 11, 12, 13])
    assert again.status_code == 200 and [i["number"] for i in again.json()["items"]] == [13]
    assert again.json()["skipped"] == [{"number": n, "reason": "đã đăng trước đó"} for n in (10, 11, 12)]
    client.post(f"/api/wattpad/batch/{again.json()['id']}/cancel")


def test_batch_stops_at_first_failure_and_resumes(env, client, book, fake, fast_batch):
    page = env["page"]
    fake.existing_part("333")
    fake.flaky_failures["333"] = 1                          # the first publish attempt shows an error
    pid = upload(client, book, "ext-batch-fail.docx")
    before = set(fake.parts)
    b = start_batch(client, pid, "333", [10, 11, 12]).json()
    page.goto(b["open_url"])
    b = wait_batch(client, page)
    assert b["state"] == "error", b
    assert [i["state"] for i in b["items"]] == ["failed", "skipped", "skipped"]
    assert "Something went wrong" in b["items"][0]["message"] and "chương 10" in b["message"]
    (first_part,) = set(fake.parts) - before                 # only chapter 10 got a part; 11 and 12 untouched
    assert not fake.parts[first_part]["published"]
    assert [status_of(client, pid, n) for n in (10, 11, 12)] == ["READY_TO_PUBLISH", "LOADED", "LOADED"]

    b = start_batch(client, pid, "333", [10, 11, 12]).json()  # run it again: continues, reuses chapter 10's part
    assert b["open_url"].endswith(f"/write/{first_part}")
    page.goto(b["open_url"])
    b = wait_batch(client, page)
    assert b["state"] == "done", b
    created = set(fake.parts) - before
    assert len(created) == 3 and first_part in created and all(fake.parts[i]["published"] for i in created)
    assert [status_of(client, pid, n) for n in (10, 11, 12)] == ["PUBLISHED"] * 3
    assert fake.trap_clicks == 0


def test_batch_validation(env, client, book, fake, fast_batch):
    pid = upload(client, book, "ext-batch-invalid.docx")
    r = client.post(f"/api/projects/{pid}/publish-batch",
                    json={"story_id": "222", "chapters": [10], "mode": "publish", "confirm": False})
    assert r.status_code >= 400 and r.json()["error"]["code"] == "CONFIRM_REQUIRED"
    r = start_batch(client, pid, "222", [10, 999])
    assert r.status_code >= 400 and r.json()["error"]["code"] == "BATCH_INVALID" and "Chương 999" in r.json()["error"]["message"]
    batch = client.get("/api/wattpad/batch").json()["batch"]
    assert batch is None or batch["state"] != "running"      # nothing was started


def test_batch_cancel_before_tab_opens(env, client, book, fake, fast_batch):
    pid = upload(client, book, "ext-batch-cancel.docx")
    b = start_batch(client, pid, "222", [10, 11]).json()
    assert client.post(f"/api/projects/{pid}/chapters/12/publish",
                       json={"story_id": "222", "title": "x", "confirm": True}).status_code == 409   # busy: batch running
    r = client.post(f"/api/wattpad/batch/{b['id']}/cancel").json()["batch"]
    assert r["state"] == "cancelled" and [i["state"] for i in r["items"]] == ["cancelled", "cancelled"]
    assert httpx.get(env["app"] + "/api/ext/task", headers={"X-NT-Extension": "1"}).json() == {"task": None}
    assert [status_of(client, pid, n) for n in (10, 11)] == ["READY_TO_PUBLISH", "LOADED"]


def test_new_part_that_ignores_script_clicks_asks_the_user(env, client, book, fake):
    """If Wattpad ignores the extension's click, it must say so (and highlight the button) instead of waiting in
    silence, press nothing twice, and carry on by itself once the user pressed New Part."""
    page = env["page"]
    fake.trusted_new_part = True
    fake.existing_part("111")
    pid = ready(client, book, 13, "ext-needs-human.docx")
    before = set(fake.parts)
    job = publish_one(client, pid, 13, "111", "Chương 13", mode="draft")
    page.goto(job["open_url"])
    end = time.time() + 60
    while time.time() < end and "tự bấm" not in client.get(f"/api/jobs/{job['id']}").json()["message"]:
        page.wait_for_timeout(300)
    assert "tự bấm" in client.get(f"/api/jobs/{job['id']}").json()["message"]
    assert set(fake.parts) == before                       # nothing was created behind the user's back
    page.click("#np")                                      # the user presses New Part with a real mouse
    j = wait_job(client, job["id"], page)
    assert j["state"] == "done", j
    assert len(set(fake.parts) - before) == 1


# ---------------------------------------------------------------- updating a chapter that is already on Wattpad
def edit_and_review(client, pid, number, text):
    ch = client.get(f"/api/projects/{pid}/chapters/{number}").json()
    blocks = ch["draft"] + [{"type": "paragraph", "runs": [{"text": text}]}]
    assert client.put(f"/api/projects/{pid}/chapters/{number}/draft", json={"blocks": blocks}).status_code == 200
    client.post(f"/api/projects/{pid}/chapters/{number}/status", json={"event": "review"})


def test_edited_published_chapter_updates_its_own_part(env, client, book, fake):
    """Re-publishing an edited chapter rewrites the part it was published to, in that part's story — even when the
    publish form shows another story — and never adds a new chapter."""
    page = env["page"]
    pid = ready(client, book, 12, "ext-update-own.docx")
    job = publish_one(client, pid, 12, "111", "Chương 12")
    page.goto(job["open_url"])
    assert wait_job(client, job["id"], page)["state"] == "done"
    part_id = client.get(f"/api/projects/{pid}/chapters/12").json()["wattpad"]["part_id"]

    edit_and_review(client, pid, 12, "Đoạn đã sửa lại.")
    before = set(fake.parts)
    job = publish_one(client, pid, 12, "222", "Chương 12 (sửa)")               # another story picked in the form
    assert job["open_url"] == f"{fake.base}/myworks/111/write/{part_id}"
    page.goto(job["open_url"])
    j = wait_job(client, job["id"], page)
    assert j["state"] == "done", j
    assert "cập nhật" in j["result"]["message"]
    assert set(fake.parts) == before and part_id not in fake.order.get("222", [])   # nothing new, nowhere else
    part = fake.parts[part_id]
    assert part["published"] and part["title"] == "Chương 12 (sửa)" and "Đoạn đã sửa lại." in part["text"]
    ch = client.get(f"/api/projects/{pid}/chapters/12").json()
    assert ch["status"] == "PUBLISHED" and ch["publish_history"][-1]["story_id"] == "111"


def test_pasted_link_updates_a_chapter_published_outside_the_tool(env, client, book, fake):
    page = env["page"]
    old = fake.existing_part("111")                                         # published by hand on Wattpad
    pid = ready(client, book, 11, "ext-update-link.docx")
    before = set(fake.parts)
    job = client.post(f"/api/projects/{pid}/chapters/11/publish", json={
        "story_id": "222", "title": "Chương 11", "mode": "publish", "confirm": True,
        "part_url": f"{fake.base}/myworks/111/write/{old}"}).json()
    page.goto(job["open_url"])
    j = wait_job(client, job["id"], page)
    assert j["state"] == "done", j
    assert set(fake.parts) == before
    assert fake.old[old]["title"] == "Chương 11" and "Hết chương 11." in fake.old[old]["text"]
    assert client.get(f"/api/projects/{pid}/chapters/11").json()["wattpad"] == {
        "story_id": "111", "part_id": old, "part_url": f"{fake.base}/{old}"}


def test_update_never_falls_back_to_a_new_part(client, book):
    """The part to update is gone: the publish fails instead of adding a duplicate chapter (the extension reports
    "part_gone", which for a new chapter's retry means: create the part again)."""
    from app.publishing import publish_service
    pid = ready(client, book, 13, "ext-update-gone.docx")
    job = client.post(f"/api/projects/{pid}/chapters/13/publish", json={
        "story_id": "111", "title": "Chương 13", "mode": "publish", "confirm": True,
        "part_url": "https://www.wattpad.com/987654321-chuong-13"}).json()
    headers = {"X-NT-Extension": "1"}
    assert client.post(f"/api/ext/task/{job['id']}/event", json={"part_gone": True}, headers=headers).status_code == 200
    j = client.get(f"/api/jobs/{job['id']}").json()
    assert j["state"] == "error" and j["error_code"] == "WATTPAD_PART_NOT_FOUND"
    assert publish_service.publisher.bridge.task is None


def test_existing_part_links():
    from app.errors import StateError
    from app.publishing.publish_service import existing_part
    rec = {"wattpad": {"story_id": "7", "part_id": "55", "part_url": "u"},
           "publish_history": [{"ok": False, "mode": "publish", "part_id": "55"}]}
    assert existing_part(rec) is None                                          # only a failed attempt: a retry
    rec["publish_history"].append({"ok": True, "mode": "publish", "part_id": "55", "story_title": "S"})
    assert existing_part(rec) == {"story_id": "7", "part_id": "55", "story_title": "S"}
    assert existing_part(rec, "https://www.wattpad.com/myworks/9-abc/write/66") == {"story_id": "9", "part_id": "66"}
    assert existing_part(rec, "https://www.wattpad.com/55-chuong-moi?x=1") == {"story_id": "7", "part_id": "55"}
    assert existing_part(rec, "https://www.wattpad.com/66") == {"story_id": None, "part_id": "66"}
    with pytest.raises(StateError):
        existing_part(rec, "https://www.wattpad.com/story/123-truyen")      # a story, not a chapter
