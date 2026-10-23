"""Background translation jobs: one job = one source file, chapters translated one by one (or a few
in parallel), progress saved after every chapter so a job survives restarts and can be paused,
resumed and retried.

    data/translations/sources/<sid>/source.(txt|docx), meta.json
    data/translations/jobs/<job_id>/job.json, results/<idx>.json
    output/translations/<name>_vi.docx (+ .txt for .txt input)
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from .. import config
from ..document.models import Block, Run
from ..errors import AppError, NotFound
from ..logging_setup import get_logger
from ..storage.project_state import _read_json, _write_json, now_iso, slugify
from .llm_service import LLMAuthError, LLMError, LLMNotConfigured, LLMRateLimit, LLMTimeout
from .novel_translator import (DEFAULT_MODEL, DEFAULT_STYLE, MODELS, LLMRefused, SourceChapter, chapter_paragraphs,
                               cost_usd, estimate, load_source, make_translator, normalize_vi_heading, split_source)

log = get_logger("TRANSLATE")

ROOT = config.DATA_DIR / "translations"
SOURCES = ROOT / "sources"
JOBS = ROOT / "jobs"
STYLE_FILE = ROOT / "style.txt"
OUT_DIR = config.OUTPUT_DIR / "translations"
RETRYABLE = (LLMRateLimit, LLMTimeout)


def reset_delay(text: str, now: Optional[float] = None) -> float:
    """Seconds until a usage limit resets, read from the provider's message (+2 min margin);
    30 min when the message gives no time (the job just tries again then)."""
    from datetime import datetime, timedelta
    now = time.time() if now is None else now
    if m := re.search(r"\|(\d{10})\b", text):                          # Claude Code: "...limit reached|1790000000"
        at = float(m.group(1))
    elif m := re.search(r"resets? in\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m)?\s*(?:(\d+)\s*s)?", text, re.I):
        at = now + int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + int(m.group(3) or 0)  # Antigravity
    elif m := re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", text, re.I):   # Claude Code: "resets 5pm"
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        when = datetime.fromtimestamp(now).replace(hour=h, minute=int(m.group(2) or 0), second=0, microsecond=0)
        at = (when if when.timestamp() > now else when + timedelta(days=1)).timestamp()
    else:
        return 30 * 60
    return min(max(at - now, 0) + 120, 24 * 3600)


class TranslateError(AppError):
    code = "TRANSLATE_ERROR"


# --------------------------------------------------------------------------- style guide
def get_style() -> str:
    try:
        return STYLE_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return DEFAULT_STYLE


def save_style(text: str) -> str:
    STYLE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STYLE_FILE.write_text(text.strip() or DEFAULT_STYLE, encoding="utf-8")
    return get_style()


# --------------------------------------------------------------------------- sources
def _source_dir(sid: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{12}", sid or ""):
        raise NotFound("Nguồn không hợp lệ.")
    return SOURCES / sid


def analyze(name: str, data: bytes) -> dict:
    suffix = Path(name).suffix.lower()
    if suffix not in (".txt", ".docx"):
        raise TranslateError("Chỉ hỗ trợ file .txt hoặc .docx.", code="DOCX_BAD_TYPE")
    if not data:
        raise TranslateError("File rỗng.", code="DOCX_UNREADABLE")
    sid = uuid.uuid4().hex[:12]
    d = SOURCES / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / f"source{suffix}").write_bytes(data)
    try:
        return _analyze_saved(sid, name)
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise


def analyze_path(path: str) -> dict:
    p = Path(path.strip().strip('"'))
    if not p.exists():
        raise TranslateError(f"File không tồn tại: {p}", code="DOCX_NOT_FOUND")
    return analyze(p.name, p.read_bytes())


def _source_file(sid: str) -> Path:
    d = _source_dir(sid)
    for f in d.glob("source.*"):
        return f
    raise NotFound("Không tìm thấy file nguồn.")


def _analyze_saved(sid: str, name: str) -> dict:
    blocks, kind = load_source(_source_file(sid))
    chapters, warnings = split_source(blocks)
    if not chapters:
        raise TranslateError(warnings[0] if warnings else "Không tìm thấy chương nào.", code="NO_CHAPTERS")
    todo = [c for c in chapters if c.lang == "ko" and not c.has_translation]
    if not todo:
        warnings.append("Không có chương tiếng Hàn nào cần dịch (mọi chương đã có bản Việt).")
    chars = sum(c.chars for c in todo)
    meta = {"sid": sid, "name": Path(name).name, "kind": kind, "created_at": now_iso(),
            "chapters": [c.to_dict() for c in chapters], "warnings": warnings,
            "todo": len(todo), "already_translated": sum(c.has_translation for c in chapters),
            "chars": chars, "estimate": estimate(chars, len(todo))}
    _write_json(_source_dir(sid) / "meta.json", meta)
    log.info("Analyzed %s: %d chapters, %d to translate, %d chars", name, len(chapters), len(todo), chars)
    return meta


def source_meta(sid: str) -> dict:
    meta = _read_json(_source_dir(sid) / "meta.json")
    if not meta:
        raise NotFound("Không tìm thấy nguồn — hãy tải file lên lại.")
    return meta


# --------------------------------------------------------------------------- jobs
class TranslationManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._stop: dict[str, threading.Event] = {}
        self._recover()

    # ---- persistence
    def _dir(self, jid: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{12}", jid or ""):
            raise NotFound("Job không hợp lệ.")
        return JOBS / jid

    def _job(self, jid: str) -> dict:
        job = _read_json(self._dir(jid) / "job.json")
        if not job:
            raise NotFound("Không tìm thấy job dịch.")
        return job

    def _save(self, job: dict) -> None:
        job["updated_at"] = now_iso()
        _write_json(self._dir(job["id"]) / "job.json", job)

    def _result(self, jid: str, idx: int) -> Optional[dict]:
        return _read_json(self._dir(jid) / "results" / f"{idx}.json")

    def _recover(self) -> None:
        """After a restart, jobs that were running are paused (the user resumes them); jobs waiting for a
        usage limit to reset get their automatic resume scheduled again."""
        for f in JOBS.glob("*/job.json"):
            job = _read_json(f)
            if job and job.get("status") == "paused" and job.get("auto_resume_at"):
                self._schedule_resume(job["id"], job["auto_resume_at"])
            if job and job.get("status") == "running":
                job["status"], job["message"] = "paused", "Tạm dừng vì tool đã khởi động lại — bấm Tiếp tục."
                _write_json(f, job)
                for ch in job["chapters"]:
                    if ch["status"] == "running":
                        ch["status"] = "pending"
                _write_json(f, job)

    # ---- public API
    def create(self, *, sid: str, model: str, effort: str, concurrency: int, from_number: Optional[int],
               to_number: Optional[int], glossary_project: Optional[str], style: str) -> dict:
        meta = source_meta(sid)
        if model not in MODELS:
            raise TranslateError(f"Model không hỗ trợ: {model}", code="BAD_MODEL")
        make_translator(model, effort)  # fails early (e.g. missing API key) before creating the job
        chapters = []
        for c in meta["chapters"]:
            if c["lang"] != "ko" or c["has_translation"]:
                continue
            if from_number is not None and c["number"] < from_number:
                continue
            if to_number is not None and c["number"] > to_number:
                continue
            chapters.append({"idx": c["idx"], "number": c["number"], "heading": c["heading"], "chars": c["chars"],
                             "status": "pending", "error": None, "attempts": 0})
        if not chapters:
            raise TranslateError("Không có chương nào trong khoảng đã chọn cần dịch.", code="NOTHING_TO_DO")
        jid = uuid.uuid4().hex[:12]
        job = {"id": jid, "sid": sid, "name": meta["name"], "kind": meta["kind"], "model": model, "effort": effort,
               "concurrency": max(1, min(4, int(concurrency))), "glossary_project": glossary_project or None,
               "style": style.strip() or get_style(), "status": "queued", "message": "", "created_at": now_iso(),
               "chapters": chapters, "auto_terms": {}, "usage": {}, "cost_usd": 0.0, "output": None}
        self._dir(jid).mkdir(parents=True, exist_ok=True)
        self._save(job)
        log.info("Job %s created: %s, %d chapters, model=%s", jid, meta["name"], len(chapters), model)
        self.start(jid)
        return self.get(jid)

    def list(self) -> list[dict]:
        out = []
        for f in JOBS.glob("*/job.json"):
            job = _read_json(f)
            if job:
                out.append(self._summary(job))
        return sorted(out, key=lambda j: j["created_at"], reverse=True)

    def get(self, jid: str) -> dict:
        job = self._job(jid)
        s = self._summary(job)
        s["chapters"] = [{k: c[k] for k in ("idx", "number", "heading", "status", "error", "chars")} for c in job["chapters"]]
        s["style"], s["auto_terms"] = job["style"], job["auto_terms"]
        return s

    def _summary(self, job: dict) -> dict:
        counts: dict[str, int] = {}
        for c in job["chapters"]:
            counts[c["status"]] = counts.get(c["status"], 0) + 1
        done = counts.get("done", 0)
        remaining = counts.get("pending", 0) + counts.get("running", 0) + counts.get("failed", 0)
        projected = round(job["cost_usd"] / done * (done + remaining), 2) if done else None
        return {k: job.get(k) for k in ("id", "sid", "name", "kind", "model", "effort", "concurrency", "status",
                                         "message", "created_at", "updated_at", "cost_usd", "usage", "output",
                                         "glossary_project", "auto_resume_at")} | {
            "counts": counts, "total": len(job["chapters"]), "projected_cost_usd": projected,
            "running": jid_alive(self, job["id"])}

    def _schedule_resume(self, jid: str, at: float) -> None:
        # ponytail: in-process timer; survives restarts only through auto_resume_at + _recover
        timer = threading.Timer(max(0.0, at - time.time()), self._auto_resume, (jid, at))
        timer.daemon = True
        timer.start()

    def _auto_resume(self, jid: str, at: float) -> None:
        try:
            job = self._job(jid)
        except NotFound:
            return
        if job.get("auto_resume_at") != at or job["status"] != "paused":
            return          # resumed / paused by hand, or rescheduled since
        log.info("Job %s: usage limit should have reset — resuming", jid)
        self.start(jid)

    def start(self, jid: str) -> dict:
        with self._lock:
            if jid_alive(self, jid):
                return self.get(jid)
            job = self._job(jid)
            if job["status"] == "done" and not any(c["status"] != "done" for c in job["chapters"]):
                return self.get(jid)
            job["status"], job["message"] = "running", "Đang dịch…"
            job.pop("auto_resume_at", None)
            self._save(job)
            stop = threading.Event()
            self._stop[jid] = stop
            t = threading.Thread(target=self._run, args=(jid, stop), daemon=True, name=f"translate-{jid}")
            self._threads[jid] = t
            t.start()
        return self.get(jid)

    def pause(self, jid: str) -> dict:
        if jid in self._stop:
            self._stop[jid].set()
        with self._lock:
            job = self._job(jid)
            if job["status"] == "running":
                job["status"], job["message"] = "pausing", "Đang dừng sau khi xong các chương đang dịch…"
                self._save(job)
            elif job.pop("auto_resume_at", None):           # cancel a scheduled automatic resume
                job["message"] = "Đã tạm dừng (huỷ tự động tiếp tục). Bấm Tiếp tục để dịch phần còn lại."
                self._save(job)
        return self.get(jid)

    def retry_failed(self, jid: str) -> dict:
        with self._lock:
            job = self._job(jid)
            for c in job["chapters"]:
                if c["status"] == "failed":
                    c["status"], c["error"], c["attempts"] = "pending", None, 0
            self._save(job)
        return self.start(jid)

    def retry_chapter(self, jid: str, idx: int) -> dict:
        """Translate one chapter again (e.g. to get a better version)."""
        with self._lock:
            job = self._job(jid)
            for c in job["chapters"]:
                if c["idx"] == idx:
                    c["status"], c["error"], c["attempts"] = "pending", None, 0
            self._save(job)
        return self.start(jid)

    def delete(self, jid: str) -> None:
        if jid_alive(self, jid):
            raise TranslateError("Hãy tạm dừng job trước khi xoá.", code="JOB_RUNNING")
        shutil.rmtree(self._dir(jid), ignore_errors=True)

    # ---- worker
    def _glossary_text(self, job: dict, korean_text: str) -> tuple[str, str]:
        """Glossary lines relevant to this chapter (terms that occur in it) + the story notes."""
        from ..editor.glossary import entries_for_text, glossary_prompt
        from ..storage.project_state import store
        entries, notes = [], ""
        if job.get("glossary_project"):
            try:
                entries = store.get_glossary(job["glossary_project"])
                notes = store.get_notes(job["glossary_project"])
            except Exception:
                entries, notes = [], ""
        known = {e["source"] for e in entries}
        auto = [{"source": s, "target": t} for s, t in job["auto_terms"].items() if s not in known]
        return glossary_prompt(entries_for_text(entries + auto, korean_text)), notes

    def _with_examples(self, job: dict, number: int, korean: str, notes: str, own: list[dict]) -> str:
        """Add how the same characters/terms were rendered in earlier chapters — the ones this job already translated
        (`own`) and the linked project's (see consistency.py)."""
        try:
            from .consistency import with_examples
            previous, sources = list(own), list(job["auto_terms"])
            if pid := job.get("glossary_project"):
                from ..storage.project_state import store
                from .retranslate import previous_chapters      # lazy: retranslate imports this module
                seen = {p["number"] for p in previous}
                previous += [p for p in previous_chapters(pid, number) if p["number"] not in seen]
                sources += [e["source"] for e in store.get_glossary(pid)]
            return with_examples(notes, previous, korean, sources)[0]
        except Exception as e:      # the examples are an aid: never fail the chapter because of them
            log.warning("No consistency examples for chapter %d: %s", number, e)
            return notes

    def _own_previous(self, job: dict, ch: dict, blocks: list, by_idx: dict, count: int = 5) -> list[dict]:
        """Up to `count` chapters before `ch` this job already translated, newest first, as consistency.py wants them.
        With concurrency > 1 the chapter right before may still be running; it is simply not used then."""
        order = [c["idx"] for c in job["chapters"]]
        out = []
        for prev in reversed(order[:order.index(ch["idx"])]):
            if len(out) >= count:
                break
            if r := self._result(job["id"], prev):
                out.append({"number": by_idx[prev].number, "ko_paragraphs": chapter_paragraphs(blocks, by_idx[prev]),
                            "vi_paragraphs": r["paragraphs"]})
        return out

    def _prev_tail(self, job: dict, ch: dict) -> list[str]:
        order = [c["idx"] for c in job["chapters"]]
        pos = order.index(ch["idx"])
        for prev in reversed(order[:pos]):
            r = self._result(job["id"], prev)
            if r:
                return r["paragraphs"][-6:]
        return []

    def _run(self, jid: str, stop: threading.Event) -> None:
        try:
            self._run_inner(jid, stop)
        except Exception as e:  # a job must never stay "running" forever
            log.exception("Job %s crashed: %s", jid, e)
            self._finish(jid, "error", f"Lỗi không mong muốn: {e}")

    def _run_inner(self, jid: str, stop: threading.Event) -> None:
        job = self._job(jid)
        try:
            translator = make_translator(job["model"], job["effort"])
        except LLMError as e:
            self._finish(jid, "error", e.message)
            return
        blocks, _ = load_source(_source_file(job["sid"]))
        meta = source_meta(job["sid"])
        by_idx = {c["idx"]: SourceChapter(**{k: c[k] for k in ("idx", "number", "heading_index", "start", "end",
                                                                "heading", "lang", "has_translation", "chars")})
                  for c in meta["chapters"]}
        fatal: list[str] = []

        def work(ch: dict) -> None:
            if stop.is_set() or fatal:
                return
            src = by_idx[ch["idx"]]
            paragraphs = chapter_paragraphs(blocks, src)
            with self._lock:
                j = self._job(jid)
                c = next(x for x in j["chapters"] if x["idx"] == ch["idx"])
                c["status"] = "running"
                self._save(j)
                glossary, notes = self._glossary_text(j, "\n".join(paragraphs + [src.heading]))
                tail, style = self._prev_tail(j, c), j["style"]
            notes = self._with_examples(j, src.number, "\n".join(paragraphs + [src.heading]), notes,
                                        self._own_previous(j, c, blocks, by_idx))
            log.info("Translating chapter %d (%d paragraphs)", src.number, len(paragraphs))
            result, error, code, usage = None, None, None, {}
            for attempt in range(3):
                try:
                    result = translator.translate(heading=src.heading, paragraphs=paragraphs, glossary_text=glossary,
                                                  prev_tail=tail, style=style, notes=notes)
                    usage = result.usage
                    break
                except RETRYABLE as e:
                    error, code = e.message, e.code
                    if stop.wait(20 * (attempt + 1)):
                        break
                except (LLMAuthError, LLMNotConfigured) as e:
                    error, code = e.message, e.code
                    fatal.append(e.message)
                    break
                except LLMError as e:
                    error, code = e.message, e.code
                    usage = (e.details or {}).get("usage", {})
                    if code == "AI_USAGE_LIMIT":      # subscription / quota used up: pause the whole job
                        fatal.append(e.message + " — tự động tiếp tục khi gói / hạn mức được làm mới.")
                        stop.set()
                        break
                    if isinstance(e, LLMRefused) or code in ("AI_BAD_MODEL",):
                        break
                except Exception as e:  # never kill the worker on one bad chapter
                    log.exception("Chapter %d failed", src.number)
                    error, code = str(e), "INTERNAL"
                    break
            with self._lock:
                j = self._job(jid)
                c = next(x for x in j["chapters"] if x["idx"] == ch["idx"])
                c["attempts"] += 1
                for k, v in usage.items():
                    j["usage"][k] = j["usage"].get(k, 0) + v
                j["cost_usd"] = round(j["cost_usd"] + cost_usd(j["model"], usage), 4)
                if result:
                    vi_heading = normalize_vi_heading(result.heading, src)
                    warn = None
                    if len(result.paragraphs) != len(paragraphs):
                        warn = f"Số đoạn khác bản gốc ({len(result.paragraphs)}/{len(paragraphs)})."
                    _write_json(self._dir(jid) / "results" / f"{src.idx}.json",
                                {"heading": vi_heading, "paragraphs": result.paragraphs, "model": result.model,
                                 "usage": usage, "at": now_iso(), "warning": warn})
                    for t in result.new_terms[:30]:
                        if len(j["auto_terms"]) < 400:
                            j["auto_terms"].setdefault(t["source"].strip(), t["target"].strip())
                    c["status"], c["error"] = "done", warn
                    log.info("Chapter %d done (%s)", src.number, warn or "ok")
                elif code in ("AI_USAGE_LIMIT", "CLAUDE_CODE_LOGIN", "CLAUDE_CODE_MISSING", "CLAUDE_CODE_UPDATE", "ANTIGRAVITY_LOGIN",
                              "ANTIGRAVITY_MISSING", "AI_INVALID_KEY", "AI_NOT_CONFIGURED"):
                    c["status"], c["error"] = "pending", None          # not this chapter's fault: retry on resume
                    log.warning("Chapter %d not translated: %s %s", src.number, code, error)
                else:
                    c["status"], c["error"] = "failed", f"[{code}] {error}"
                    log.warning("Chapter %d failed: %s %s", src.number, code, error)
                self._save(j)

        pending = [c for c in job["chapters"] if c["status"] in ("pending", "running")]
        with ThreadPoolExecutor(max_workers=job["concurrency"]) as pool:
            for _ in pool.map(work, pending):
                pass
        try:
            self.build_output(jid)
        except Exception as e:
            log.exception("Output build failed: %s", e)
        j = self._job(jid)
        if fatal and "tiếp tục khi gói" in fatal[0]:      # usage limit: wait for the reset, then go on by itself
            at = time.time() + reset_delay(fatal[0])
            when = time.strftime("%H:%M %d/%m", time.localtime(at))
            self._finish(jid, "paused", f"{fatal[0].split(' — ')[0]} — tự động tiếp tục lúc {when}.", auto_resume_at=at)
            self._schedule_resume(jid, at)
        elif fatal:
            self._finish(jid, "error", fatal[0])
        elif stop.is_set():
            self._finish(jid, "paused", "Đã tạm dừng. Bấm Tiếp tục để dịch phần còn lại.")
        else:
            failed = sum(c["status"] == "failed" for c in j["chapters"])
            self._finish(jid, "done", f"Xong. {failed} chương lỗi — bấm “Dịch lại chương lỗi”." if failed else "Đã dịch xong tất cả.")

    def _finish(self, jid: str, status: str, message: str, auto_resume_at: Optional[float] = None) -> None:
        with self._lock:
            j = self._job(jid)
            if auto_resume_at:
                j["auto_resume_at"] = auto_resume_at
            for c in j["chapters"]:
                if c["status"] == "running":
                    c["status"] = "pending"
            j["status"], j["message"] = status, message
            self._save(j)
            self._threads.pop(jid, None)
            self._stop.pop(jid, None)
        log.info("Job %s %s: %s", jid, status, message)

    # ---- output
    def build_output(self, jid: str) -> dict:
        """Write the bilingual file: every Korean chapter followed by its translation (when available)."""
        with self._lock:
            job = self._job(jid)
        src_path = _source_file(job["sid"])
        blocks, kind = load_source(src_path)
        meta = source_meta(job["sid"])
        results = {c["idx"]: self._result(jid, c["idx"]) for c in job["chapters"]}
        insert_after: dict[int, tuple[Block, list[Block]]] = {}
        for c in meta["chapters"]:
            r = results.get(c["idx"])
            if not r:
                continue
            ko_heading = blocks[c["heading_index"]]
            vi_heading = Block.model_construct(type=ko_heading.type, level=ko_heading.level, align=ko_heading.align,
                                               runs=[Run.model_construct(text=r["heading"], b=all(x.b for x in ko_heading.runs) if ko_heading.runs else False, i=False, u=False)])
            paras = [Block.model_construct(type="paragraph", level=None, align=None,
                                           runs=[Run.model_construct(text=p, b=False, i=False, u=False)]) for p in r["paragraphs"]]
            last = max(c["end"] - 1, c["heading_index"])
            insert_after[last] = (vi_heading, paras)
        out_blocks: list[Block] = []
        for i, b in enumerate(blocks):
            out_blocks.append(b)
            if i in insert_after:
                h, paras = insert_after[i]
                if kind == "txt":
                    out_blocks.append(Block.model_construct(type="paragraph", level=None, align=None, runs=[]))
                out_blocks.append(h)
                out_blocks.extend(paras)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stem = slugify(Path(job["name"]).stem) or "truyen"
        docx_path = OUT_DIR / f"{stem}_vi_{jid[:6]}.docx"
        _write_bilingual_docx(docx_path, out_blocks)
        out = {"docx": str(docx_path), "translated": len([r for r in results.values() if r]), "at": now_iso()}
        if kind == "txt":
            txt_path = docx_path.with_suffix(".txt")
            txt_path.write_text("\n".join(b.text for b in out_blocks), encoding="utf-8")
            out["txt"] = str(txt_path)
        with self._lock:
            j = self._job(jid)
            j["output"] = out
            self._save(j)
        log.info("Output written: %s (%d chapters translated)", docx_path.name, out["translated"])
        return out


