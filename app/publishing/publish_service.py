"""Server-side orchestration of Wattpad actions: session state, story list, publish + chapter status.

Status rules:
- Publishing requires confirmation and a REVIEWED / READY_TO_PUBLISH chapter (→ READY_TO_PUBLISH).
- Only a verified successful publish moves the chapter to PUBLISHED (after saving the local DOCX).
- A failure keeps READY_TO_PUBLISH and records the error; if a part was already created, its id is
  kept so a retry updates that part instead of creating a duplicate.

Batch (several chapters in one go): chapters are published strictly one after another, in ascending order,
each through the same single-chapter path. The first failure or a cancel stops the batch - the remaining
chapters stay untouched, so fixing the problem and running the batch again continues where it stopped.
"""
from __future__ import annotations

import importlib.util
import json
import re
import threading
import uuid
from typing import Callable, Optional

from .. import config
from ..editor.chapter_editor import ChapterService, service as chapter_service
from ..editor.chapter_state import Status, transition
from ..errors import AppError, StateError
from ..logging_setup import get_logger
from ..storage.project_state import _read_json, _write_json, now_iso
from .extension_bridge import ExtensionBridge
from .jobs import JobBusy, JobManager, jobs as default_jobs

log = get_logger("WATTPAD")
STATE_FILE = config.DATA_DIR / "wattpad_state.json"
EXTENSION_VERSION = json.loads((config.BASE_DIR / "extension" / "manifest.json").read_text(encoding="utf-8"))["version"]
BATCH_MAX = 100
BATCH_DELAY = 8        # seconds between two chapters of a batch (don't hammer Wattpad)
BATCH_COUNTDOWN = 3    # seconds the extension waits (with a Cancel button) before each Publish click


def _state() -> dict:
    return _read_json(STATE_FILE, {}) or {}


def _save_state(**fields) -> None:
    _write_json(STATE_FILE, {**_state(), **fields})


def status() -> dict:
    extension_mode = config.WATTPAD_MODE == "extension"
    available = extension_mode or importlib.util.find_spec("playwright") is not None
    st = _state()
    return {
        "mode": "extension" if extension_mode else "playwright",
        "base": config.WATTPAD_BASE_URL.rstrip("/"),
        "extension_connected": publisher.bridge.connected(),
        "extension_last_seen": publisher.bridge.last_seen_iso,
        "extension_version": publisher.bridge.version,       # None until the extension has called; "1.0.0" = too old to say
        "extension_expected": EXTENSION_VERSION,
        "available": available,
        "error": None if available else "Chưa cài playwright (pip install playwright).",
        "logged_in_hint": bool(st.get("logged_in_at")) and not st.get("session_expired"),
        "logged_in_at": st.get("logged_in_at"),
        "stories": st.get("stories", []),
        "stories_updated_at": st.get("stories_updated_at"),
        "has_snapshot": (config.LOG_DIR / "wattpad" / "myworks-latest.png").exists(),
    }


STORY_ID_RE = re.compile(r"(?:/(?:myworks|story)/|^)(\d{5,12})(?=\D|$)")


def save_stories_from_extension(stories: list[dict], page_url: str) -> dict:
    """Story list scraped by the extension from My Works in the user's own browser."""
    clean = [{"id": str(s["id"]), "title": str(s.get("title") or f"Truyện #{s['id']}")[:200]}
             for s in stories if str(s.get("id", "")).isdigit()]
    manual = [s for s in _state().get("stories", []) if s.get("manual")]
    found = {s["id"] for s in clean}
    _save_state(stories=clean + [s for s in manual if s["id"] not in found], stories_updated_at=now_iso(),
                logged_in_at=now_iso(), session_expired=False, stories_source=page_url[:300])
    log.info("Stories received from extension: %d", len(clean))
    return {"saved": len(clean)}


PART_EDIT_RE = re.compile(r"/myworks/(\d+)[^/]*/write/(\d+)")
PART_PUBLIC_RE = re.compile(r"wattpad\.com/(\d+)(?:-[^/?#]*)?/?(?:[?#]|$)")


