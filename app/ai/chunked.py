"""Translate one long chapter as several parts in parallel.

A reasoning model needs minutes for a whole chapter, and the time is almost linear in the output, so N parts at once
take roughly 1/N of the time (plus a fixed start-up cost). Each part gets the same glossary / notes / style; the parts
after the first also see the Korean paragraphs right before them, so names and who-is-speaking stay clear.

A provider may refuse a part (xAI answers "403 I can't help with that request", and not always for the same text).
A refused piece is cut in half and both halves are retried at the same time, recursively, so everything that can be translated is, and only the
paragraphs that really are refused stay in Korean, marked with REFUSED_MARK.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from ..logging_setup import get_logger
from .novel_translator import ChapterResult, LLMRefused

log = get_logger("CHUNKED")
MIN_PARAGRAPHS = 30       # shorter chapters are not worth splitting
STAGGER = 1.5             # seconds between starting the parts: providers cap requests per second (xAI: 2/s per team)
CONTEXT = 4               # Korean paragraphs of context before each later part
REFUSED_MARK = "[CHƯA DỊCH — bị từ chối] "


def split_parts(paragraphs: list[str], parts: int) -> list[tuple[int, int]]:
    """Contiguous [start, end) ranges with roughly equal character counts."""
    parts = max(1, min(parts, len(paragraphs) // 12 or 1))
    total = sum(len(p) for p in paragraphs) or 1
    bounds, acc, start = [], 0, 0
    for i, p in enumerate(paragraphs):
        acc += len(p)
        if len(bounds) < parts - 1 and acc >= total * (len(bounds) + 1) / parts:
            bounds.append((start, i + 1))
            start = i + 1
    bounds.append((start, len(paragraphs)))
    return [b for b in bounds if b[1] > b[0]]


def translate_parallel(translator, *, heading: str, paragraphs: list[str], glossary_text: str, prev_tail: list[str],
                       style: str, notes: str = "", parts: int = 1, reference: list[str] | None = None) -> ChapterResult:
    """reference: an existing Vietnamese translation to revise instead of translating from scratch. Sliced per part when
    it has one paragraph per Korean paragraph; otherwise the chapter is sent whole (no alignment to cut it by)."""
    if reference and len(reference) != len(paragraphs):
        parts = 1
    ranges = split_parts(paragraphs, parts) if parts > 1 and len(paragraphs) >= MIN_PARAGRAPHS else [(0, len(paragraphs))]
    if len(ranges) > 1:
        log.info("Translating %d paragraphs as %d parallel parts: %s", len(paragraphs), len(ranges), ranges)

    def piece(a: int, b: int) -> ChapterResult:
        n = notes
        if a > 0:
            ctx = "\n".join(paragraphs[max(0, a - CONTEXT):a])
            n = (notes.strip() + "\n\n" if notes.strip() else "") + (
                f"[MỘT PHẦN CỦA CHƯƠNG] Chỉ dịch các đoạn trong <chapter>. Các đoạn Hàn ngay trước phần này, "
                f"CHỈ để hiểu ngữ cảnh (ai đang nói, tên gọi), KHÔNG dịch và KHÔNG đưa vào kết quả:\n{ctx}")
        if reference:
            ref = reference[a:b] if len(reference) == len(paragraphs) else reference
            n = (n.strip() + "\n\n" if n.strip() else "") + "[BẢN DỊCH HIỆN CÓ — cần sửa, không dịch lại từ đầu]\n" + "\n".join(ref)
        return translator.translate(heading=heading, paragraphs=paragraphs[a:b], glossary_text=glossary_text,
                                    prev_tail=prev_tail if a == 0 else [], style=style, notes=n)

    def resilient(a: int, b: int) -> list[ChapterResult]:
        try:
            return [piece(a, b)]
        except LLMRefused:
            if b - a <= 1:
                log.warning("Paragraph %d refused by the provider — kept in Korean", a + 1)
                return [ChapterResult(heading="", paragraphs=[REFUSED_MARK + paragraphs[a]])]
            mid = (a + b) // 2
            log.warning("Paragraphs %d-%d refused — retrying as %d-%d and %d-%d", a + 1, b, a + 1, mid, mid + 1, b)
            with ThreadPoolExecutor(max_workers=2) as pool:   # both halves at once: each refusal already cost minutes
                left = pool.submit(resilient, a, mid)
                time.sleep(STAGGER)
                right = pool.submit(resilient, mid, b)
                return left.result() + right.result()

    def work(k: int) -> list[ChapterResult]:
        time.sleep(STAGGER * k)
        return resilient(*ranges[k])

    with ThreadPoolExecutor(max_workers=len(ranges)) as pool:
        results = [r for rs in pool.map(work, range(len(ranges))) for r in rs]

    usage: dict = {}
    terms: dict[str, dict] = {}
    out: list[str] = []
    for r in results:
        out += r.paragraphs
        for k, v in r.usage.items():
            usage[k] = usage.get(k, 0) + v
        for t in r.new_terms:
            terms.setdefault(t["source"], t)
    first = next((r for r in results if r.heading), results[0])
    return ChapterResult(heading=first.heading, paragraphs=out, new_terms=list(terms.values()), usage=usage, model=first.model)
