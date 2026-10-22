"""Chapter workflow service: open book → list chapters → load → edit → review → export.

Glue between document/, storage/ and the lifecycle. No HTTP and no Wattpad logic here.
"""
from __future__ import annotations

import hashlib
import pickle
import threading
import zipfile
from pathlib import Path
from typing import Optional

from .. import config
from ..document.chapter_parser import MISSING_WARNING_MARK, ParseResult, extract_chapter, missing_warning, parse_chapters
from ..document.docx_reader import read_docx
from ..document.docx_writer import write_chapter_docx
from ..document.models import Block, Run
from ..errors import ChapterError, DocxError, StateError
from ..logging_setup import get_logger
from ..storage.project_state import ProjectStore, content_hash, now_iso, store as default_store
from . import arcs
from .chapter_state import Status, transition
from .validation import validate_translation

log = get_logger("EDITOR")


# Changes to the reader/parser code invalidate the on-disk parse cache automatically.
_DOC_DIR = Path(__file__).resolve().parent.parent / "document"
PARSER_VERSION = hashlib.sha1(b"".join((_DOC_DIR / f).read_bytes() for f in
                                       ("docx_reader.py", "chapter_parser.py", "models.py"))).hexdigest()[:12]


def default_title(number: int, vi_title: str) -> str:
    return f"Chương {number}" + (f": {vi_title}" if vi_title else "")


def _dump(blocks: list[Block]) -> list[dict]:
    return [b.model_dump() for b in blocks]


def _load(blocks: list[dict]) -> list[Block]:
    return [Block.model_validate(b) for b in blocks]