def existing_part(rec: dict, part_url: str = "") -> Optional[dict]:
    """The Wattpad part a publish must update in place, or None for a new chapter:
    the pasted link (editor link …/myworks/<story>/write/<part>, or reader link wattpad.com/<part>-ten-chuong),
    else the part this chapter was successfully published to before. A part only created by a failed attempt is
    not one (that is a retry of a new chapter). story_id is None when only a reader link is known."""
    wp = rec.get("wattpad") or {}
    if part_url.strip():
        if m := PART_EDIT_RE.search(part_url):
            return {"story_id": m.group(1), "part_id": m.group(2)}
        if m := PART_PUBLIC_RE.search(part_url.strip()):
            same = wp.get("part_id") == m.group(1)
            return {"story_id": wp.get("story_id") if same else None, "part_id": m.group(1)}
        raise StateError("Không nhận ra link chương Wattpad. Hãy dán link đọc (https://www.wattpad.com/123456789-ten-chuong) "
                         "hoặc link soạn thảo (…/myworks/<truyện>/write/<chương>).", code="BAD_PART_LINK")
    done = [h for h in rec.get("publish_history") or [] if h.get("ok") and h.get("mode") == "publish"
            and h.get("part_id") and h["part_id"] == wp.get("part_id")]
    if not done:
        return None
    return {"story_id": wp.get("story_id"), "part_id": wp["part_id"], "story_title": done[-1].get("story_title")}


def add_manual_story(link: str, title: str = "") -> dict:
    """Add a story from a pasted Wattpad link (…/story/123456789-ten, …/myworks/123456789 or the editor link
    …/myworks/123456789/write/987654321) or a bare id."""
    link = link.strip()
    m = STORY_ID_RE.search(link)
    if not m:
        raise StateError("Không nhận ra ID truyện trong link. Hãy dán link dạng "
                         "https://www.wattpad.com/story/123456789-ten-truyen hoặc chỉ số ID.", code="BAD_STORY_LINK")
    sid = m.group(1)
    if not title.strip():
        slug = re.search(rf"{sid}-([\w-]+)", link)
        title = slug.group(1).replace("-", " ").strip().title() if slug else f"Truyện #{sid}"
    story = {"id": sid, "title": title.strip(), "manual": True}
    _save_state(stories=[s for s in _state().get("stories", []) if s["id"] != sid] + [story])
    log.info("Story added manually: %s", sid)
    return story


