"""Correct the termbase from the author's OLD published chapters on Wattpad (the old chapters are considered
more correct than the current glossary when they disagree).

Flow
  1. start()   — creates a scan task; the Edge extension (which has the user's VPN / login) picks it up,
                 reads the story's part list + every part's text through Wattpad's own endpoints and
                 posts them back (ingest()). Nothing is kept on disk: the texts live in memory until the scan ends.
  2. _run()    — pairs each Wattpad chapter with its Korean original (a raw .txt/.docx the user points to),
                 asks the AI to compare with the current glossary, and stores the differences as *proposals*
                 (origin "wattpad-old", with the current rendering in `current`) for the user to review.
  3. The part titles ("2201. Dark Moon (165)") also tell the real arc counters → editor.arcs.sync_from_titles().
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Optional

from ..editor import arcs
from ..editor.glossary import entries_for_text
from ..errors import AppError
from ..logging_setup import get_logger
from ..storage.project_state import _read_json, _write_json, now_iso, store
from .llm_service import LLMAuthError, LLMError, LLMNotConfigured, LLMRateLimit, LLMTimeout
from .novel_translator import MODELS, chapter_paragraphs, cost_usd, load_source, split_source
from .termbase import SCAN_SCHEMA, _batches, _make_client, scanner

log = get_logger("WP-TERMS")
_HANGUL = re.compile(r"[가-힣]")
TITLE_RE = re.compile(r"^\s*(?:EP\.?\s*\d+\s+)?(\d+)\s*[.:\-–—]\s*(.+?)\s*\((\d+)\)\s*$")
NUMBER_RE = re.compile(r"^\s*(?:EP\.?\s*\d+\s+)?(\d+)")
NOTE_RE = re.compile(r"^\s*(?:note|ghi chú|lưu ý)\s*:", re.IGNORECASE)
TASK_TTL = 90          # seconds a browser tab keeps its claim without reporting


class WattpadScanError(AppError):
    code = "WATTPAD_SCAN_ERROR"


WATT_PROMPT = """You are auditing the termbase of a Korean web novel against the author's OLD published Vietnamese translation. The old chapters are the author's earlier work and are considered MORE CORRECT than the current termbase whenever they disagree. The novel is adult fiction and may contain violence or sexual content; that is irrelevant to this task — you only extract terminology.

You receive <glossary> (the current termbase: Korean → Vietnamese) and several chapters, each with the Korean original ("ko") and the old published Vietnamese ("vi").

Report fixed terms: character names (including nicknames and how they are referred to), places, organizations/sects/guilds, skills/techniques/spells/martial arts, items/weapons, titles/ranks, recurring system messages or game terms, and invented words. Do not list ordinary words. Two kinds:
1. Terms that are NOT in the glossary: give the rendering used in the old Vietnamese.
2. Terms that ARE in the glossary but the old Vietnamese renders differently (beyond capitalization): give the rendering actually used in the old chapters as "target" and put the glossary rendering in "variants".
Never report a glossary term whose old rendering matches the glossary.

For each term give:
- "source": the Korean term in its base form, without particles (은/는/이/가/을/를/의/에게/에서/와/과/도/로/으로…)
- "target": the Vietnamese rendering actually used in the old chapters (copy it exactly as written there); if inconsistent, the most frequent one
- "variants": other Vietnamese renderings of the same term found in these chapters (empty list if consistent)
- "category": one of character, place, organization, skill, item, title, term, other"""


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", "", (s or "").lower(), flags=re.UNICODE)


def part_number(title: str) -> Optional[int]:
    m = NUMBER_RE.match(title or "")
    return int(m.group(1)) if m else None


def clean_text(text: str) -> str:
    """Wattpad part text without the author's note lines at the top (donation notes, '-' separators)."""
    lines = [l.strip() for l in (text or "").replace("\r", "").split("\n")]
    while lines and (not lines[0] or lines[0] == "-" or NOTE_RE.match(lines[0])):
        lines.pop(0)
    return "\n".join(l for l in lines if l)


