"""Whole-novel Korean → Vietnamese translation with Claude.

Input: a .txt or .docx with the Korean novel (chapters marked by headings such as "1920. 다크 문",
"제 12 화", "EP.2221 2221. …"). Output keeps the input's layout and adds the Vietnamese translation
right under each Korean chapter — the same bilingual format the editor reads.

Chapters that are already followed by a Vietnamese version (a bilingual file being completed)
are left as they are.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import config
from ..document.chapter_parser import (HeadingMatch, _add_bare_headings, _filter_weak, _leading_lang, _trim,
                                       match_heading)
from ..document.docx_reader import read_docx
from ..document.models import Block, Run
from ..errors import DocxError
from ..logging_setup import get_logger
from .llm_service import LLMAuthError, LLMEmptyResponse, LLMError, LLMNotConfigured, LLMRateLimit, LLMTimeout

log = get_logger("TRANSLATE")


class LLMRefused(LLMError):
    code = "AI_REFUSED"


# --------------------------------------------------------------------------- reading the source
def read_txt(path: Path) -> list[Block]:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "cp949", "euc-kr"):
        try:
            text = raw.decode(enc)
            if enc == "utf-16" and not raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                continue
            break
        except UnicodeDecodeError:
            continue
    else:
        raise DocxError("Không đọc được file .txt (bảng mã không nhận ra). Hãy lưu file dạng UTF-8.", code="TXT_ENCODING")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return [Block.model_construct(type="paragraph", level=None, align=None,
                                  runs=[Run.model_construct(text=l.rstrip(), b=False, i=False, u=False)] if l.strip() else [])
            for l in lines]


def load_source(path: str | Path) -> tuple[list[Block], str]:
    path = Path(path)
    if not path.exists():
        raise DocxError(f"File không tồn tại: {path.name}", code="DOCX_NOT_FOUND")
    kind = path.suffix.lower()
    if kind == ".docx":
        return read_docx(path), "docx"
    if kind == ".txt":
        return read_txt(path), "txt"
    raise DocxError("Chỉ hỗ trợ file .txt hoặc .docx.", code="DOCX_BAD_TYPE")


# --------------------------------------------------------------------------- splitting into chapters
@dataclass
class SourceChapter:
    idx: int                       # position in the document (unique, also for duplicate numbers)
    number: int
    heading_index: int
    start: int
    end: int
    heading: str
    lang: Optional[str]
    has_translation: bool = False  # a Vietnamese section with the same number follows directly
    chars: int = 0

    def to_dict(self) -> dict:
        return {"idx": self.idx, "number": self.number, "heading": self.heading, "chars": self.chars,
                "has_translation": self.has_translation, "heading_index": self.heading_index,
                "start": self.start, "end": self.end, "lang": self.lang}


def split_source(blocks: list[Block]) -> tuple[list[SourceChapter], list[str]]:
    candidates = [(i, m) for i, b in enumerate(blocks) if (m := match_heading(b))]
    headings, rejected = _filter_weak(candidates)
    if not headings and candidates:  # a single chapter "1920. …" has no neighbour to confirm it
        headings, rejected = candidates, []
    headings, _ = _add_bare_headings(blocks, headings)
    warnings: list[str] = []
    if not headings:
        return [], ["Không tìm thấy heading chương nào (ví dụ '1920. 다크 문', '제 12 화', '12화', 'Chapter 12')."]
    sections: list[SourceChapter] = []
    for k, (idx, m) in enumerate(headings):
        nxt = headings[k + 1][0] if k + 1 < len(headings) else len(blocks)
        start, end = _trim(blocks, idx + 1, nxt)
        lang = _leading_lang(blocks, start, end) or m.lang_hint
        chars = sum(len(blocks[j].text) for j in range(start, end))
        sections.append(SourceChapter(len(sections), m.number, idx, start, end, blocks[idx].text.strip(), lang, chars=chars))
    for a, b in zip(sections, sections[1:]):
        if a.lang == "ko" and b.lang == "vi" and a.number == b.number:
            a.has_translation = True
    seen: dict[int, int] = {}
    for s in sections:
        if s.lang == "ko":
            seen[s.number] = seen.get(s.number, 0) + 1
    dups = sorted(n for n, c in seen.items() if c > 1)
    if dups:
        warnings.append(f"Số chương bị trùng (sẽ dịch cả hai lần xuất hiện): {', '.join(map(str, dups[:20]))}")
    if rejected:
        warnings.append(f"{len(rejected)} dòng giống heading nhưng sai thứ tự số chương — được coi là nội dung.")
    empty = [s.number for s in sections if s.lang == "ko" and s.start >= s.end]
    if empty:
        warnings.append(f"Chương không có nội dung: {', '.join(map(str, empty[:20]))}")
    return sections, warnings


def chapter_paragraphs(blocks: list[Block], ch: SourceChapter) -> list[str]:
    """Non-empty paragraphs sent for translation (empty spacer lines are not)."""
    return join_wrapped([blocks[j].text.strip() for j in range(ch.start, ch.end) if blocks[j].text.strip()])


WRAP_MIN = 60            # some source files are hard-wrapped every ~65 characters, even mid-word
_SENTENCE_END = re.compile(r"[.!?…\"”’'』」\]】)>~:;*•▶=\-]$")
_OPENS_LINE = re.compile(r"^[\"“‘'『「\[【(<※▶•*=\-]")


def join_wrapped(paragraphs: list[str]) -> list[str]:
    """Glue back paragraphs that a hard wrap cut mid-sentence: a long line with no sentence-ending punctuation, followed
    by a line that does not open something new (quote, bracket, bullet). Otherwise the model translates each piece as its
    own paragraph and the Vietnamese breaks mid-sentence."""
    out: list[str] = []
    for p in paragraphs:
        if out and len(out[-1]) >= WRAP_MIN and not _SENTENCE_END.search(out[-1]) and not _OPENS_LINE.search(p):
            out[-1] += " " + p      # ponytail: the wrap dropped the space or cut a word — can't tell which, the model reads both
        else:
            out.append(p)
    return out


# --------------------------------------------------------------------------- models & prices
# USD per 1M tokens: input, output, cache read
MODELS = {
    "claude-opus-5-5": {"label": "Claude Opus 5.5 — chất lượng cao nhất", "price": (4.0, 20.0, 0.20), "effort": True, "fallbacks": True},
    "claude-sonnet-5-5": {"label": "Claude Sonnet 5.5 — rẻ hơn ~2 lần", "price": (2.0, 10.0, 0.20), "effort": True, "fallbacks": True},
    "claude-haiku-4-5": {"label": "Claude Haiku 4.5 — rẻ nhất, chất lượng thấp hơn", "price": (1.0, 5.0, 0.10), "effort": False, "fallbacks": False},
    # Through the local Claude Code CLI: counted against the user's Claude subscription, not billed per token.
    "claude-code:opus": {"label": "Claude Code · Opus 5.5 — dùng gói Claude của bạn (không tính tiền API)", "price": (0.0, 0.0, 0.0),
                         "effort": True, "fallbacks": False, "subscription": True, "provider": "claude_code", "alias": "claude-opus-5-5"},
    "claude-code:sonnet": {"label": "Claude Code · Sonnet — dùng gói Claude, tốn ít lượt hơn Opus", "price": (0.0, 0.0, 0.0),
                           "effort": True, "fallbacks": False, "subscription": True, "provider": "claude_code", "alias": "sonnet"},
    # Through the Antigravity CLI (`agy`): Gemini, counted against the quota of the Google account signed in to Antigravity.
    "antigravity:pro": {"label": "Antigravity · Gemini Pro — dùng hạn mức tài khoản Google (không tính tiền API)",
                        "price": (0.0, 0.0, 0.0), "effort": True, "fallbacks": False, "subscription": True,
                        "provider": "antigravity", "tier": "pro"},
    "antigravity:flash": {"label": "Antigravity · Gemini Flash — nhanh, tốn ít hạn mức hơn Pro",
                          "price": (0.0, 0.0, 0.0), "effort": True, "fallbacks": False, "subscription": True,
                          "provider": "antigravity", "tier": "flash"},
    # Grok (xAI API, per-token billing). Slugs come from XAI_MODEL / XAI_MODEL_FAST (app/ai/grok.py). Grok accepts 18+ fiction.
    "grok:best": {"label": "Grok (xAI) · mạnh nhất — dịch được truyện 18+, tính tiền API xAI", "price": (2.0, 6.0, 0.50),
                  "effort": False, "fallbacks": False, "provider": "xai", "tier": "best"},
    "grok:fast": {"label": "Grok (xAI) · rẻ — dịch được truyện 18+, rẻ hơn ~2 lần", "price": (1.25, 2.5, 0.20),
                  "effort": False, "fallbacks": False, "provider": "xai", "tier": "fast"},
    # Through the local Grok Build CLI (`grok`): the grok.com account signed in there, no API key.
    "grok-cli:default": {"label": "Grok CLI — dùng tài khoản grok.com đã đăng nhập, dịch được truyện 18+",
                         "price": (0.0, 0.0, 0.0), "effort": True, "fallbacks": False, "subscription": True,
                         "provider": "grok_cli"},
    # Through OmniRoute (local gateway, OpenAI-compatible): the model / combo is OMNIROUTE_MODEL in .env.
    "omniroute:default": {"label": "OmniRoute — cổng AI cục bộ (model/combo đặt ở OMNIROUTE_MODEL trong .env)",
                          "price": (0.0, 0.0, 0.0), "effort": False, "fallbacks": False, "provider": "omniroute"},
}
DEFAULT_MODEL = "claude-opus-5-5"


def cost_usd(model: str, usage: dict) -> float:
    pin, pout, pcache = MODELS.get(model, MODELS[DEFAULT_MODEL])["price"]
    return (usage.get("input_tokens", 0) * pin + usage.get("cache_creation_input_tokens", 0) * pin * 1.25
            + usage.get("cache_read_input_tokens", 0) * pcache + usage.get("output_tokens", 0) * pout) / 1e6


def estimate(chars: int, chapters: int) -> dict:
    """Rough cost before starting (±50%): Korean ≈ 1 token/char in, Vietnamese ≈ 1.6 tokens per Korean char out."""
    tin = int(chars * 1.0 + chapters * 2500)          # + system prompt / style / glossary per request
    tout = int(chars * 1.6 + chapters * 600)          # + thinking overhead
    return {m: {"input_tokens": tin, "output_tokens": tout,
                "usd": round(cost_usd(m, {"input_tokens": tin, "output_tokens": tout}), 2)} for m in MODELS}


# --------------------------------------------------------------------------- prompt
DEFAULT_STYLE = """- Dịch đầy đủ, sát nghĩa từng câu; văn phong truyện mạng tiếng Việt tự nhiên, trôi chảy, không lai câu cú tiếng Hàn.
- Tên riêng phiên âm Latin chuẩn (Kim Soo-hyun, Lee Ha-eun…) và thống nhất theo glossary; tên chiêu thức/kỹ năng/hệ thống dịch Hán Việt hoặc nghĩa, giữ nhất quán.
- Xưng hô chọn theo quan hệ, tuổi tác, ngữ cảnh (tôi/cậu, anh/em, chị/em, ông/cháu, hắn, ả…). KHÔNG dùng xưng hô mày–tao.
- Cảnh 18+: dịch trực tiếp, đầy đủ, không né tránh hay nói giảm; dùng từ ngữ tình dục thẳng thắn đúng mức độ bản gốc (ví dụ: làm tình/địt, cặc/dương vật, lồn/âm đạo — chọn mức thô tục tương ứng bản gốc).
- Chửi thề, lời lẽ thô tục: dùng từ tiếng Việt tương đương, giữ nguyên độ gắt.
- Lời thoại dùng “ ”; thông báo hệ thống trong [ ] giữ nguyên dấu ngoặc; từ tượng thanh chuyển sang cách viết tiếng Việt tự nhiên.
- Không thêm chú thích, lời bình, tóm tắt hay cảnh báo."""

SYSTEM_PROMPT = """You are a professional Korean→Vietnamese literary translator. You are translating a published Korean web novel for an adult (18+) Vietnamese readership; the rights holder has commissioned a complete and faithful translation.