class PublishService:
    def __init__(self, chapters: ChapterService = chapter_service, job_manager: JobManager = default_jobs):
        self.chapters = chapters
        self.jobs = job_manager
        self.bridge = ExtensionBridge(job_manager)
        self.batch: Optional[dict] = None
        self._batch_lock = threading.RLock()

    def login(self) -> dict:
        def done(job, result):
            if result.get("ok"):
                _save_state(logged_in_at=now_iso(), session_expired=False)
        return self.jobs.start("login", {"action": "login", "timeout": config.WATTPAD_LOGIN_TIMEOUT},
                               timeout=config.WATTPAD_LOGIN_TIMEOUT + 60, on_done=done)

    def stories(self) -> dict:
        def done(job, result):
            if result.get("ok"):
                manual = [s for s in _state().get("stories", []) if s.get("manual")]
                found = {s["id"] for s in result["stories"]}
                _save_state(stories=result["stories"] + [s for s in manual if s["id"] not in found],
                            stories_updated_at=now_iso(), has_snapshot=bool(result.get("snapshot") or result.get("screenshot")),
                            logged_in_at=_state().get("logged_in_at") or now_iso(), session_expired=False)
                if result["stories"]:
                    result["message"] = f"Đã tải {len(result['stories'])} truyện."
                else:
                    result["message"] = ("Không tự tìm thấy truyện nào trên trang My Works. Hãy dán link truyện "
                                         "(copy từ thanh địa chỉ khi mở truyện trên Wattpad) vào ô “Thêm truyện bằng link”."
                                         + (f" Ảnh chụp trang: {result['screenshot']}" if result.get("screenshot") else ""))
            elif result.get("error_code") == "WATTPAD_SESSION_EXPIRED":
                _save_state(session_expired=True)
        return self.jobs.start("stories", {"action": "stories"}, timeout=120, on_done=done)

    def publish(self, pid: str, number: int, *, story_id: str, story_title: str, title: str,
                mode: str, confirm: bool, countdown: int = 5, batch: Optional[dict] = None,
                after: Optional[Callable[[dict], None]] = None, part_url: str = "") -> dict:
        """Publish one chapter. `batch`/`after` are used by publish_batch: batch info shown by the
        extension, and a callback that receives the final result (also on failure).
        A chapter already on Wattpad (published before, or `part_url` pasted by the user) updates that part in
        its own story instead of adding a new one."""
        if batch is None and self.batch_running():
            raise JobBusy("Đang có một đợt đăng nhiều chương chạy. Hãy đợi nó xong hoặc bấm Huỷ đợt đăng.")
        store = self.chapters.store
        rec = self.chapters._require(pid, number)
        if not confirm:
            raise StateError("Cần tick “I confirm this is the final version” trước khi đăng.", code="CONFIRM_REQUIRED")
        if not title.strip():
            raise StateError("Tiêu đề chương trống.", code="EMPTY_TITLE")
        if not story_id:
            raise StateError("Chưa chọn truyện trên Wattpad.", code="NO_STORY")
        target = existing_part(rec, part_url)   # before the status change: a bad link must not mark the chapter ready
        if rec["status"] == Status.REVIEWED.value:
            transition(rec, "ready")
        elif rec["status"] != Status.READY_TO_PUBLISH.value:
            raise StateError(f"Chương đang ở trạng thái {rec['status']}. Cần Preview và đánh dấu 'Đã review' trước khi đăng.",
                             code="INVALID_TRANSITION")
        if target:          # update in place: the part's own story, whatever story the publish form shows
            part_id, update = target["part_id"], True
            if target.get("story_id") and target["story_id"] != story_id:
                story_id = target["story_id"]
                story_title = target.get("story_title") or next(
                    (s["title"] for s in _state().get("stories", []) if s["id"] == story_id), story_id)
        else:               # new chapter; a retry reuses the part an earlier attempt created
            wp = rec.get("wattpad") or {}
            part_id, update = (wp.get("part_id") if wp.get("story_id") == story_id else None), False
        store.save_chapter(pid, number, rec)
        if not update:
            store.update_project(pid, wattpad={"story_id": story_id, "story_title": story_title})
        content_hash = json.dumps(rec["draft"], sort_keys=True)
        log.info("Starting publish: project=%s chapter=%d story=%s mode=%s reuse_part=%s update=%s",
                 pid, number, story_id, mode, bool(part_id), update)

        def done(job, result):
            r = self.chapters._require(pid, number)
            entry = {"at": now_iso(), "ok": bool(result.get("ok")), "mode": mode, "story_id": story_id,
                     "story_title": story_title, "title": title, "part_id": result.get("part_id"),
                     "part_url": result.get("part_url"), "error_code": result.get("error_code"),
                     "error": None if result.get("ok") else result.get("message")}
            if result.get("part_id"):
                r["wattpad"] = {"story_id": story_id, "part_id": result["part_id"], "part_url": result.get("part_url")}
            if result.get("ok") and mode == "publish" and result.get("published"):
                if json.dumps(r["draft"], sort_keys=True) != content_hash:
                    log.warning("Draft changed during publish; local DOCX reflects the current draft")
                store.save_chapter(pid, number, r)
                try:
                    path = self.chapters.export(pid, number)
                    entry["docx"] = str(path)
                except Exception as e:  # published remotely: record the export problem, still mark published
                    entry["docx_error"] = str(e)
                r = self.chapters._require(pid, number)
                transition(r, "published")
                verb = "Đã cập nhật chương trên Wattpad" if update else "Đã publish lên Wattpad"
                result["message"] = f"{verb} và lưu DOCX." if "docx" in entry else \
                    f"{verb}, nhưng lưu DOCX thất bại: {entry['docx_error']}"
            elif result.get("ok"):
                result["message"] = "Đã lưu nháp lên Wattpad (chưa publish)."
            elif result.get("error_code") == "WATTPAD_SESSION_EXPIRED":
                _save_state(session_expired=True)
            r.setdefault("publish_history", []).append(entry)
            store.save_chapter(pid, number, r)
            log.info("Publish %s: chapter=%d %s", "successful" if entry["ok"] else "failed", number,
                     entry.get("error_code") or "")
            if after:
                after(result)

        if config.WATTPAD_MODE == "extension":
            return self.bridge.create_publish_task(story_id=story_id, story_title=story_title, title=title.strip(),
                                                   blocks=rec["draft"], mode=mode, part_id=part_id, on_done=done,
                                                   countdown=countdown, batch=batch, update=update)
        return self.jobs.start("publish", {"action": "publish", "story_id": story_id, "title": title.strip(),
                                           "blocks": rec["draft"], "mode": mode, "part_id": part_id, "update": update},
                               timeout=240, on_done=done)

    # ------------------------------------------------------------------ batch
    def batch_running(self) -> bool:
        return bool(self.batch and self.batch["state"] == "running")

    def publish_batch(self, pid: str, numbers: list[int], *, story_id: str, story_title: str, mode: str,
                      confirm: bool, delay: Optional[int] = None) -> dict:
        """Publish (or save as drafts) several chapters one after another. Everything is validated up front,
        so nothing starts when any chapter is unusable."""
        if not confirm:
            raise StateError("Cần tick “Tôi xác nhận các chương này là bản cuối cùng” trước khi đăng.", code="CONFIRM_REQUIRED")
        if not story_id:
            raise StateError("Chưa chọn truyện trên Wattpad.", code="NO_STORY")
        nums = sorted(set(numbers))
        if not nums:
            raise StateError("Chưa chọn chương nào.", code="NO_CHAPTERS")
        if len(nums) > BATCH_MAX:
            raise StateError(f"Mỗi đợt tối đa {BATCH_MAX} chương.", code="TOO_MANY_CHAPTERS")
        with self._batch_lock:
            if self.batch_running():
                raise JobBusy("Đang có một đợt đăng nhiều chương chạy. Hãy đợi nó xong hoặc bấm Huỷ đợt đăng.")
            if self.jobs.busy():
                raise JobBusy("Đang có một thao tác Wattpad khác chạy. Hãy đợi nó xong hoặc bấm Huỷ.")

            items, skipped, problems = [], [], []
            for n in nums:
                rec = self.chapters.store.get_chapter(pid, n)
                try:
                    if rec is None:  # not opened in the editor yet: load it from the Word file (nothing is changed)
                        self.chapters.load(pid, n)
                        rec = self.chapters.store.get_chapter(pid, n)
                except AppError as e:
                    problems.append(f"Chương {n}: {e.message}")
                    continue
                if rec["status"] == Status.PUBLISHED.value:
                    skipped.append({"number": n, "reason": "đã đăng trước đó"})
                    continue
                if not any(b.get("runs") for b in rec["draft"]):
                    problems.append(f"Chương {n}: bản dịch trống.")
                    continue
                if rec.get("confidence") == "low" and rec["status"] == Status.LOADED.value:
                    problems.append(f"Chương {n}: parser không chắc ranh giới chương — hãy mở chương này trong editor để kiểm tra trước.")
                    continue
                title = (rec.get("title") or "").strip()[:200]
                if not title:
                    problems.append(f"Chương {n}: tiêu đề trống.")
                    continue
                items.append({"number": n, "title": title, "state": "queued", "message": "", "part_url": None,
                              "error_code": None, "job_id": None})
            if problems:
                raise StateError("Không thể bắt đầu — có chương chưa đủ điều kiện:\n" + "\n".join(problems),
                                 code="BATCH_INVALID", details={"problems": problems})
            if not items:
                raise StateError("Không còn chương nào cần đăng (tất cả đã ở trạng thái PUBLISHED).", code="NOTHING_TO_PUBLISH")

            batch = {"id": uuid.uuid4().hex[:12], "pid": pid, "story_id": story_id, "story_title": story_title,
                     "mode": mode, "delay": BATCH_DELAY if delay is None else max(0, int(delay)), "state": "running",
                     "items": items, "skipped": skipped, "current": None, "open_url": None,
                     "message": f"Đang bắt đầu đợt {len(items)} chương…", "started_at": now_iso(),
                     "finished_at": None}
            previous, self.batch = self.batch, batch
            log.info("Batch %s started: project=%s story=%s mode=%s chapters=%s", batch["id"], pid, story_id, mode,
                     [i["number"] for i in items])
            try:
                self._start_item(batch, items[0])
            except Exception:
                self.batch = previous  # nothing was started: drop it so the user can simply try again
                raise
            return self._public_batch(batch)

    def _start_item(self, batch: dict, item: dict) -> None:
        """Start the extension task for one chapter. Raises if it cannot be started."""
        pid, number = batch["pid"], item["number"]
        rec = self.chapters.store.get_chapter(pid, number)
        if rec and rec["status"] in (Status.LOADED.value, Status.EDITING.value):
            self.chapters.set_status(pid, number, "review")  # confirming the batch counts as the review
        total = len(batch["items"])
        index = batch["items"].index(item) + 1
        job = self.publish(pid, number, story_id=batch["story_id"], story_title=batch["story_title"],
                           title=item["title"], mode=batch["mode"], confirm=True, countdown=BATCH_COUNTDOWN,
                           batch={"id": batch["id"], "index": index, "total": total},
                           after=lambda result: self._item_done(batch, item, result))
        item["state"], item["job_id"], batch["current"] = "running", job["id"], number
        batch["message"] = f"Đang đăng chương {number} ({index}/{total})…"
        if not batch["open_url"]:
            batch["open_url"] = job.get("open_url")

    def _item_done(self, batch: dict, item: dict, result: dict) -> None:
        with self._batch_lock:
            ok = bool(result.get("ok"))
            cancelled = result.get("error_code") == "CANCELLED"
            item.update(state="done" if ok else ("cancelled" if cancelled else "failed"),
                        message=result.get("message") or "", part_url=result.get("part_url"),
                        error_code=None if ok else result.get("error_code"))
            batch["current"] = None
            if batch["state"] != "running":  # cancelled from the app; the rest is already marked
                return
            queued = [i for i in batch["items"] if i["state"] == "queued"]
            if not ok:
                for i in queued:
                    i.update(state="skipped", message="Bỏ qua vì chương trước chưa đăng được.")
                self._finish_batch(batch, "cancelled" if cancelled else "error",
                                   f"Đã huỷ ở chương {item['number']}." if cancelled
                                   else f"Dừng ở chương {item['number']}: {item['message']}")
            elif not queued:
                self._finish_batch(batch, "done", None)
            else:
                batch["message"] = f"Đã xong chương {item['number']}. Chờ {batch['delay']} giây rồi sang chương kế…"
                self._later(max(1, batch["delay"]), batch)

    def _later(self, seconds: float, batch: dict, attempt: int = 0) -> None:
        timer = threading.Timer(seconds, self._advance, args=(batch, attempt))
        timer.daemon = True
        timer.start()

    def _advance(self, batch: dict, attempt: int = 0) -> None:
        with self._batch_lock:
            if batch["state"] != "running":
                return
            item = next((i for i in batch["items"] if i["state"] == "queued"), None)
            if item is None:
                return self._finish_batch(batch, "done", None)
            try:
                self._start_item(batch, item)
            except JobBusy:
                if attempt < 20:  # the previous job is still closing: try again shortly
                    return self._later(1.5, batch, attempt + 1)
                self._item_done(batch, item, {"ok": False, "error_code": "JOB_BUSY",
                                              "message": "Wattpad đang bận với một thao tác khác."})
            except AppError as e:
                self._item_done(batch, item, {"ok": False, "error_code": e.code, "message": e.message})
            except Exception as e:  # never let a timer thread die silently with the batch stuck on "running"
                log.exception("Batch %s: cannot start chapter %s", batch["id"], item["number"])
                self._item_done(batch, item, {"ok": False, "error_code": "INTERNAL", "message": str(e)})

    def _finish_batch(self, batch: dict, state: str, message: Optional[str]) -> None:
        done = [i for i in batch["items"] if i["state"] == "done"]
        verb = "đăng" if batch["mode"] == "publish" else "lưu nháp"
        batch.update(state=state, current=None, finished_at=now_iso(),
                     message=message or f"Đã {verb} {len(done)}/{len(batch['items'])} chương.")
        log.info("Batch %s finished: %s (%d/%d) %s", batch["id"], state, len(done), len(batch["items"]), message or "")

    def cancel_batch(self, batch_id: str) -> Optional[dict]:
        with self._batch_lock:
            batch = self.batch
            if not batch or batch["id"] != batch_id:
                raise StateError("Không tìm thấy đợt đăng này.", code="BATCH_NOT_FOUND")
            if batch["state"] != "running":
                return self._public_batch(batch)
            running = next((i for i in batch["items"] if i["state"] == "running"), None)
            for i in batch["items"]:
                if i["state"] == "queued":
                    i.update(state="cancelled", message="Đã huỷ.")
            self._finish_batch(batch, "cancelled", "Đã huỷ đợt đăng. Các chương chưa chạy giữ nguyên.")
            if running and running.get("job_id"):
                self.bridge.cancel(running["job_id"])  # -> _item_done marks the running chapter cancelled
            return self._public_batch(batch)

    def batch_status(self) -> Optional[dict]:
        batch = self.batch
        if not batch:
            return None
        running = next((i for i in batch["items"] if i["state"] == "running"), None)
        if running and running.get("job_id"):
            try:
                self.jobs.get(running["job_id"])  # a stalled extension task times out lazily, on read
            except AppError:
                pass
        with self._batch_lock:
            return self._public_batch(batch)

    @staticmethod
    def _public_batch(batch: dict) -> dict:
        return {**{k: v for k, v in batch.items() if k != "items"},
                "items": [{k: v for k, v in i.items() if k != "job_id"} for i in batch["items"]]}


publisher = PublishService()
