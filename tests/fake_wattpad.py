"""A tiny local imitation of the Wattpad writer flow, used only for tests.

It is NOT Wattpad: real selectors are configured in app/publishing/wattpad_selectors.json and were checked
against the live site by hand.

Stories: 111 (normal), 222 (normal), 333 ("flaky": the first publish attempt fails).

Two flows (`fake.flow`):
- "classic": the legacy layout the Playwright publisher was written for: /myworks/<id> is a story page with a
  "+ New Part" link that creates a part and redirects into its editor; Publish opens a role=dialog popup.
- "modern": what the real site does (checked on a real account, Oct 2026):
  * /myworks/<id> ("Edit Story Details") lists the parts, oldest first, each row "Published|Draft - date". Its
    "+ New Part" button only adds a blank draft to the list and opens NOTHING (`fake.trap_clicks` counts presses:
    a correct client never presses it).
  * The editor /myworks/<id>/write/<part> has the title in h2#story-title[contenteditable], the text in
    div.story-editor (no #storytext), a header with the part's title, "Draft|Published" and "(N Words)", and a parts
    dropdown (closed until the header is clicked) holding the list of parts and "New Part" (single-page: the URL
    changes without a reload).
  * Publish asks for confirmation the way `fake.confirm_style` says: "native" (the browser's own confirm(); a
    client that cannot answer it publishes nothing), "layer" (a plain div popup without role / modal class) or
    "two_step" (two such popups). Failures show a "Something went wrong" toast.
"""
from __future__ import annotations

import html
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

STORIES = {"111": "Truyện Thử Nghiệm", "222": "Second Story", "333": "Flaky Story"}