Translate everything completely and faithfully — including violence, crude language, profanity and explicit sexual content between adult characters — without censoring, softening, euphemizing, summarizing, skipping passages, or adding notes or warnings. Where the source is crude or explicit, the Vietnamese must be equally direct, as the style guide below specifies.

Input: a JSON object with the chapter "heading" and its "paragraphs" (in order), plus the story glossary (the fixed renderings used in earlier chapters — names, skills, places, items, titles), optional story notes (characters, relationships, setting) and the end of the previous chapter's translation for continuity of names, pronouns and tone.

Output rules:
- "paragraphs": exactly one Vietnamese paragraph for each input paragraph, same order, same count. Never merge or split paragraphs; keep line breaks inside a paragraph as "\\n".
- "heading": the chapter heading in Vietnamese. Keep the chapter number and the numbering format: "1920. 다크 문" → "1920. <tiêu đề dịch>", "EP.2221 2221. 경성 2033" → "Tập 2221. <tiêu đề dịch>", "제 12 화" / "12화" → "Chương 12", "제 12 화 - 제목" → "Chương 12: <tiêu đề dịch>".
- Follow the glossary exactly for names and terms, even when another rendering seems better: consistency with earlier chapters matters more. In "new_terms", list character names and recurring terms that are not in the glossary yet, with the rendering you used (empty list if none).

