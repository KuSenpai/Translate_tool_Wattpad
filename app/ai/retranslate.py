"""Re-translate one chapter from its Korean source with any model of the translation screen (Claude, Claude Code,
Gemini, Grok …). Never touches the draft: the UI shows the result and the user decides to apply it.

Consistency with the chapters already translated:
  • the glossary entries and the pending termbase proposals (renderings found in old chapters) that occur in this
    chapter go into the prompt, together with the ends of the previous chapters;
  • the result is checked against the glossary (forbidden renderings, Hangul left over, terms missing);
    when it breaks a hard rule the chapter is translated once more with the problems spelled out, and the better
    of the two is kept;
  • terms the AI reports as new are compared with the previous chapters: a Korean term that already occurs
    there but whose rendering is absent from their Vietnamese text is flagged.
"""
from __future__ import annotations

import re

from ..document.chapter_parser import extract_chapter
from ..document.models import Block
from ..editor.chapter_editor import default_title, service as chapter_service
from ..editor.glossary import entries_for_text, glossary_prompt
from ..editor.validation import validate_translation
from ..errors import AppError, ChapterError
from ..logging_setup import get_logger
from ..storage.project_state import _read_json, store
from .chunked import REFUSED_MARK, translate_parallel
from .consistency import with_examples
from .llm_service import LLMError
from .novel_translator import MODELS, cost_usd, join_wrapped, make_translator
from .termbase import scanner
from .translation_jobs import get_style

log = get_logger("RETRANSLATE")

_TITLE_PREFIX = re.compile(r"^\s*(?:ep\.?\s*\d+\s+)?(?:t[ậa]p|ch(?:ư|u)(?:ơ|o)ng|chapter)?\s*\d+\s*[.:\-–—]?\s*", re.IGNORECASE)
_HANGUL = re.compile(r"[가-힣]")
PREVIOUS = 5          # earlier chapters used for context, address examples and for checking new terms


def _text(blocks: list[dict]) -> str:
    return "\n".join(t for b in blocks if (t := Block.model_validate(b).text.strip()))


def _paragraphs(blocks: list[dict]) -> list[str]:
    return [t for b in blocks if (t := Block.model_validate(b).text.strip())]


def previous_chapters(pid: str, number: int, count: int = PREVIOUS) -> list[dict]:
    """The `count` chapters before `number` that have a Vietnamese text: [{number, ko, vi}] newest first.
    The user's own draft wins over the translation inside the Word file."""
    out = []
    blocks_result = None
    for n in range(number - 1, number - 1 - count * 3, -1):         # tolerate a few missing numbers
        if len(out) >= count or n < 0:
            break
        rec = store.get_chapter(pid, n)
        ko, vi = (rec.get("ko_blocks"), rec.get("draft")) if rec else (None, None)
        if not (ko and vi and _text(vi)):
            try:
                blocks_result = blocks_result or chapter_service._parsed(pid)
                c = extract_chapter(*blocks_result, n)
                ko, vi = [b.model_dump() for b in c.ko_blocks], [b.model_dump() for b in c.vi_blocks]
            except AppError:
                continue
        if ko and vi and _text(vi):
            out.append({"number": n, "ko": _text(ko), "vi": _text(vi), "ko_paragraphs": _paragraphs(ko), "vi_paragraphs": _paragraphs(vi)})
    return out


def _glossary_context(pid: str, korean: str) -> tuple[str, list[dict], int]:
    """Prompt text for the glossary entries of this chapter + hints from pending termbase proposals."""
    entries = entries_for_text(store.get_glossary(pid), korean)
    text = glossary_prompt(entries)
    known = {e["source"] for e in entries}
    hints = [p for p in scanner.proposals(pid) if p.get("status") == "pending" and p["source"] in korean and p["source"] not in known]
    for p in hints:
        how = "đã dùng ở chương cũ trên Wattpad — ưu tiên" if p.get("origin") == "wattpad-old" else "đã dùng ở các chương trước"
        text += f"\n- {p['source']} → {p['target']} ({how})"
    return text, entries, len(hints)


EN_SOURCE_STYLE = ("\n- NGUỒN CHƯƠNG NÀY LÀ BẢN DỊCH TIẾNG ANH của nguyên tác tiếng Hàn (không có bản Hàn). Dịch sang tiếng Việt như dịch"
                   " từ bản Hàn: tên riêng, chiêu thức, danh hiệu, địa danh theo đúng glossary (cột trái là cách bản tiếng Anh gọi),"
                   " không giữ cách gọi tiếng Anh khi glossary đã có bản Việt; tên người Hàn viết theo glossary / phiên âm chuẩn"
                   " (Lee Yeon-hee…). Tiếng Anh không có kính ngữ: chọn xưng hô theo quan hệ nhân vật, ghi chú truyện và các câu"
                   " mẫu của chương trước, không dịch máy móc I/you thành tôi/bạn. Trong new_terms, source là cách gọi tiếng Anh.")
