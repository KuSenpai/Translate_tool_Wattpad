"""Lightweight checks shown to the user before preview/publish. Advisory only."""
from __future__ import annotations

import re

from ..document.chapter_parser import _HANGUL
from ..document.models import Block

_SPACE_BEFORE_PUNCT = re.compile(r"\s+[,.;:!?…](?!\.)")
_DOUBLE_SPACE = re.compile(r"\S  +\S")
# Leftovers from copying out of web pages / AI chats.
_JUNK = re.compile(r"ads\s+by\s+\w+|dịch sang tiếng việt|tiếp tục dịch|^(?:grok|chatgpt|gemini|claude|copilot)(?:\s*[\d.]+)?$"
                   r"|^dưới đây là\b.{0,200}\bbản dịch|^[A-Za-z0-9+/]{32,}={0,2}$",
                   re.IGNORECASE)


def _issue(level: str, message: str, block: int | None = None) -> dict:
    return {"level": level, "message": message, "block": block}


def validate_translation(vi_blocks: list[Block], ko_blocks: list[Block], glossary: list[dict]) -> list[dict]:
    issues: list[dict] = []
    if not any(b.text.strip() for b in vi_blocks):
        return [_issue("error", "Bản dịch trống.")]
    ko_text = "\n".join(b.text for b in ko_blocks)
    vi_text = "\n".join(b.text for b in vi_blocks)

    for i, b in enumerate(vi_blocks):
        t = b.text
        if _HANGUL.search(t):
            issues.append(_issue("warn", "Đoạn còn chữ Hàn (chưa dịch?).", i))
        if _JUNK.search(t.strip()):
            issues.append(_issue("warn", f"Có vẻ là rác khi copy (quảng cáo / tên AI / lời nhắc dịch): “{t.strip()[:50]}”.", i))
        if _DOUBLE_SPACE.search(t):
            issues.append(_issue("info", "Có khoảng trắng kép.", i))
        if _SPACE_BEFORE_PUNCT.search(t):
            issues.append(_issue("info", "Có khoảng trắng trước dấu câu.", i))
        if t.count("“") != t.count("”") or t.count('"') % 2:
            issues.append(_issue("info", "Dấu ngoặc kép có thể chưa đóng/mở đúng.", i))

    vi_lower = vi_text.lower()
    for e in glossary:
        src, tgt = e["source"], e["target"]
        for bad in e.get("avoid", []):
            # Whole word, exact case, and not inside the correct rendering itself: “LA” must not hit “làm”/“lại”,
            # “Vô Hạn” must not hit “Vô hạn”, “Lưỡi Gió” must not hit “Lưỡi Gió (Wind Cutter)”.
            pat = re.compile(rf"(?<!\w){re.escape(bad)}(?!\w)")
            for i, b in enumerate(vi_blocks):
                if pat.search(b.text.replace(tgt, "\0")):
                    issues.append(_issue("warn", f"Glossary: dùng “{bad}” — nên là “{tgt}”.", i))
        if _HANGUL.search(src):
            if src in ko_text and tgt.lower() not in vi_lower:
                issues.append(_issue("info", f"Glossary: bản Hàn có “{src}” nhưng bản dịch không có “{tgt}”."))
        elif src.lower() != tgt.lower():
            # A non-Korean source is treated as a wrong rendering to replace.
            for i, b in enumerate(vi_blocks):
                if re.search(rf"(?<!\w){re.escape(src)}(?!\w)", b.text, re.IGNORECASE):
                    issues.append(_issue("warn", f"Glossary: “{src}” → nên dùng “{tgt}”.", i))
    return issues