class ChapterService:
    def __init__(self, store: ProjectStore = default_store, output_dir: Path | None = None):
        self.store = store
        self.output_dir = Path(output_dir or config.OUTPUT_DIR)
        self._cache: dict[tuple, tuple[list[Block], ParseResult]] = {}
        self._lock = threading.Lock()

    # --- document ----------------------------------------------------------
    def _parsed(self, pid: str) -> tuple[list[Block], ParseResult]:
        """Parsed document, cached in memory and on disk (big novels take ~20s to read)."""
        path = self.store.source_path(pid)
        if not path.exists():
            raise DocxError(f"File không tồn tại: {path}", code="DOCX_NOT_FOUND")
        st = path.stat()
        key = (str(path), st.st_mtime_ns, st.st_size, PARSER_VERSION)
        with self._lock:
            if key not in self._cache:
                cache_file = self.store.dir(pid) / "parsed.pkl"
                parsed = None
                try:
                    with open(cache_file, "rb") as f:
                        data = pickle.load(f)
                    if data.get("key") == key:
                        parsed = data["value"]
                        log.info("Parse cache hit: %s", path.name)
                except (OSError, EOFError, pickle.UnpicklingError, AttributeError, KeyError, TypeError):
                    parsed = None
                if parsed is None:
                    blocks = read_docx(path)
                    parsed = (blocks, parse_chapters(blocks))
                    try:
                        tmp = cache_file.with_suffix(".tmp")
                        with open(tmp, "wb") as f:
                            pickle.dump({"key": key, "value": parsed}, f, protocol=pickle.HIGHEST_PROTOCOL)
                        tmp.replace(cache_file)
                    except OSError as e:
                        log.warning("Could not write parse cache: %s", e)
                # keep at most 2 documents in memory (a large novel is ~100MB of objects)
                self._cache = {k: v for k, v in list(self._cache.items())[-1:] if k[0] != key[0]} | {key: parsed}
            return self._cache[key]

    def open_path(self, path: str) -> dict:
        p = Path(path.strip().strip('"'))
        if not p.exists():
            raise DocxError(f"File không tồn tại: {p}", code="DOCX_NOT_FOUND")
        if p.suffix.lower() != ".docx":
            raise DocxError("Chỉ hỗ trợ file .docx.", code="DOCX_BAD_TYPE")
        try:  # cheap validity check; the full read happens once, in index()
            with zipfile.ZipFile(p) as zf:
                zf.getinfo("word/document.xml")
        except (zipfile.BadZipFile, KeyError, OSError) as e:
            raise DocxError(f"Không đọc được file Word (file hỏng hoặc không phải .docx): {e}", code="DOCX_UNREADABLE")
        return self.store.upsert_project(p.name, source_path=str(p))

    def upload(self, filename: str, data: bytes) -> dict:
        if not filename.lower().endswith(".docx"):
            raise DocxError("Chỉ hỗ trợ file .docx.", code="DOCX_BAD_TYPE")
        if not data:
            raise DocxError("File rỗng.", code="DOCX_UNREADABLE")
        project = self.store.upsert_project(filename, uploaded_bytes=data)
        try:
            self._parsed(project["id"])
        except DocxError:
            raise
        return project

    def index(self, pid: str) -> dict:
        project = self.store.get_project(pid)
        _, result = self._parsed(pid)
        statuses = self.store.chapter_statuses(pid)
        chapters = result.summary()
        for c in chapters:
            c["status"] = (statuses.get(c["number"]) or {}).get("status")
        for n in statuses.keys() - result.chapters.keys():          # chapters added by hand (not in the Word file)
            rec = self.store.get_chapter(pid, n)
            if rec and rec.get("manual"):
                chapters.append({"number": n, "confidence": "high", "has_ko": bool(rec["ko_blocks"]),
                                 "has_vi": bool(rec["original_vi"]), "manual": True, "warnings": [],
                                 "title": rec.get("vi_title", ""), "status": rec["status"]})
        chapters.sort(key=lambda c: c["number"])
        missing, gap = missing_warning([c["number"] for c in chapters])
        warnings = [w for w in result.warnings if MISSING_WARNING_MARK not in w] + gap
        stats = {"chapter_count": len(chapters),
                 "chapter_range": [chapters[0]["number"], chapters[-1]["number"]] if chapters else None,
                 "last_opened_at": now_iso()}
        project = self.store.update_project(pid, **stats)
        return {"project": project, "chapters": chapters, "warnings": warnings, "missing": missing[:300]}

    def add_missing(self, pid: str, number: int, ko_text: str, vi_text: str, title: str = "") -> dict:
        """Create a chapter that is not in the Word file from pasted text (one paragraph per line).
        The Word file is never touched; the chapter lives only in the project store."""
        _, result = self._parsed(pid)
        if number in result.chapters or self.store.get_chapter(pid, number):
            raise ChapterError(f"Chương {number} đã có — hãy mở chương đó để sửa.", code="CHAPTER_EXISTS")
        para = lambda text: [Block(runs=[Run(text=t.strip())]) for t in text.splitlines() if t.strip()]  # noqa: E731
        ko, vi = _dump(para(ko_text)), _dump(para(vi_text))
        if not ko and not vi:
            raise ChapterError("Hãy dán bản tiếng Hàn và/hoặc bản tiếng Việt của chương.", code="EMPTY_CHAPTER")
        title = title.strip() or arcs.assigned_title(pid, number) or default_title(number, "")
        self.store.save_chapter(pid, number, {
            "number": number, "manual": True, "status": Status.LOADED.value, "draft": vi, "original_vi": vi,
            "ko_blocks": ko, "title": title, "exports": [], "publish_history": [], "status_history": [],
            "created_at": now_iso(), "source_hash": content_hash(vi), "ko_choice": 0, "vi_choice": 0,
            "ko_options": 1, "vi_options": 1, "confidence": "high", "warnings": [], "ko_title": "", "vi_title": "",
            "ko_heading": "", "vi_heading": ""})
        log.info("Added missing chapter %d by hand (ko=%d, vi=%d blocks)", number, len(ko), len(vi))
        return self.index(pid)

    # --- chapter lifecycle ------------------------------------------------------
    def _require(self, pid: str, number: int) -> dict:
        rec = self.store.get_chapter(pid, number)
        if rec is None:
            raise ChapterError(f"Chương {number} chưa được load.", code="CHAPTER_NOT_LOADED")
        return rec

    def load(self, pid: str, number: int, ko_choice: int = 0, vi_choice: int = 0) -> dict:
        blocks, result = self._parsed(pid)
        rec = self.store.get_chapter(pid, number)
        if rec and rec.get("manual") and number not in result.chapters:
            self.store.update_project(pid, last_chapter=number, last_opened_at=now_iso())
            return self._with_issues(pid, dict(rec, notices=["Chương này được thêm thủ công (không có trong file Word)."]))
        content = extract_chapter(blocks, result, number, ko_choice, vi_choice)
        original = _dump(content.vi_blocks)
        src_hash = content_hash(original)
        rec = self.store.get_chapter(pid, number)
        notices: list[str] = []

        fresh = rec is None or (rec.get("ko_choice"), rec.get("vi_choice")) != (ko_choice, vi_choice)
        if fresh:
            rec = {"number": number, "status": Status.LOADED.value, "draft": original,
                   "title": arcs.assigned_title(pid, number) or default_title(number, content.vi_title), "exports": [], "publish_history": [],
                   "status_history": [], "created_at": now_iso()}
        elif rec.get("source_hash") != src_hash:
            if rec["status"] == Status.LOADED.value:
                rec["draft"] = original
            else:
                notices.append("Bản dịch trong file Word đã thay đổi kể từ khi bạn tạo draft. "
                               "Draft của bạn được giữ nguyên — bấm Reset nếu muốn lấy bản mới từ Word.")
        rec.update({
            "ko_choice": ko_choice, "vi_choice": vi_choice, "ko_options": content.ko_options,
            "vi_options": content.vi_options, "confidence": content.confidence, "warnings": content.warnings,
            "ko_title": content.ko_title, "vi_title": content.vi_title,
            "ko_heading": content.ko_heading, "vi_heading": content.vi_heading,
            "ko_blocks": _dump(content.ko_blocks), "original_ko": _dump(content.ko_blocks), "original_vi": original, "source_hash": src_hash,
        })
        if not fresh and "ko_edited" in rec:        # the user's fixed Korean text wins over the Word file until Reset
            rec["ko_blocks"] = rec["ko_edited"]
        self.store.save_chapter(pid, number, rec)
        self.store.update_project(pid, last_chapter=number, last_opened_at=now_iso())
        rec = dict(rec, notices=notices)
        return self._with_issues(pid, rec)

    def get(self, pid: str, number: int) -> dict:
        return self._with_issues(pid, self._require(pid, number))

    def _with_issues(self, pid: str, rec: dict) -> dict:
        rec = dict(rec)
        rec["issues"] = validate_translation(_load(rec["draft"]), _load(rec.get("ko_blocks", [])),
                                             self.store.get_glossary(pid))
        return rec

    def save_draft(self, pid: str, number: int, blocks: list[Block], title: Optional[str],
                   ko_blocks: Optional[list[Block]] = None) -> dict:
        rec = self._require(pid, number)
        new = _dump(blocks)
        new_title = (title or "").strip() or rec["title"]
        if ko_blocks is not None and (ko := _dump(ko_blocks)) != rec.get("ko_blocks"):
            rec["ko_blocks"] = rec["ko_edited"] = ko
            transition(rec, "edit")
            log.info("Korean text edited: project=%s chapter=%d blocks=%d", pid, number, len(ko))
        if new != rec["draft"] or new_title != rec["title"]:
            rec["draft"], rec["title"] = new, new_title
            transition(rec, "edit")
            log.info("Draft updated: project=%s chapter=%d blocks=%d", pid, number, len(new))
        self.store.save_chapter(pid, number, rec)
        return self._with_issues(pid, rec)

    def reset(self, pid: str, number: int) -> dict:
        rec = self._require(pid, number)
        rec["draft"] = rec["original_vi"]
        if rec.pop("ko_edited", None) is not None:
            rec["ko_blocks"] = rec.get("original_ko", rec["ko_blocks"])
        rec["title"] = default_title(number, rec.get("vi_title", ""))
        transition(rec, "edit")
        log.info("Draft reset to Word original: chapter=%d", number)
        self.store.save_chapter(pid, number, rec)
        return self._with_issues(pid, rec)

    def set_status(self, pid: str, number: int, event: str, confirm: bool = False) -> dict:
        rec = self._require(pid, number)
        if event == "ready" and not confirm:
            raise StateError("Cần xác nhận 'Đây là bản cuối cùng' trước khi đánh dấu sẵn sàng đăng.",
                             code="CONFIRM_REQUIRED")
        if event in ("review", "ready") and not any(b.get("runs") for b in rec["draft"]):
            raise StateError("Bản dịch trống — không thể review.", code="EMPTY_DRAFT")
        transition(rec, event)
        log.info("Chapter %d status -> %s", number, rec["status"])
        self.store.save_chapter(pid, number, rec)
        return self._with_issues(pid, rec)

    def export(self, pid: str, number: int) -> Path:
        rec = self._require(pid, number)
        path = self.output_dir / pid / f"Chapter_{number}.docx"
        write_chapter_docx(path, rec["title"], _load(rec["draft"]), protect=[self.store.source_path(pid)])
        rec.setdefault("exports", []).append({"path": str(path), "at": now_iso(), "hash": content_hash(rec["draft"])})
        self.store.save_chapter(pid, number, rec)
        return path


service = ChapterService()
