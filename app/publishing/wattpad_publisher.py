"""WattpadPublisher — browser automation (Playwright) for creating/publishing a story part.

Independent of the DOCX parser and the editor: it only receives a title and content blocks.

Security
- Never sees or stores a password: the user logs in manually in a real browser window.
- The session lives in a dedicated persistent browser profile (data/wattpad_profile, git-ignored);
  cookies are never read into logs or returned to the UI.
"""
from __future__ import annotations

import html
import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse
from typing import Callable, Optional

from .. import config

SELECTORS: dict = json.loads((Path(__file__).parent / "wattpad_selectors.json").read_text(encoding="utf-8"))


class WattpadError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


def blocks_to_html(blocks: list[dict]) -> str:
    out = []
    for b in blocks:
        inner = ""
        for r in b.get("runs", []):
            t = html.escape(r["text"]).replace("\n", "<br>")
            if r.get("u"):
                t = f"<u>{t}</u>"
            if r.get("i"):
                t = f"<i>{t}</i>"
            if r.get("b"):
                t = f"<b>{t}</b>"
            inner += t
        align = f' style="text-align:{b["align"]}"' if b.get("align") in ("center", "right") else ""
        out.append(f"<p{align}>{inner or '<br>'}</p>")
    return "".join(out)


def blocks_to_text(blocks: list[dict]) -> str:
    return "\n\n".join("".join(r["text"] for r in b.get("runs", [])) for b in blocks)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