_LATIN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .'\-]{2,}$")


def en_bridge_path(pid: str):
    """English names of the story's glossary terms: {"aliases": {korean: {"en", "votes"}}, "extra": [{source(en), target}]}."""
    return store._glossary_file(pid).with_name("glossary_en.json")


def _name_pattern(name: str) -> re.Pattern:
    # “Lee Yeon-hee” (glossary) must also match “Lee Yeonhee” / “Lee Yeon hee” (English text)
    return re.compile(r"(?<!\w)" + re.escape(name).replace(r"\-", r"[-\s]?").replace(r"\ ", r"[-\s]?") + r"(?!\w)", re.IGNORECASE)


def en_entries(glossary: list[dict], bridge: dict, english: str) -> list[dict]:
    """Glossary entries that occur in an English text, keyed by their English name: the alias found by aligning earlier
    Korean / English chapters, or the Vietnamese target itself when it is already a Latin name (Kim Soo-hyun)."""
    aliases = bridge.get("aliases", {})
    found: dict[str, tuple[int, dict]] = {}
    for e in glossary:
        if not _HANGUL.search(e["source"]):
            continue
        a = aliases.get(e["source"]) or {}
        counts = a.get("counts") or ({a["en"]: a.get("votes", 1)} if a.get("en") else {})
        best = max(counts.values(), default=0)      # the English is not consistent either (Seong Yujin / Sung Yujin): keep the
        names = [(n, c) for n, c in sorted(counts.items(), key=lambda x: -x[1]) if c >= max(2, best * 0.1)]  # common spellings
        for name, votes in names + [(e["target"] if _LATIN_NAME.match(e["target"]) else None, 0)]:
            if name and len(name) >= 3 and _name_pattern(name).search(english):
                key = name.lower()
                if key not in found or votes > found[key][0]:     # one English name → the best-attested Vietnamese rendering
                    found[key] = (votes, {**e, "source": name, "ko": e["source"]})
                break
    for x in bridge.get("extra", []):              # terms first met in earlier English-source chapters
        if x["source"].lower() not in found and _name_pattern(x["source"]).search(english):
            found[x["source"].lower()] = (0, {"source": x["source"], "target": x["target"]})
    return [v for _, v in found.values()]


def _to_blocks(paragraphs: list[str]) -> list[dict]:
    return [{"type": "paragraph", "level": None, "align": None, "runs": [{"text": p, "b": False, "i": False, "u": False}]}
            for p in paragraphs]


def _check(rec: dict, paragraphs: list[str], glossary: list[dict]) -> list[dict]:
    vi = [Block.model_validate(b) for b in _to_blocks(paragraphs)]
    ko = [Block.model_validate(b) for b in rec.get("ko_blocks", [])]
    return validate_translation(vi, ko, glossary)


def _hard(issues: list[dict]) -> list[dict]:
    return [i for i in issues if i["level"] in ("warn", "error")]


def _term_report(entries: list[dict], korean: str, vi_text: str, english: bool = False) -> dict:
    """How many glossary terms of this chapter appear with the chosen rendering in the new translation."""
    low = vi_text.lower()
    present = entries if english else [e for e in entries if _HANGUL.search(e["source"]) and e["source"] in korean]
    ok = [e for e in present if e["target"].lower() in low]
    missing = [{"source": e["source"], "target": e["target"]} for e in present if e not in ok]
    return {"terms_in_chapter": len(present), "terms_ok": len(ok), "terms_missing": missing[:30]}


def _new_term_conflicts(new_terms: list[dict], known: set[str], previous: list[dict]) -> list[dict]:
    """New terms (not in the glossary) whose Korean form is already in an earlier chapter, but whose rendering is not
    used in that chapter's Vietnamese — the earlier chapters probably translated the same term differently."""
    out = []
    for t in new_terms:
        src, tgt = t.get("source", ""), t.get("target", "")
        if not src or src in known:
            continue
        for p in previous:
            if src in p["ko"]:
                if tgt.lower() not in p["vi"].lower():
                    out.append({"source": src, "target": tgt, "chapter": p["number"]})
                break
    return out