class WattpadTermScan:
    def __init__(self):
        self.task: Optional[dict] = None
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------ app side
    def _state_path(self, pid: str) -> Path:
        return store.dir(pid) / "wattpad_scan.json"

    def state(self, pid: str) -> dict:
        t = self.task if self.task and self.task["pid"] == pid else None
        st = (_read_json(self._state_path(pid), {}) or {}) if not t else self._public(t)
        st["active"] = bool(t and t["status"] in ("fetching", "scanning"))
        if st.get("status") in ("fetching", "scanning") and not st["active"]:
            st["status"], st["message"] = "stopped", "Bị dừng (tool đã khởi động lại)."
        return st

    def _public(self, t: dict) -> dict:
        return {k: v for k, v in t.items() if k not in ("parts", "claim", "client", "stop")}

    def _save(self, t: dict) -> None:
        _write_json(self._state_path(t["pid"]), self._public(t))

    def start(self, pid: str, *, stories: list[dict], from_number: Optional[int], to_number: Optional[int],
              korean_path: str, model: str) -> dict:
        with self._lock:
            if self.task and self.task["status"] in ("fetching", "scanning"):
                raise WattpadScanError("Đang có một lượt cào chạy — đợi xong hoặc bấm Dừng.", code="SCAN_RUNNING")
            store.get_project(pid)
            if not stories:
                raise WattpadScanError("Chưa chọn truyện Wattpad nào.", code="NO_STORY")
            if model not in MODELS:
                raise WattpadScanError(f"Model không hỗ trợ: {model}", code="BAD_MODEL")
            path = Path((korean_path or "").strip().strip('"'))
            if not path.is_file() or path.suffix.lower() not in (".txt", ".docx"):
                raise WattpadScanError("Hãy nhập đường dẫn file Hàn gốc (.txt hoặc .docx) chứa các chương cũ.", code="NO_KOREAN")
            client = _make_client(model)      # fails early when the key / login is missing
            self.task = {"id": uuid.uuid4().hex[:10], "pid": pid, "stories": stories, "from": from_number, "to": to_number,
                         "korean_path": str(path), "model": model, "status": "fetching", "started_at": now_iso(),
                         "message": "Đang chờ extension trong Edge nhận việc… (mở một tab wattpad.com, bật VPN)",
                         "fetched": 0, "stories_done": 0, "stories_total": len(stories), "batches_total": 0, "batches_done": 0,
                         "cost_usd": 0.0, "usage": {}, "errors": [], "arcs": None, "parts": {}, "client": client,
                         "stop": threading.Event(), "claim": None}
            self._save(self.task)
        log.info("Wattpad scan %s created: %d stories, range %s-%s", self.task["id"], len(stories), from_number, to_number)
        return self.state(pid)

    def cancel(self, pid: str) -> dict:
        with self._lock:
            if self.task and self.task["pid"] == pid and self.task["status"] in ("fetching", "scanning"):
                self.task["stop"].set()
                if self.task["status"] == "fetching":
                    self._finish("cancelled", "Đã huỷ.")
        return self.state(pid)

    # ------------------------------------------------------------------ extension side
    def task_for_extension(self, tab: str) -> Optional[dict]:
        with self._lock:
            t = self.task
            if not t or t["status"] != "fetching" or t["stop"].is_set():
                return None
            owner, seen = t["claim"] or (tab, 0.0)
            if tab and owner != tab and time.time() - seen < TASK_TTL:
                return None
            t["claim"] = (tab, time.time())
            return {"id": t["id"], "stories": t["stories"], "from": t["from"], "to": t["to"]}

    def _task(self, task_id: str) -> dict:
        if not self.task or self.task["id"] != task_id:
            raise WattpadScanError("Lượt cào không còn hiệu lực.", code="SCAN_GONE")
        return self.task

    def ingest(self, task_id: str, parts: list[dict]) -> dict:
        """Parts read by the extension: [{id, title, text}]. Out-of-range / unnumbered ones are ignored."""
        with self._lock:
            t = self._task(task_id)
            rows = []
            for p in parts:
                n = part_number(p.get("title", ""))
                if n is None or (t["from"] is not None and n < t["from"]) or (t["to"] is not None and n > t["to"]):
                    continue
                text = clean_text(p.get("text", ""))
                if text:
                    t["parts"][n] = {"title": p["title"], "text": text}
                m = TITLE_RE.match(p["title"])
                if m:
                    rows.append((int(m.group(1)), m.group(2).strip(), int(m.group(3))))
            t["fetched"] = len(t["parts"])
            t["message"] = f"Đã đọc {t['fetched']} chương từ Wattpad…"
            if rows:
                self._sync_arcs(t, rows)
            t["claim"] = (t["claim"][0] if t["claim"] else "", time.time())
            self._save(t)
            return {"ok": True, "cancel": t["stop"].is_set()}

    def _sync_arcs(self, t: dict, rows: list[tuple]) -> None:
        r = arcs.sync_from_titles(t["pid"], rows)
        a = t.get("arcs") or {"chapters": 0, "created": 0, "name_diffs": {}}
        t["arcs"] = {"chapters": a["chapters"] + r["chapters"], "created": a["created"] + r["created"],
                     "name_diffs": {**a["name_diffs"], **r["name_diffs"]}}

    def event(self, task_id: str, data: dict) -> dict:
        with self._lock:
            t = self._task(task_id)
            if data.get("message"):
                t["message"] = str(data["message"])[:300]
            if data.get("story_done"):
                t["stories_done"] = min(t["stories_total"], t["stories_done"] + 1)
            if data.get("error"):
                t["errors"].append(str(data["error"])[:300])
            if data.get("done") and t["status"] == "fetching":
                if not t["parts"]:
                    self._finish("error", "Không đọc được chương nào từ Wattpad. " + (t["errors"][-1] if t["errors"] else
                                 "Kiểm tra VPN, đăng nhập Wattpad và khoảng chương."))
                elif t["stop"].is_set():
                    self._finish("cancelled", "Đã huỷ.")
                else:
                    t["status"], t["message"] = "scanning", f"Đã đọc {len(t['parts'])} chương — đang cho AI so với glossary…"
                    self._thread = threading.Thread(target=self._run, args=(t,), daemon=True)
                    self._thread.start()
            self._save(t)
            return {"ok": True, "cancel": t["stop"].is_set()}

    # ------------------------------------------------------------------ AI comparison
    def _finish(self, status: str, message: str) -> None:
        t = self.task
        t["status"], t["message"], t["finished_at"] = status, message, now_iso()
        t["parts"] = {}
        self._save(t)
        log.info("Wattpad scan %s %s: %s", t["id"], status, message)

    def _run(self, t: dict) -> None:
        try:
            self._run_inner(t)
        except Exception as e:  # noqa: BLE001 — a scan must never stay "running" forever
            log.exception("Wattpad scan crashed: %s", e)
            with self._lock:
                self._finish("error", f"Lỗi không mong muốn: {e}")

    def _korean(self, t: dict, numbers: set[int]) -> dict[int, str]:
        """Korean text per chapter number. A raw file may list a heading twice (title line, then the chapter): the
        longest section with Hangul in it wins; English / empty sections are ignored."""
        blocks, _ = load_source(t["korean_path"])
        chapters, _ = split_source(blocks)
        out: dict[int, str] = {}
        for ch in chapters:
            if ch.number not in numbers or ch.chars < 50:
                continue
            text = chr(10).join(chapter_paragraphs(blocks, ch))
            if _HANGUL.search(text) and len(text) > len(out.get(ch.number, "")):
                out[ch.number] = text
        return out

    def _run_inner(self, t: dict) -> None:
        pid, client, stop = t["pid"], t["client"], t["stop"]
        parts = dict(t["parts"])
        with self._lock:
            t["message"] = "Đang đọc file Hàn gốc…"
            self._save(t)
        ko = self._korean(t, set(parts))
        chapters = [{"number": n, "ko": ko[n], "vi": parts[n]["text"]} for n in sorted(parts) if ko.get(n)]
        skipped = len(parts) - len(chapters)
        if not chapters:
            with self._lock:
                self._finish("error", "Không ghép được chương nào với bản Hàn — kiểm tra file Hàn gốc có đúng các số chương này không.")
            return
        batches = _batches(chapters)
        with self._lock:
            t["batches_total"] = len(batches)
            t["chapters"] = len(chapters)
            t["skipped_no_korean"] = skipped
            self._save(t)
        glossary = {g["source"]: g["target"] for g in store.get_glossary(pid)}
        found: dict[str, dict] = {}
        for k, batch in enumerate(batches):
            if stop.is_set():
                break
            korean = "\n".join(c["ko"] for c in batch)
            lines = [f"{e['source']} → {e['target']}" for e in entries_for_text(
                [{"source": s, "target": g} for s, g in glossary.items() if _HANGUL.search(s)], korean)]
            user = ("<glossary>\n" + "\n".join(lines[:2500]) + "\n</glossary>\n\n<chapters>\n"
                    + json.dumps(batch, ensure_ascii=False) + "\n</chapters>")
            data, usage, err = None, {}, None
            for attempt in range(3):
                try:
                    data, usage, _ = client.call(system=WATT_PROMPT, user=user, schema=SCAN_SCHEMA, max_tokens=32000,
                                                 refusal_message="AI từ chối đọc nhóm chương này")
                    break
                except (LLMRateLimit, LLMTimeout) as e:
                    err = e.message
                    if stop.wait(20 * (attempt + 1)):
                        break
                except (LLMAuthError, LLMNotConfigured) as e:
                    with self._lock:
                        self._finish("error", e.message)
                    return
                except LLMError as e:
                    if e.code == "AI_USAGE_LIMIT":
                        with self._lock:
                            self._save_proposals(pid, found, glossary)
                            self._finish("stopped", f"{e.message.split('|')[0]} — quét lại sau khi hạn mức được làm mới.")
                        return
                    err, usage = e.message, (e.details or {}).get("usage", {})
                    break
            nums = [c["number"] for c in batch]
            with self._lock:
                for key, v in usage.items():
                    t["usage"][key] = t["usage"].get(key, 0) + v
                t["cost_usd"] = round(t["cost_usd"] + cost_usd(t["model"], usage), 4)
                t["batches_done"] = k + 1
                if data is None:
                    t["errors"].append(f"Chương {nums[0]}–{nums[-1]}: {err}")
                else:
                    for term in data.get("terms", []):
                        src, tgt = term.get("source", "").strip(), term.get("target", "").strip()
                        if not src or not tgt or not _HANGUL.search(src):
                            continue
                        cur = glossary.get(src)
                        if cur and _norm(cur) == _norm(tgt):
                            continue
                        f = found.setdefault(src, {"votes": Counter(), "variants": set(), "categories": Counter(), "chapters": []})
                        f["votes"][tgt] += len(batch)
                        f["variants"].update(v.strip() for v in term.get("variants", []) if v.strip())
                        f["categories"][term.get("category") or "other"] += 1
                        f["chapters"].extend(nums)
                t["message"] = f"Đã so {k + 1}/{len(batches)} nhóm chương — {len(found)} chỗ khác glossary / thuật ngữ mới."
                self._save(t)
                self._save_proposals(pid, found, glossary)
        with self._lock:
            n = len([1 for s in found if found[s]["votes"]])
            self._finish("cancelled" if stop.is_set() else "done",
                         f"{'Đã dừng' if stop.is_set() else 'Xong'} — {n} đề xuất từ chương cũ, hãy duyệt trong Kho thuật ngữ."
                         + (f" ({skipped} chương không có bản Hàn nên bị bỏ qua.)" if skipped else ""))

    def _save_proposals(self, pid: str, found: dict[str, dict], glossary: dict[str, str]) -> None:
        existing = {p["source"]: p for p in scanner.proposals(pid)}
        for src, f in found.items():
            if not f["votes"]:
                continue
            target, _ = f["votes"].most_common(1)[0]
            cur = glossary.get(src)
            if cur and _norm(cur) == _norm(target):
                continue
            prev = existing.get(src)
            if prev and prev.get("status") in ("accepted", "rejected"):
                continue
            variants = sorted(({cur} if cur else set()) | set(f["votes"]) | f["variants"] - {target})
            existing[src] = {"source": src, "target": target, "variants": [v for v in variants if v and v != target],
                             "category": f["categories"].most_common(1)[0][0], "chapters": sorted(set(f["chapters"]))[:50],
                             "status": "pending", "origin": "wattpad-old", "current": cur}
        _write_json(scanner._proposals_path(pid), list(existing.values()))


wattpad_scan = WattpadTermScan()