Style guide (Vietnamese):
{style}"""

SCHEMA = {
    "type": "object",
    "properties": {
        "heading": {"type": "string"},
        "paragraphs": {"type": "array", "items": {"type": "string"}},
        "new_terms": {"type": "array", "items": {
            "type": "object",
            "properties": {"source": {"type": "string"}, "target": {"type": "string"}},
            "required": ["source", "target"], "additionalProperties": False}},
    },
    "required": ["heading", "paragraphs", "new_terms"],
    "additionalProperties": False,
}


def build_user_message(heading: str, paragraphs: list[str], glossary_text: str, prev_tail: list[str],
                       notes: str = "") -> str:
    parts = []
    if notes.strip():
        parts.append(f"<story_notes>\n{notes.strip()}\n</story_notes>")
    parts.append(f"<glossary>\n{glossary_text or '(trống)'}\n</glossary>")
    if prev_tail:
        parts.append("<previous_chapter_ending_vi>\n" + "\n".join(prev_tail) + "\n</previous_chapter_ending_vi>")
    parts.append("<chapter>\n" + json.dumps({"heading": heading, "paragraphs": paragraphs}, ensure_ascii=False) + "\n</chapter>")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- translators
@dataclass
class ChapterResult:
    heading: str
    paragraphs: list[str]
    new_terms: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    model: str = ""


class ClaudeJSON:
    """One structured-output Claude call (streamed), with errors mapped to the app's LLM errors.
    Shared by chapter translation and the termbase scanner."""

    def __init__(self, model: str, effort: str = "medium"):
        key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not key:
            raise LLMNotConfigured("Thiếu ANTHROPIC_API_KEY trong file .env — cần API key Claude.")
        import anthropic
        self._sdk = anthropic
        self.model = model if model in MODELS else DEFAULT_MODEL
        self.effort = effort
        self.client = anthropic.Anthropic(api_key=key, timeout=900, max_retries=3)

    def call(self, *, system: str, user: str, schema: dict, max_tokens: int = 64000,
             refusal_message: str = "Claude từ chối xử lý nội dung này") -> tuple[dict, dict, str]:
        a = self._sdk
        info = MODELS[self.model]
        output_config: dict = {"format": {"type": "json_schema", "schema": schema}}
        if info["effort"] and self.effort:
            output_config["effort"] = self.effort
        extra: dict = {}
        if info["fallbacks"]:  # a policy decline is retried server-side on a suitable model
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        try:
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
                output_config=output_config,
                **extra,
            ) as stream:
                msg = stream.get_final_message()
        except a.APITimeoutError:
            raise LLMTimeout("Claude không phản hồi kịp (timeout).")
        except (a.AuthenticationError, a.PermissionDeniedError):
            raise LLMAuthError("API key Claude không hợp lệ hoặc không có quyền (ANTHROPIC_API_KEY trong .env).")
        except a.RateLimitError:
            raise LLMRateLimit("Bị giới hạn tốc độ (rate limit) — sẽ thử lại sau.")
        except a.NotFoundError:
            raise LLMError(f"Model '{self.model}' không khả dụng với API key này.", code="AI_BAD_MODEL")
        except a.APIStatusError as e:
            raise LLMError(f"Claude API lỗi {e.status_code}: {e.message}", code="AI_SERVER" if e.status_code >= 500 else "AI_ERROR")
        except a.APIConnectionError:
            raise LLMError("Không kết nối được tới Claude API (mạng?).", code="AI_NETWORK")

        u = msg.usage
        usage = {"input_tokens": u.input_tokens or 0, "output_tokens": u.output_tokens or 0,
                 "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
                 "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0}
        if msg.stop_reason == "refusal":
            cat = getattr(getattr(msg, "stop_details", None), "category", None)
            raise LLMRefused(f"{refusal_message}{f' ({cat})' if cat else ''}.", details={"usage": usage})
        if msg.stop_reason == "max_tokens":
            raise LLMEmptyResponse("Kết quả bị cắt (quá dài cho một lần gọi).", details={"usage": usage})
        text = "".join(b.text for b in msg.content if b.type == "text")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise LLMEmptyResponse("Claude trả về dữ liệu không đúng định dạng.", details={"usage": usage})
        return data, usage, getattr(msg, "model", self.model) or self.model


class AnthropicChapterTranslator(ClaudeJSON):
    def translate(self, *, heading: str, paragraphs: list[str], glossary_text: str, prev_tail: list[str],
                  style: str, notes: str = "") -> ChapterResult:
        data, usage, model = self.call(system=SYSTEM_PROMPT.format(style=style),
                                       user=build_user_message(heading, paragraphs, glossary_text, prev_tail, notes),
                                       schema=SCHEMA, refusal_message="Claude từ chối dịch chương này")
        if not data.get("paragraphs"):
            raise LLMEmptyResponse("Claude trả về bản dịch rỗng.", details={"usage": usage})
        return ChapterResult(heading=str(data.get("heading") or "").strip(), paragraphs=[str(p) for p in data["paragraphs"]],
                             new_terms=[t for t in data.get("new_terms", []) if t.get("source") and t.get("target")],
                             usage=usage, model=model)


class MockChapterTranslator:
    """Offline translator for tests / trying the UI (TRANSLATE_PROVIDER=mock): no API calls."""

    def __init__(self, model: str = "mock", effort: str = ""):
        self.model = model

    def translate(self, *, heading: str, paragraphs: list[str], glossary_text: str, prev_tail: list[str],
                  style: str, notes: str = "") -> ChapterResult:
        m = match_heading(Block(runs=[Run(text=heading)]))
        vi_heading = f"{m.number}. Tiêu đề dịch thử" if m and m.title else (f"Chương {m.number}" if m else "Chương ?")
        if "REFUSE" in " ".join(paragraphs):
            raise LLMRefused("Claude từ chối dịch chương này (mock).")
        return ChapterResult(heading=vi_heading,
                             paragraphs=[f"Bản dịch thử đoạn {i + 1} ({len(p)} ký tự)." for i, p in enumerate(paragraphs)],
                             new_terms=[{"source": "김수현", "target": "Kim Soo-hyun"}] if "김수현" in " ".join(paragraphs) else [],
                             usage={"input_tokens": 100, "output_tokens": 200}, model="mock")


def make_translator(model: str, effort: str):
    if os.getenv("TRANSLATE_PROVIDER", "anthropic").strip().lower() == "mock":
        return MockChapterTranslator(model, effort)
    if MODELS.get(model, {}).get("provider") == "antigravity":
        from .antigravity import AntigravityTranslator
        return AntigravityTranslator(model, effort)
    if MODELS.get(model, {}).get("provider") == "xai":
        from .grok import GrokTranslator
        return GrokTranslator(model, effort)
    if MODELS.get(model, {}).get("provider") == "grok_cli":
        from .grok_cli import GrokCLITranslator
        return GrokCLITranslator(model, effort)
    if MODELS.get(model, {}).get("provider") == "omniroute":
        from .omniroute import OmniRouteTranslator
        return OmniRouteTranslator(model, effort)
    if MODELS.get(model, {}).get("subscription"):
        from .claude_code import ClaudeCodeJSON

        class ClaudeCodeTranslator(ClaudeCodeJSON):
            translate = AnthropicChapterTranslator.translate

        return ClaudeCodeTranslator(model, effort)
    return AnthropicChapterTranslator(model, effort)


def normalize_vi_heading(vi_heading: str, ko: SourceChapter) -> str:
    """Make sure the Vietnamese heading is recognised by the parser with the same chapter number."""
    m = match_heading(Block(runs=[Run(text=vi_heading)])) if vi_heading else None
    if m and m.number == ko.number:
        return vi_heading
    title = re.sub(r"^\s*(?:ep\.?\s*\d+\s+)?(?:t[ậa]p|ch(?:ư|u)(?:ơ|o)ng|chapter)?\s*\d+\s*[.:\-–—]?\s*", "", vi_heading or "",
                   flags=re.IGNORECASE).strip()
    return f"{ko.number}. {title}" if title else f"Chương {ko.number}"