class FakeWattpad:
    OLD_TEXT = "Nội dung chương cũ — tuyệt đối không được ghi đè."

    def __init__(self, port: int = 0, auto_login: bool = False):
        self.auto_login = auto_login  # /login signs in by itself (stands in for the user typing)
        self.empty_myworks = False
        self.flow = "classic"
        self.confirm_style = "native"           # modern flow: "native" | "layer" | "two_step"
        self.frozen_menu = False                # the editor's New Part button never turns visible (background tab)
        self.trusted_new_part = False           # the editor's New Part ignores script-made clicks (only a real mouse works)
        self.title_model = False                # the editor takes the title into its model on key events only
        self.title_locked = False               # saving never changes a part's title (it stays "Untitled Part N")
        self.trap_clicks = 0                    # presses of the story page's "+ New Part" (it opens nothing)
        self.existing: dict[str, str] = {}      # story id -> id of the first pre-existing (published) chapter
        self.old: dict[str, dict] = {}          # chapters that existed before the test (`parts` only holds new ones)
        self.parts: dict[str, dict] = {}
        self.order: dict[str, list[str]] = {}   # story id -> part ids, oldest first (what the lists show)
        self.next_id = 9000
        self.flaky_failures = {"333": 1}
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    def reset(self):
        """Back to a clean account: no parts, default behaviour (modern flow tests start from here)."""
        for d in (self.old, self.parts, self.order, self.existing):
            d.clear()
        self.trap_clicks = 0
        self.flaky_failures = {"333": 1}
        self.confirm_style, self.frozen_menu, self.trusted_new_part = "native", False, False
        self.title_model = self.title_locked = False

    def find(self, pid: str):
        return self.old.get(pid) or self.parts.get(pid)

    def _new_id(self) -> str:
        self.next_id += 1
        return str(self.next_id)

    def add_old(self, story: str, title: str, text: str = "", published: bool = True) -> str:
        """A chapter that already exists on the account, appended after the story's current latest one."""
        pid = self._new_id()
        self.old[pid] = {"story": story, "title": title, "html": f"<p>{html.escape(text)}</p>" if text else "",
                         "text": text, "published": published}
        self.order.setdefault(story, []).append(pid)
        return pid

    def existing_part(self, story: str) -> str:
        """The story's first pre-existing chapter (published), created on first use. Anything written to it
        shows up in `fake.old`."""
        if story not in self.existing:
            self.existing[story] = self.add_old(story, "Chương cũ", self.OLD_TEXT)
        return self.existing[story]

    def add_blank_draft(self, story: str) -> str:
        """An empty "Untitled Part N" draft as the story's newest chapter (what a half-done attempt leaves behind)."""
        self.existing_part(story)
        return self.add_old(story, f"Untitled Part {len(self.order[story]) + 1}", "", published=False)

    def _create_part(self, story: str, title: str) -> str:
        pid = self._new_id()
        self.parts[pid] = {"story": story, "title": title, "html": "", "text": "", "published": False}
        self.order.setdefault(story, []).append(pid)
        return pid

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code=200, body="", ctype="text/html; charset=utf-8", headers=()):
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _redirect(self, to, headers=()):
                self._send(302, "", headers=[("Location", to), *headers])

            def _logged_in(self):
                return "token=ok" in (self.headers.get("Cookie") or "")

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/dev-login":
                    return self._redirect("/myworks", [("Set-Cookie", "token=ok; Path=/; Max-Age=86400")])
                if path == "/login":
                    auto = "<script>setTimeout(() => location.href = '/dev-login', 800)</script>" if fake.auto_login else ""
                    return self._send(body=f"<h1>Log in</h1><form><input name=u><input type=password name=p></form>{auto}")
                if path == "/":
                    trending = "".join(f'<a href="/story/{n}">.</a>' for n in (319781399, 351599673, 90623050))
                    login = "" if self._logged_in() else '<a href="/login">Log in</a>'
                    return self._send(body=f"<header>{login}</header><h3>Trending</h3>{trending}")
                m = re.fullmatch(r"/api/v3/stories/(\d+)", path)
                if m and self._logged_in():      # the parts of a story, drafts included, as Wattpad stored them
                    parts = [{"id": int(p), "title": fake.find(p)["title"], "draft": not fake.find(p)["published"]}
                             for p in fake.order.get(m.group(1), [])]
                    return self._send(200, json.dumps({"parts": parts}), "application/json")
                if path.startswith("/myworks") and not self._logged_in():
                    return self._redirect("/")
                if path == "/myworks":
                    # Rendered by JavaScript after a delay, like a single-page app; the links are the story pages
                    # (…/myworks/<id>-<slug>, text = the title) and "View as reader" (/story/…).
                    cards = [] if fake.empty_myworks else [
                        f'<li class="story-card"><h3>{html.escape(t)}</h3>'
                        f'<a href="/myworks/{sid}-slug">{html.escape(t)}</a> '
                        f'<a href="/story/{sid}-slug">View as reader</a></li>' for sid, t in STORIES.items()]
                    script = ("<script>setTimeout(() => { document.querySelector('#list').innerHTML = "
                              + json.dumps("".join(cards)) + "; }, 2500)</script>")
                    return self._send(body=f"<h1>My Works</h1><ul id='list'></ul>{script}")
                m = re.fullmatch(r"/myworks/(\d+)(?:-[\w-]+)?", path)
                if m:
                    sid = m.group(1)
                    if sid not in STORIES:
                        return self._send(404, "<h1>Page not found</h1>")
                    if fake.flow == "modern":
                        fake.existing_part(sid)
                        rows = [{"id": p, "title": fake.find(p)["title"], "published": fake.find(p)["published"]}
                                for p in fake.order[sid]]
                        return self._send(body=STORY_PAGE.replace("__STORY__", sid)
                                          .replace("__ROWS__", json.dumps(rows).replace("</", "<\\/")))
                    return self._send(body=f'<h1>{STORIES[sid]}</h1><a href="/myworks/{sid}/write/new">+ New Part</a>')
                m = re.fullmatch(r"/myworks/(\d+)/write/new", path)
                if m:
                    pid = fake._create_part(m.group(1), "")
                    return self._redirect(f"/myworks/{m.group(1)}/write/{pid}")
                m = re.fullmatch(r"/myworks/(\d+)/write/(\d+)", path)
                if m:
                    part = fake.find(m.group(2))
                    if part is None:
                        return self._send(404, "<h1>Page not found</h1>")
                    if fake.flow == "modern":
                        rows = [{"id": p, "title": fake.find(p)["title"], "published": fake.find(p)["published"]}
                                for p in fake.order.get(part["story"], [])]
                        init = {"title": part["title"], "html": part["html"], "published": part["published"]}
                        return self._send(body=EDITOR_MODERN.replace("__INIT__", json.dumps(init).replace("</", "<\\/"))
                                          .replace("__ROWS__", json.dumps(rows).replace("</", "<\\/"))
                                          .replace("__PART__", m.group(2)).replace("__STORY__", part["story"])
                                          .replace("__CONFIRM__", fake.confirm_style)
                                          .replace("__TITLE_MODEL__", "true" if fake.title_model else "false")
                                          .replace("__TRUSTED__", "true" if fake.trusted_new_part else "false")
                                          .replace("__FROZEN__", "visibility:hidden" if fake.frozen_menu else ""))
                    return self._send(body=EDITOR.replace("__PART__", m.group(2)))
                m = re.fullmatch(r"/(\d+)", path)
                if m and fake.find(m.group(1)):
                    return self._send(body=f"<h1>{html.escape(fake.find(m.group(1))['title'])}</h1>")
                return self._send(404, "<h1>Page not found</h1>")

            def do_POST(self):
                path = urlparse(self.path).path
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                m = re.fullmatch(r"/api/(new-part|trap-new-part)/(\d+)", path)
                if m and self._logged_in() and m.group(2) in STORIES:
                    if m.group(1) == "trap-new-part":
                        fake.trap_clicks += 1
                    title = f"Untitled Part {len(fake.order.get(m.group(2), [])) + 1}"
                    pid = fake._create_part(m.group(2), title)
                    return self._send(200, json.dumps({"id": pid, "title": title}), "application/json")
                m = re.fullmatch(r"/api/(save|publish)/(\d+)", path)
                if not m or fake.find(m.group(2)) is None or not self._logged_in():
                    return self._send(403, "{}", "application/json")
                part = fake.find(m.group(2))
                part.update(html=body["html"], text=body["text"])
                if not fake.title_locked:
                    part["title"] = body["title"]
                if m.group(1) == "publish":
                    if fake.flaky_failures.get(part["story"], 0) > 0:
                        fake.flaky_failures[part["story"]] -= 1
                        return self._send(500, '{"error":"server error"}', "application/json")
                    part["published"] = True
                return self._send(200, "{}", "application/json")

        return H