def _write_bilingual_docx(path: Path, blocks: list[Block]) -> None:
    """Like the chapter writer, but headings keep their original level (no extra title heading)."""
    import os
    import tempfile

    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
    from docx.shared import Pt
    align = {"center": WD_ALIGN_PARAGRAPH.CENTER, "right": WD_ALIGN_PARAGRAPH.RIGHT,
             "justify": WD_ALIGN_PARAGRAPH.JUSTIFY, "left": WD_ALIGN_PARAGRAPH.LEFT}
    d = docx.Document()
    d.styles["Normal"].font.name = "Times New Roman"
    d.styles["Normal"].font.size = Pt(13)
    for b in blocks:
        p = d.add_heading("", level=max(1, min(3, b.level or 1))) if b.type == "heading" else d.add_paragraph()
        for r in b.runs:
            parts = r.text.split("\n")
            for k, part in enumerate(parts):
                run = p.add_run(part)
                run.bold, run.italic, run.underline = r.b or None, r.i or None, r.u or None
                if k < len(parts) - 1:
                    run.add_break(WD_BREAK.LINE)
        if b.align in align:
            p.alignment = align[b.align]
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".docx", dir=path.parent)
    os.close(fd)
    try:
        d.save(tmp)
        os.replace(tmp, path)
    except PermissionError:
        Path(tmp).unlink(missing_ok=True)
        raise TranslateError(f"Không ghi được {path.name} — file có thể đang mở trong Word.", code="DOCX_LOCKED")


def jid_alive(mgr: TranslationManager, jid: str) -> bool:
    t = mgr._threads.get(jid)
    return bool(t and t.is_alive())


config.ensure_dirs()
for _d in (SOURCES, JOBS):
    _d.mkdir(parents=True, exist_ok=True)
manager = TranslationManager()
