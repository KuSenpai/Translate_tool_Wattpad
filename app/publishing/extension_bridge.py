"""Bridge to the "Novel Translator – Wattpad Helper" browser extension (folder `extension/`).

Why: the user's everyday Edge is already logged in to Wattpad (Google/Facebook login) and has their
VPN extension. Browsers do not allow automating that profile, so instead a small extension running
there does the Wattpad side and talks to this local server:

    app  --(publish task)-->  /api/ext/task  <--polls--  extension (in the user's Edge, on wattpad.com)
    app  <--(progress / result, story list, page diagnostics)--  extension

Only requests carrying the `X-NT-Extension` header are accepted (web pages cannot send custom headers
to this server: the CORS preflight is never answered), so other websites cannot drive the bridge.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

from .. import config
from ..errors import AppError, NotFound
from ..logging_setup import get_logger
from ..storage.project_state import now_iso
from .jobs import JobManager
from .wattpad_publisher import SELECTORS, blocks_to_html, blocks_to_text

log = get_logger("WATTPAD")
TASK_TIMEOUT = 15 * 60
CLAIM_TTL = 90  # seconds a browser tab keeps its claim on the task without polling / reporting


class ExtensionAuthError(AppError):
    status = 403
    code = "EXTENSION_ONLY"


class ExtensionBridge:
    def __init__(self, job_manager: JobManager):
        self.jobs = job_manager
        self.task: Optional[dict] = None
        self.last_seen: Optional[float] = None
        self.last_seen_iso: Optional[str] = None
        self.version: Optional[str] = None  # version of the extension that talked to us last

    # ------------------------------------------------------------------ app side
    def touch(self) -> None:
        self.last_seen, self.last_seen_iso = time.time(), now_iso()

    def connected(self) -> bool:
        return bool(self.last_seen and time.time() - self.last_seen < 120)

    def create_publish_task(self, *, story_id: str, story_title: str, title: str, blocks: list[dict], mode: str,
                            part_id: Optional[str], on_done: Callable[[dict, dict], None],
                            countdown: int = 5, batch: Optional[dict] = None, update: bool = False) -> dict:
        hint = ("" if self.connected() else
                " Chưa thấy extension kết nối — hãy chắc chắn đã cài “Novel Translator – Wattpad Helper” vào Edge.")
        job = self.jobs.start_external(
            "publish", "Đang mở Wattpad trong Edge của bạn… (extension sẽ tự điền và đăng)" + hint,
            timeout=TASK_TIMEOUT,
            timeout_message="Extension không báo kết quả. Kiểm tra: đã cài extension chưa, tab Wattpad còn mở không, "
                            "VPN có bật không, đã đăng nhập Wattpad trong Edge chưa.",
            on_done=on_done)
        base = config.WATTPAD_BASE_URL.rstrip("/")
        # Retry -> the part created before; otherwise the story page (the extension opens its latest part from there).
        open_url = base + (SELECTORS["part_edit_path"].format(story_id=story_id, part_id=part_id) if part_id
                           else SELECTORS["story_path"].format(story_id=story_id))
        self.task = {"id": job["id"], "story_id": story_id, "story_title": story_title, "title": title,
                     "html": blocks_to_html(blocks), "text": blocks_to_text(blocks), "mode": mode,
                     "part_id": part_id, "from_part": None, "stage": "pending", "open_url": open_url,
                     "countdown": countdown, "batch": batch, "update": update, "created_at": now_iso()}
        log.info("Extension task %s created: story=%s mode=%s reuse_part=%s update=%s",
                 job["id"], story_id, mode, bool(part_id), update)
        return {**job, "open_url": open_url}

    def cancel(self, job_id: str) -> None:
        if self.task and self.task["id"] == job_id:
            self.jobs.finish(job_id, {"ok": False, "error_code": "CANCELLED", "message": "Đã huỷ."})
            self.task = None

    # ------------------------------------------------------------------ extension side
    def ext_config(self) -> dict:
        self.touch()
        return {"base": config.WATTPAD_BASE_URL.rstrip("/"), "selectors": SELECTORS.get("ext", {}),
                "story_path": SELECTORS["story_path"], "part_edit_path": SELECTORS["part_edit_path"],
                "part_public_url": SELECTORS["part_public_url"], "story_link_regex": SELECTORS["story_link_regex"],
                "part_url_regex": SELECTORS["part_url_regex"], "story_title_ignore": SELECTORS["story_title_ignore"]}

    def current_task(self, tab: str = "") -> Optional[dict]:
        """The task for the polling tab. With several Wattpad tabs open only the first one to poll gets it
        (its claim is kept alive by polling / progress events), so two tabs never drive the same task."""
        self.touch()
        if not self.task:
            return None
        job = self.jobs.jobs.get(self.task["id"])
        if job is None or self.jobs.get(self.task["id"])["state"] != "running":
            self.task = None
            return None
        if tab:
            owner, seen = self.task.get("_claim", (tab, 0.0))
            if owner != tab and time.time() - seen < CLAIM_TTL:
                return None
            self.task["_claim"] = (tab, time.time())
        return {k: v for k, v in self.task.items() if not k.startswith("_")}

    def event(self, task_id: str, data: dict) -> dict:
        self.touch()
        if not self.task or self.task["id"] != task_id:
            raise NotFound("Task không còn hiệu lực (đã xong, bị huỷ hoặc hết hạn).")
        if data.get("tab") and self.task.get("_claim", (data["tab"], 0.0))[0] == data["tab"]:
            self.task["_claim"] = (data["tab"], time.time())
        for key in ("stage", "part_id"):
            if data.get(key):
                self.task[key] = str(data[key])
        if "from_part" in data:  # editor page the "New Part" click started from ("" = not from an editor)
            self.task["from_part"] = str(data["from_part"]) or None
        if data.get("part_gone") and self.task.get("update"):   # never add a duplicate of an already-published chapter
            data = {"final": True, "ok": False, "error_code": "WATTPAD_PART_NOT_FOUND",
                    "message": f"Không mở được chương #{self.task['part_id']} trên Wattpad để cập nhật (đã xoá, sai link hoặc "
                               "không thuộc truyện này). Không tạo chương mới — kiểm tra link rồi thử lại."}
        elif data.get("part_gone"):  # the part created in an earlier attempt no longer exists on Wattpad
            self.task.update(part_id=None, stage="pending", from_part=None)
        if data.get("message"):
            self.jobs.progress(task_id, str(data["message"])[:300])
        if data.get("final"):
            part_id = self.task.get("part_id")
            result = {"ok": bool(data.get("ok")), "published": bool(data.get("published")), "part_id": part_id,
                      "part_url": (config.WATTPAD_BASE_URL.rstrip("/") + SELECTORS["part_public_url"].format(part_id=part_id))
                      if part_id else None,
                      "error_code": data.get("error_code"), "message": data.get("message"),
                      "via": "extension"}
            if data.get("diag"):
                result["diagnostics"] = self.save_diag(data["diag"], "failed-step")
            self.task = None
            self.jobs.finish(task_id, result)
        return {"ok": True}

    def save_diag(self, diag: dict, name: str = "page") -> str:
        d = config.LOG_DIR / "wattpad"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"ext-{time.strftime('%Y%m%d-%H%M%S')}-{name}.json"
        path.write_text(json.dumps(diag, ensure_ascii=False, indent=1)[:2_000_000], encoding="utf-8")
        log.info("Extension diagnostics saved: %s", path.name)
        return str(path)