EDITOR = """<!doctype html><html><body>
<input id="story-title" placeholder="Untitled Part">
<button class="on-save">Save</button> <button class="on-publish">Publish</button> <span id="st"></span>
<div id="storytext" contenteditable="true" style="min-height:200px;border:1px solid #ccc"></div>
<div id="dlg" role="dialog" hidden><p>Publish this part?</p><button id="go">Publish</button></div>
<script>
const part = "__PART__";
const payload = () => ({title: document.querySelector('#story-title').value,
  html: document.querySelector('#storytext').innerHTML, text: document.querySelector('#storytext').innerText});
document.querySelector('.on-save').onclick = async () => {
  await fetch('/api/save/' + part, {method: 'POST', body: JSON.stringify(payload())});
  document.querySelector('#st').textContent = 'Saved';
};
document.querySelector('.on-publish').onclick = () => { document.querySelector('#dlg').hidden = false; };
document.querySelector('#go').onclick = async () => {
  const r = await fetch('/api/publish/' + part, {method: 'POST', body: JSON.stringify(payload())});
  document.querySelector('#dlg').hidden = true;
  if (r.ok) location.href = '/' + part; else document.querySelector('#st').textContent = 'Error publishing';
};
</script></body></html>"""


# The real story page ("Edit Story Details"): the parts list renders progressively, and its "+ New Part" button
# adds a blank draft to the list but opens nothing.
STORY_PAGE = """<!doctype html><html><body>
<h1>Edit Story Details</h1>
<button class="btn btn-orange on-new-part">+ New Part</button>
<ul id="toc"></ul>
<script>
const story = "__STORY__", rows = __ROWS__;
const row = (r) => '<li><div class="part-meta vcenter"><a class="on-navigate auto-shorten" href="/myworks/' + story +
  '/write/' + r.id + '">' + r.title + '</a> <span>' + (r.published ? 'Published' : 'Draft') + ' - Oct 06, 2026</span></div></li>';
const half = Math.max(1, rows.length - 1);
document.querySelector('#toc').innerHTML = rows.slice(0, half).map(row).join('');
setTimeout(() => { document.querySelector('#toc').insertAdjacentHTML('beforeend', rows.slice(half).map(row).join('')); }, 700);
document.querySelector('.on-new-part').onclick = async () => {
  const r = await (await fetch('/api/trap-new-part/' + story, {method: 'POST', body: '{}'})).json();
  document.querySelector('#toc').insertAdjacentHTML('beforeend', row({id: r.id, title: r.title, published: false}));
};
</script></body></html>"""


