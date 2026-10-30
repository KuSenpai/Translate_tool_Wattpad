"""Translate from an English translation when the Korean raw is missing, keeping the story glossary.

- build:  align chapters that exist both in Korean (the tool's projects) and in the English epub, and ask the AI how the
          English text names each glossary term → data/stories/<story>/glossary_en.json (the glossary itself is untouched).
          The re-translate screen then finds the terms of an English chapter through these names (ai/retranslate.py).
- import: add the English chapters as hand-added chapters of a project (English text in the source pane).

    python -m app.tools.en_bridge build  PID FROM TO EPUB [MODEL]
    python -m app.tools.en_bridge import PID FROM TO EPUB
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import zipfile
from collections import Counter

from ..ai.llm_service import LLMError
from ..ai.retranslate import _HANGUL, _name_pattern, en_bridge_path, previous_chapters
from ..ai.termbase import _make_client
from ..ai.translation_jobs import reset_delay
from ..editor.chapter_editor import service as chapter_service
from ..editor.glossary import entries_for_text
from ..logging_setup import get_logger
from ..storage.project_state import _read_json, _write_json, store

log = get_logger("EN-BRIDGE")
BATCH_CHARS = 40000

PROMPT = """You align a Korean web-novel chapter with its English translation. For each Korean term in <terms>, return the exact
string the English text uses for it (copy it verbatim from <english>, same spelling and capitalisation). If the English uses
several forms, return the most frequent full form. Leave out terms the English text does not render. Output JSON only."""
SCHEMA = {"type": "object", "properties": {"terms": {"type": "array", "items": {
    "type": "object", "properties": {"source": {"type": "string"}, "en": {"type": "string"}},
    "required": ["source", "en"], "additionalProperties": False}}}, "required": ["terms"], "additionalProperties": False}


def read_epub(path: str) -> dict[int, list[str]]:
    """{chapter number: paragraphs} from an epub whose chapter files start with a heading line “3121. Title”."""
    out = {}
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            if not name.lower().endswith((".html", ".xhtml", ".htm")):
                continue
            text = html.unescape(re.sub(r"<[^>]+>", "\n", z.read(name).decode("utf-8", "replace")))
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            if lines and (m := re.match(r"(\d{1,5})\.\s", lines[0])) and len(lines) > 3:
                while len(lines) > 1 and lines[1] == lines[0]:       # <title> + <h1>: the heading appears twice
                    del lines[1]
                out.setdefault(int(m.group(1)), lines)
    return out


def _korean(story: str, number: int) -> str:
    for p in store.story_projects(story):
        lo, hi = p.get("chapter_range") or (0, -1)
        if lo <= number <= hi:
            got = previous_chapters(p["id"], number + 1, count=1)
            if got and got[0]["number"] == number:
                return got[0]["ko"]
    return ""


def build(pid: str, start: int, end: int, epub: str, model: str = "claude-code:opus") -> dict:
    story = store.story_of(pid)
    english = read_epub(epub)
    korean_terms = [g for g in store.get_glossary(pid) if _HANGUL.search(g["source"])]
    known = {g["source"] for g in korean_terms}
    path = en_bridge_path(pid)
    bridge = _read_json(path, {}) or {}
    bridge.setdefault("aliases", {})
    scanned = set(bridge.setdefault("scanned", []))
    chapters = [(n, ko, "\n".join(english[n][1:])) for n in range(start, end + 1)
                if n not in scanned and n in english and (ko := _korean(story, n))]
    batches, cur = [], []
    for c in chapters:
        if cur and sum(len(x[1]) + len(x[2]) for x in cur) + len(c[1]) + len(c[2]) > BATCH_CHARS:
            batches.append(cur)
            cur = []
        cur.append(c)
    batches += [cur] if cur else []
    client = _make_client(model)
    print(f"{len(chapters)} chapters to align in {len(batches)} requests", flush=True)
    for k, batch in enumerate(batches):
        ko_text, en_text = "\n".join(c[1] for c in batch), "\n".join(c[2] for c in batch)
        terms = entries_for_text(korean_terms, ko_text)[:600]
        user = ("<terms>\n" + "\n".join(t["source"] for t in terms) + "\n</terms>\n\n<korean>\n" + ko_text
                + "\n</korean>\n\n<english>\n" + en_text + "\n</english>")
        while True:
            try:
                data, _, _ = client.call(system=PROMPT, user=user, schema=SCHEMA, max_tokens=16000)
                break
            except LLMError as e:
                if e.code != "AI_USAGE_LIMIT":
                    print(f"  batch {k + 1} failed: {e.message[:150]}", flush=True)
                    data = None
                    break
                wait = reset_delay(e.message)
                print(f"  usage limit — waiting {wait / 60:.0f} min", flush=True)
                time.sleep(wait)
        if data is None:
            continue
        for t in data.get("terms", []):
            src, en = t.get("source", "").strip(), t.get("en", "").strip()
            if src in known and en and not _HANGUL.search(en) and _name_pattern(en).search(en_text):   # verbatim in the English
                a = bridge["aliases"].setdefault(src, {"counts": {}})
                a["counts"][en] = a["counts"].get(en, 0) + len(batch)
                a["en"], a["votes"] = Counter(a["counts"]).most_common(1)[0]
        scanned.update(c[0] for c in batch)
        bridge["scanned"] = sorted(scanned)
        _write_json(path, bridge)
        print(f"  {k + 1}/{len(batches)} ch {batch[0][0]}–{batch[-1][0]}: {len(bridge['aliases'])} English names", flush=True)
    log.info("English bridge for %s: %d aliases", pid, len(bridge["aliases"]))
    return bridge


def import_chapters(pid: str, start: int, end: int, epub: str) -> list[int]:
    english = read_epub(epub)
    added = []
    for n in range(start, end + 1):
        if n not in english or store.get_chapter(pid, n):
            continue
        heading, *paragraphs = english[n]
        chapter_service.add_missing(pid, n, "\n".join(paragraphs), "", "")
        rec = store.get_chapter(pid, n)
        rec.update(ko_heading=heading, ko_title=re.sub(r"^\d+\.\s*", "", heading), source_lang="en")
        store.save_chapter(pid, n, rec)
        added.append(n)
    log.info("Imported %d English chapters into %s", len(added), pid)
    return added


if __name__ == "__main__":
    cmd, pid, a, b, epub, *rest = sys.argv[1:]
    if cmd == "build":
        r = build(pid, int(a), int(b), epub, *rest)
        print(json.dumps({k: v for k, v in r.items() if k == "aliases"}, ensure_ascii=False)[:300])
    else:
        print("added", import_chapters(pid, int(a), int(b), epub))