class WattpadPublisher:
    def __init__(self, progress: Callable[[str], None] = lambda m: None, *, base_url: Optional[str] = None,
                 profile_dir: Optional[Path] = None, channel: Optional[str] = None, headless: Optional[bool] = None):
        self.progress = progress
        self.base = (base_url or config.WATTPAD_BASE_URL).rstrip("/")
        self.profile_dir = Path(profile_dir or config.WATTPAD_PROFILE_DIR)
        self.channel = config.WATTPAD_BROWSER_CHANNEL if channel is None else channel
        self.headless = config.WATTPAD_HEADLESS if headless is None else headless
        self._pw = self._ctx = self.page = None

    # ------------------------------------------------------------------ browser
    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        errors = []
        for ch in dict.fromkeys([self.channel, "msedge", "chrome", ""]):  # configured first, then fallbacks
            try:
                self._ctx = self._pw.chromium.launch_persistent_context(
                    str(self.profile_dir), channel=ch or None, headless=self.headless,
                    # Keep extensions enabled: a VPN extension installed in this profile must work
                    # (Wattpad is blocked on some networks).
                    ignore_default_args=["--disable-extensions",
                                         "--disable-component-extensions-with-background-pages"],
                    proxy={"server": config.WATTPAD_PROXY} if config.WATTPAD_PROXY else None,
                    no_viewport=not self.headless,
                    viewport={"width": 1280, "height": 900} if self.headless else None,
                    locale="vi-VN")
                break
            except Exception as e:  # browser not installed / profile locked
                errors.append(f"{ch or 'chromium'}: {str(e).splitlines()[0]}")
        if self._ctx is None:
            self._pw.stop()
            raise WattpadError("WATTPAD_BROWSER", "Không mở được trình duyệt. " + " | ".join(errors) +
                               " — Có thể profile đang được dùng bởi một cửa sổ khác.")
        self._ctx.set_default_timeout(15000)
        self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        return self

    def __exit__(self, *exc):
        try:
            if self._ctx:
                self._ctx.close()
        finally:
            if self._pw:
                self._pw.stop()

    def url(self, key: str, **kw) -> str:
        return self.base + SELECTORS[key].format(**kw)

    NETWORK_HINT = ("Không kết nối được Wattpad — mạng có thể đang chặn Wattpad. Bấm “Đăng nhập Wattpad…” để mở "
                    "cửa sổ Edge và bật VPN trong đó (tiện ích VPN cài trong cửa sổ này sẽ được nhớ), hoặc bật VPN "
                    "cho toàn máy, hoặc đặt WATTPAD_PROXY trong .env.")

    def goto(self, url: str, attempts: int = 3) -> None:
        """Navigate; network errors are retried a few times (a VPN extension needs a moment after launch)."""
        for k in range(attempts):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=45000)
                return
            except Exception as e:
                msg = str(e)
                if not ("net::ERR_" in msg or "NS_ERROR" in msg or "Timeout" in msg):
                    raise
                if k == attempts - 1:
                    raise WattpadError("WATTPAD_NETWORK", f"{self.NETWORK_HINT} (Chi tiết: {msg.splitlines()[0][:160]})")
                self.page.wait_for_timeout(3000)

    @staticmethod
    def _logged_out_url(url: str) -> bool:
        """Wattpad sends logged-out visitors of /myworks to the home page (not to /login)."""
        path = urlparse(url).path.rstrip("/")
        return any(m in url for m in SELECTORS["login_url_markers"]) or path in SELECTORS["logged_out_paths"]

    def _on_login_page(self) -> bool:
        return any(m in self.page.url for m in SELECTORS["login_url_markers"])

    def _has_auth_cookie(self) -> bool:
        return any(c["name"] == SELECTORS["logged_in_cookie"] for c in self._ctx.cookies(self.base))

    def _first(self, key: str, timeout: float = 10.0, visible: bool = True):
        """First matching locator among the candidates for `key`, polling until timeout."""
        deadline = time.time() + timeout
        while True:
            for sel in SELECTORS[key]:
                loc = self.page.locator(sel)
                try:
                    if loc.count() and (not visible or loc.first.is_visible()):
                        return loc.first
                except Exception:
                    continue
            if time.time() >= deadline:
                return None
            self.page.wait_for_timeout(300)

    def _require(self, key: str, what: str, timeout: float = 15.0):
        loc = self._first(key, timeout)
        if loc is None:
            shot = self.debug_snapshot(f"missing-{key}")
            raise WattpadError("WATTPAD_UI_CHANGED",
                               f"Không tìm thấy {what} trên trang Wattpad ({self.page.url}). Giao diện Wattpad có thể đã "
                               f"thay đổi — cập nhật mục '{key}' trong app/publishing/wattpad_selectors.json.",
                               screenshot=shot)
        return loc

    def debug_snapshot(self, name: str) -> Optional[str]:
        try:
            d = config.LOG_DIR / "wattpad"
            d.mkdir(parents=True, exist_ok=True)
            path = d / f"{time.strftime('%Y%m%d-%H%M%S')}-{name}.png"
            self.page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:
            return None

    # ------------------------------------------------------------------ session
    def is_logged_in(self) -> bool:
        """Logged in only if /myworks actually stays on My Works."""
        self.goto(self.url("myworks_path"))
        self.page.wait_for_timeout(1500)
        return SELECTORS["myworks_path"] in urlparse(self.page.url).path and not self._logged_out_url(self.page.url)

    def login(self, timeout: int) -> dict:
        """Open Wattpad in a visible window and wait for the user to sign in manually."""
        if self.headless:
            raise WattpadError("WATTPAD_LOGIN_FAILED", "Đăng nhập thủ công cần WATTPAD_HEADLESS=false.")
        return self._login_flow(timeout)

    def _live_page(self):
        """The page to work with; follows the user if they closed our tab but kept the window."""
        if self.page is None or self.page.is_closed():
            pages = [pg for pg in self._ctx.pages if not pg.is_closed()]
            if not pages:
                raise WattpadError("WATTPAD_LOGIN_FAILED", "Cửa sổ trình duyệt đã bị đóng trước khi đăng nhập xong.")
            self.page = pages[-1]
        return self.page

    def _wait_until_reachable(self, deadline: float) -> None:
        """Keep the window open while Wattpad is unreachable (e.g. VPN off). Error pages are reloaded
        automatically; the user can also turn on / install a VPN in this window and press F5."""
        self.progress("Không vào được Wattpad (mạng đang chặn?). Cửa sổ Edge vẫn mở: hãy BẬT VPN — nếu VPN là tiện ích "
                      "của Edge thì cài/bật nó ngay trong cửa sổ này (chỉ cần một lần, tool sẽ nhớ). "
                      "Tool tự thử lại mỗi vài giây, hoặc bạn bấm F5.")
        last_reload = time.time()
        while time.time() < deadline:
            page = self._live_page()
            for pg in self._ctx.pages:
                if not pg.is_closed() and pg.url.startswith(self.base):
                    self.page = pg
                    self.progress("Đã vào được Wattpad.")
                    return
            if time.time() - last_reload > 8 and page.url.startswith("chrome-error://"):
                last_reload = time.time()
                try:
                    page.reload(wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    pass  # still unreachable
            page.wait_for_timeout(1500)
        raise WattpadError("WATTPAD_NETWORK", "Hết thời gian chờ mà vẫn không vào được Wattpad. Hãy bật VPN rồi thử lại.")

    def _login_flow(self, timeout: int) -> dict:
        deadline = time.time() + timeout
        waiting_for_user = False
        last_probe = time.time()
        self.progress("Đang mở Wattpad…")
        while time.time() < deadline:
            try:
                page = self._live_page()
                if not waiting_for_user:
                    if self.is_logged_in():
                        self.progress("Đã đăng nhập.")
                        return {"logged_in": True}
                    self.goto(self.url("login_path"))
                    self.progress(f"Hãy đăng nhập Wattpad trong cửa sổ Edge (tối đa {timeout // 60} phút). "
                                  "Tool không đọc mật khẩu của bạn.")
                    waiting_for_user = True
                else:
                    # Never navigate the tab while the user is on the login page (they may be typing).
                    # Check when the auth cookie appears, or now and then once they have left that page.
                    left_login = page.url.startswith(self.base) and not self._on_login_page()
                    if self._has_auth_cookie() or (left_login and time.time() - last_probe > 20):
                        last_probe = time.time()
                        page.wait_for_timeout(1500)
                        if self.is_logged_in():
                            self.progress("Đăng nhập thành công.")
                            return {"logged_in": True}
                        self.goto(self.url("login_path"))
                        self.progress("Chưa đăng nhập — hãy đăng nhập Wattpad trong cửa sổ Edge.")
                page.wait_for_timeout(1500)
            except WattpadError as e:
                if e.code != "WATTPAD_NETWORK":
                    raise
                self._wait_until_reachable(deadline)
                waiting_for_user = False
            except Exception as e:
                if "closed" in str(e).lower():
                    raise WattpadError("WATTPAD_LOGIN_FAILED", "Cửa sổ trình duyệt đã bị đóng trước khi đăng nhập xong.")
                raise
        raise WattpadError("WATTPAD_LOGIN_FAILED", f"Hết {timeout}s mà chưa đăng nhập xong. Hãy thử lại.")

    def _ensure_session(self) -> None:
        if self._logged_out_url(self.page.url):
            raise WattpadError("WATTPAD_SESSION_EXPIRED", "Phiên đăng nhập Wattpad đã hết hạn. Bấm “Đăng nhập Wattpad…” rồi thử lại.")

    # ------------------------------------------------------------------ stories
    _LINKS_JS = """() => [...document.querySelectorAll('a[href]')].map(a => {
        const card = a.closest('li, article, [class*="story" i], [class*="work" i]');
        const heading = card && card.querySelector('h1, h2, h3, h4, [class*="title" i]');
        return [a.href, (a.innerText || a.getAttribute('title') || a.getAttribute('aria-label') || '').trim(),
                heading ? heading.innerText.trim() : ''];
    })"""

    def _collect_stories(self) -> dict[str, str]:
        """Story id → title from the links on the page. The first regex that matches anything wins
        (links to the user's own works are preferred over public /story/ links)."""
        links = self.page.evaluate(self._LINKS_JS)
        for pattern in SELECTORS["story_link_regexes"]:
            stories: dict[str, str] = {}
            for href, text, heading in links:
                m = re.search(pattern, href or "")
                if not m:
                    continue
                sid = m.group(1)
                for cand in (_norm(heading), _norm(text)):
                    if cand and len(cand) < 200 and len(cand) > len(stories.get(sid, "")) \
                            and cand.lower() not in SELECTORS["story_title_ignore"]:
                        stories[sid] = cand
                stories.setdefault(sid, "")
            if stories:
                return stories
        return {}

    def list_stories(self) -> dict:
        self.progress("Đang tải danh sách truyện…")
        self.goto(self.url("myworks_path"))
        self._ensure_session()
        # My Works is rendered by JavaScript: wait until story links show up (or give up after ~25s).
        stories: dict[str, str] = {}
        deadline = time.time() + 25
        while time.time() < deadline:
            try:
                self.page.wait_for_load_state("networkidle", timeout=3000)
            except Exception:
                pass
            stories = self._collect_stories()
            if stories:
                self.page.wait_for_timeout(1500)  # let the rest of the list render
                stories = self._collect_stories() or stories
                break
            self.page.wait_for_timeout(1000)
        result = [{"id": sid, "title": title or f"Truyện #{sid}"} for sid, title in stories.items()]
        snapshot = None
        try:  # keep a picture of My Works next to the saved list
            d = config.LOG_DIR / "wattpad"
            d.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(d / "myworks-latest.png"), full_page=True)
            snapshot = str(d / "myworks-latest.png")
        except Exception:
            pass
        if not result:
            shot = self.debug_snapshot("myworks-empty")
            html_path = None
            try:
                html_path = Path(shot).with_suffix(".html") if shot else None
                if html_path:
                    html_path.write_text(self.page.content(), encoding="utf-8")
            except Exception:
                html_path = None
            self.progress(f"Không tìm thấy truyện nào trên {self.page.url}.")
            return {"stories": [], "page_url": self.page.url, "screenshot": shot,
                    "html": str(html_path) if html_path else None}
        self.progress(f"Tìm thấy {len(result)} truyện.")
        return {"stories": result, "snapshot": snapshot}

    # ------------------------------------------------------------------ publish
    def _insert_body(self, body, blocks: list[dict]) -> None:
        html_content, text = blocks_to_html(blocks), blocks_to_text(blocks)
        body.click()
        self.page.keyboard.press("Control+A")
        self.page.keyboard.press("Delete")
        body.evaluate("""(el, [html, text]) => {
            el.focus();
            const dt = new DataTransfer();
            dt.setData('text/html', html); dt.setData('text/plain', text);
            const ev = new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true});
            if (el.dispatchEvent(ev)) document.execCommand('insertHTML', false, html);
        }""", [html_content, text])
        self.page.wait_for_timeout(500)
        if self._body_matches(body, text):
            return
        self.progress("Dán HTML không thành công, chuyển sang gõ từng đoạn…")
        body.click()
        self.page.keyboard.press("Control+A")
        self.page.keyboard.press("Delete")
        paras = text.split("\n\n")
        for k, p in enumerate(paras):
            lines = p.split("\n")
            for j, line in enumerate(lines):
                if line:
                    self.page.keyboard.insert_text(line)
                if j < len(lines) - 1:
                    self.page.keyboard.press("Shift+Enter")
            if k < len(paras) - 1:
                self.page.keyboard.press("Enter")
        if not self._body_matches(body, text):
            shot = self.debug_snapshot("body-mismatch")
            raise WattpadError("WATTPAD_PUBLISH_FAILED", "Không chèn được nội dung chương vào editor Wattpad.", screenshot=shot)

    @staticmethod
    def _body_matches(body, text: str) -> bool:
        got, want = _norm(body.inner_text()), _norm(text)
        return bool(want) and got[:40] == want[:40] and got[-40:] == want[-40:] and abs(len(got) - len(want)) <= max(20, len(want) // 50)

    def publish(self, *, story_id: str, title: str, blocks: list[dict], mode: str = "publish",
                part_id: Optional[str] = None, update: bool = False) -> dict:
        """Create (or update, when `part_id` is given) a part; publish it only if mode == 'publish'.
        `update`: the part is an already-published chapter — if it is gone, fail rather than add a duplicate."""
        if part_id:
            self.progress(f"Mở lại phần đã tạo trước đó (#{part_id})…")
            self.goto(self.url("part_edit_path", story_id=story_id, part_id=part_id))
            self._ensure_session()
            if self._first("not_found", 2) or not re.search(SELECTORS["part_url_regex"], self.page.url):
                if update:
                    raise WattpadError("WATTPAD_PART_NOT_FOUND", f"Không mở được chương #{part_id} trên Wattpad để cập nhật. "
                                       "Không tạo chương mới — kiểm tra link rồi thử lại.")
                self.progress("Phần cũ không còn, sẽ tạo phần mới.")
                part_id = None
        if not part_id:
            self.progress("Mở trang truyện…")
            self.goto(self.url("story_path", story_id=story_id))
            self._ensure_session()
            if self._first("not_found", 2):
                raise WattpadError("WATTPAD_STORY_NOT_FOUND", f"Không tìm thấy truyện #{story_id} trong tài khoản Wattpad.")
            new_part = self._first("new_part", 10)
            if new_part is None:
                if not re.search(SELECTORS["story_link_regex"], self.page.url):
                    raise WattpadError("WATTPAD_STORY_NOT_FOUND", f"Không mở được truyện #{story_id}.")
                new_part = self._require("new_part", "nút “New Part”")
            self.progress("Tạo phần mới…")
            new_part.click()
            try:
                self.page.wait_for_url(re.compile(SELECTORS["part_url_regex"]), timeout=30000)
            except Exception:
                shot = self.debug_snapshot("new-part")
                raise WattpadError("WATTPAD_UI_CHANGED", "Bấm “New Part” nhưng không vào được trang soạn thảo.", screenshot=shot)
            m = re.search(SELECTORS["part_url_regex"], self.page.url)
            part_id = m.group(1) if m else None

        result = {"part_id": part_id, "part_url": self.url("part_public_url", part_id=part_id) if part_id else None}
        try:
            self.progress("Điền tiêu đề…")
            title_el = self._require("title", "ô tiêu đề chương")
            title_el.click()
            try:
                title_el.fill(title)
            except Exception:  # contenteditable title
                self.page.keyboard.press("Control+A")
                self.page.keyboard.insert_text(title)

            self.progress("Chèn nội dung…")
            self._insert_body(self._require("body", "vùng soạn nội dung"), blocks)

            save = self._first("save", 3)
            if save is not None and save.is_enabled():
                self.progress("Lưu nháp…")
                save.click()
                self._first("saved_indicator", 10)
            else:
                self.page.wait_for_timeout(3000)  # let autosave run

            if mode != "publish":
                self.progress("Đã lưu nháp trên Wattpad (chưa publish).")
                return {**result, "published": False}

            self.progress("Publish…")
            start_url = self.page.url
            self._require("publish", "nút “Publish”").click()
            confirm = self._first("publish_confirm", 5)
            if confirm is not None:
                confirm.click()
            deadline = time.time() + config.WATTPAD_PUBLISH_WAIT
            while time.time() < deadline:
                self.page.wait_for_timeout(700)
                self._ensure_session()
                if self.page.url != start_url and "/write/" not in self.page.url:
                    break
                if self._first("published_indicator", 0.1) is not None:
                    break
                if self._first("publish", 0.1) is None and self._first("publish_confirm", 0.1) is None:
                    break  # the Publish button is gone: the part is no longer a draft
            else:
                shot = self.debug_snapshot("publish-unconfirmed")
                raise WattpadError("WATTPAD_PUBLISH_FAILED",
                                   f"Đã bấm Publish nhưng không xác nhận được kết quả trong {config.WATTPAD_PUBLISH_WAIT} giây. "
                                   "Hãy kiểm tra trên Wattpad trước khi thử lại.", screenshot=shot)
            self.progress("Publish thành công.")
            return {**result, "published": True}
        except WattpadError as e:
            e.extra.setdefault("part_id", part_id)  # lets a retry reuse the part instead of duplicating it
            raise