# The real editor. Deliberately not markup the extension could lean on beyond what the real site has: popups are
# plain divs without role / "modal" class, and the confirmation may be the browser's own confirm().
EDITOR_MODERN = """<!doctype html><html><body>
<div class="navbar-story-metadata dropdown">
  <div class="navbar-story-parts dropdown-toggle" id="partsToggle" style="cursor:pointer">
    <p class="small group-title">Story</p><p class="h4 story-part-title" id="hdrTitle"></p>
    <span class="small" id="hdrState"></span> <span class="word-count small" id="hdrWords"></span>
    <span class="save-indicator small" id="st"></span>
  </div>
  <ul class="dropdown-menu" id="partsMenu" style="display:none">
    <div class="story-parts-wrapper" id="partsList"></div>
    <li class="newpart-wrapper"><button class="btn btn-orange on-newpart" id="np" style="__FROZEN__">New Part</button></li>
  </ul>
</div>
<h2 id="story-title" contenteditable="true"></h2>
<button class="on-save">Save</button> <span id="pubslot"></span>
<div class="story-editor medium-editor-element" contenteditable="true" role="textbox" data-placeholder="Type your text"
     style="min-height:200px;border:1px solid #ccc"></div>
<div id="toasts"></div>
<script>
const story = "__STORY__", CONFIRM = "__CONFIRM__", TRUSTED = __TRUSTED__, TITLE_MODEL = __TITLE_MODEL__;
let part = "__PART__";
const parts = __ROWS__;
const $ = (s) => document.querySelector(s);
const editor = () => $('.story-editor');
let titleModel = null;   // TITLE_MODEL: like Wattpad, the saved title is the model's, updated on key events only
const payload = () => ({title: TITLE_MODEL ? titleModel : $('#story-title').innerText, html: editor().innerHTML, text: editor().innerText});
$('#story-title').addEventListener('keyup', () => { titleModel = $('#story-title').innerText; });
const words = () => editor().innerText.split(/\\s+/).filter(Boolean).length;
function toast(t) { $('#toasts').innerHTML = '<div class="snackbar" role="status">' + t + '</div>'; }
function layer(inner) {
  const d = document.createElement('div');
  d.className = 'zq-' + Math.random().toString(36).slice(2, 7);
  d.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:50;display:flex;align-items:center;justify-content:center';
  d.innerHTML = '<div style="background:#fff;padding:20px;min-width:260px">' + inner + '</div>';
  document.body.appendChild(d);
  return d;
}
function header(published) {
  $('#hdrTitle').textContent = $('#story-title').innerText;
  $('#hdrState').textContent = published ? 'Published' : 'Draft';
  $('#hdrWords').textContent = '(' + words() + ' Words)';
}
function list() {
  $('#partsList').innerHTML = parts.map((r) => '<li class="story-part"><a class="story-part-link" href="/myworks/' + story +
    '/write/' + r.id + '"><div class="part-title">' + r.title + '</div><div class="part-meta"><span class="publish-state ' +
    (r.published ? 'published' : '') + '">' + (r.published ? 'Published' : 'Draft') + '</span></div></a></li>').join('');
}
function render(st) {
  $('#story-title').innerText = titleModel = st.title; editor().innerHTML = st.html; $('#toasts').innerHTML = '';
  $('#pubslot').innerHTML = st.published ? '<button class="x-un">Unpublish</button>' : '<button class="x-pub">Publish</button>';
  const b = $('.x-pub'); if (b) b.onclick = askPublish;
  header(st.published); list();
}
async function doPublish() {
  const r = await fetch('/api/publish/' + part, {method: 'POST', body: JSON.stringify(payload())});
  if (r.ok) { parts.forEach((p) => { if (p.id === part) p.published = true; });
    render({title: $('#story-title').innerText, html: editor().innerHTML, published: true}); toast('Part published'); }
  else toast('Something went wrong, please try again.');
}
function ask(text, ok, then) {
  const d = layer('<p>' + text + '</p><button class="c">Cancel</button> <button class="g">' + ok + '</button>');
  d.querySelector('.c').onclick = () => d.remove();
  d.querySelector('.g').onclick = () => { d.remove(); then(); };
}
function askPublish() {
  if (CONFIRM === 'native') { if (confirm('Are you sure you want to publish this part?')) doPublish(); return; }
  ask('Ready to go live? Readers will see this part.', 'Publish', () => CONFIRM === 'two_step'
    ? ask('Last check: this part becomes public.', 'Yes, publish', doPublish) : doPublish());
}
$('.on-save').onclick = async () => {
  await fetch('/api/save/' + part, {method: 'POST', body: JSON.stringify(payload())}); $('#st').textContent = 'Saved';
  header(false);
};
$('#partsToggle').onclick = () => { const m = $('#partsMenu'); m.style.display = m.style.display === 'none' ? 'block' : 'none'; };
$('#np').onclick = async (ev) => {
  if (TRUSTED && !ev.isTrusted) return;   // ignores clicks made by scripts
  const r = await (await fetch('/api/new-part/' + story, {method: 'POST', body: '{}'})).json();
  part = r.id; parts.push({id: r.id, title: r.title, published: false});
  history.pushState(null, '', '/myworks/' + story + '/write/' + part);   // single-page: no reload
  $('#partsMenu').style.display = 'none';
  render({title: r.title, html: '', published: false});
};
render(__INIT__);
</script></body></html>"""