def retranslate_chapter(pid: str, number: int, *, model: str, effort: str = "medium", instruction: str = "",
                        auto_fix: bool = True, parallel: int = 1, current: list[str] | None = None) -> dict:
    """current: the Vietnamese text being edited — when given, the AI revises it (fixes mistakes, keeps what is right)
    instead of translating the chapter again from scratch."""
    rec = store.get_chapter(pid, number)
    if rec is None:
        raise ChapterError(f"Chương {number} chưa được load.", code="CHAPTER_NOT_LOADED")
    if model not in MODELS:
        raise ChapterError(f"Model '{model}' không có trong danh sách.", code="AI_BAD_MODEL")
    paragraphs = join_wrapped(_paragraphs(rec.get("ko_blocks", [])))
    if not paragraphs:
        raise ChapterError("Chương này không có bản Hàn để dịch lại.", code="NO_SOURCE")
    heading = (rec.get("ko_heading") or rec.get("ko_title") or f"{number}").strip()
    korean = "\n".join(paragraphs + [heading])

    glossary_all = store.get_glossary(pid)
    previous = previous_chapters(pid, number)
    english = not _HANGUL.search(korean)        # an English translation stands in for the missing Korean raw
    if english:
        entries = en_entries(glossary_all, _read_json(en_bridge_path(pid), {}) or {}, korean)
        glossary_text, hints = glossary_prompt(entries), 0
        keys = [e["source"] for e in entries] + [e["ko"] for e in entries if e.get("ko")]   # address examples: ko or en lines
        notes, examples = with_examples(store.get_notes(pid), previous, " ".join(keys), keys)
    else:
        glossary_text, entries, hints = _glossary_context(pid, korean)
        notes, examples = with_examples(store.get_notes(pid), previous, korean, [e["source"] for e in glossary_all])
    tail = [p for c in reversed(previous) for p in c["vi_paragraphs"][-4:]][-10:]        # oldest → newest
    style = get_style() + (EN_SOURCE_STYLE if english else "")
    if instruction.strip():
        style += f"\n- Yêu cầu riêng cho lần dịch lại chương này (ưu tiên cao): {instruction.strip()}"
    current = [p.strip() for p in current or [] if p.strip()]
    if current:
        style += ("\n- CHẾ ĐỘ SỬA BẢN DỊCH: trong story_notes có [BẢN DỊCH HIỆN CÓ] của chương. Đối chiếu với bản Hàn và giữ nguyên"
                  " câu chữ đã đúng; chỉ sửa chỗ dịch sai nghĩa, sót ý, sai thuật ngữ glossary, sai xưng hô, sai chính tả, câu lủng củng."
                  " Không viết lại những câu không có lỗi. Vẫn trả đúng một đoạn tiếng Việt cho mỗi đoạn Hàn.")

    log.info("%s chapter %d with %s (%d paragraphs, %d glossary entries, %d previous chapters)",
             "Revising" if current else "Re-translating", number, model, len(paragraphs), len(entries), len(previous))
    translator = make_translator(model, effort)
    translator.count_tolerance = 0.05      # the user reviews the preview: a few merged paragraphs are not worth another 3 minutes

    def attempt(extra: str):
        r = translate_parallel(translator, heading=heading, paragraphs=paragraphs, glossary_text=glossary_text, prev_tail=tail,
                               style=style + extra, notes=notes, parts=parallel, reference=current)
        return r, _check(rec, r.paragraphs, glossary_all)

    result, issues = attempt("")
    usage = dict(result.usage)
    retried = False
    if auto_fix and _hard(issues):   # forbidden renderings / Hangul / junk: one corrective pass with the problems spelled out
        translator.max_attempts = 1          # …without the translator's own paragraph-count retry on top (calls multiply)
        translator.timeout_min = 8           # Antigravity: a stuck fix pass gives up after 8 min, not 20 (first result is kept)
        problems = "; ".join(dict.fromkeys(i["message"] for i in _hard(issues)))[:900]
        retried = True
        try:
            second, issues2 = attempt("\n- LỖI Ở LẦN DỊCH TRƯỚC, bắt buộc sửa lần này: " + problems)
        except LLMError as e:          # the fix pass failing (refused, cut off, timeout) must not throw away the first result
            log.warning("Corrective pass failed (%s) — keeping the first translation", e.message[:120])
        else:
            for k, v in second.usage.items():
                usage[k] = usage.get(k, 0) + v
            if len(_hard(issues2)) <= len(_hard(issues)):
                result, issues = second, issues2

    vi_text = "\n".join(result.paragraphs)
    known = {e["source"] for e in glossary_all}
    report = {**_term_report(entries, korean, vi_text, english), "english_source": english, "hints_used": hints, "examples_used": examples, "previous_chapters": [c["number"] for c in previous],
              "retried": retried, "parallel_parts": parallel, "revised": bool(current),
              "refused_paragraphs": [i for i, p in enumerate(result.paragraphs) if p.startswith(REFUSED_MARK)], "new_term_conflicts": _new_term_conflicts(result.new_terms, known, previous)}
    vi_title = _TITLE_PREFIX.sub("", result.heading or "").strip()
    return {
        "number": number,
        "title": default_title(number, vi_title),
        "blocks": _to_blocks(result.paragraphs),
        "paragraph_count": {"source": len(paragraphs), "result": len(result.paragraphs)},
        "new_terms": result.new_terms,
        "issues": issues,
        "consistency": report,
        "model": result.model or model,
        "usage": usage,
        "cost_usd": round(cost_usd(model, usage), 4),
    }
