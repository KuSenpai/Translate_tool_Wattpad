"""Translate with Gemini through the Antigravity CLI (`agy`) in headless mode, using the quota of the
Google account signed in to Antigravity — no API key, no per-token billing.

Requirements: the Antigravity CLI installed and signed in once:
    irm https://antigravity.google/cli/install.ps1 | iex      (PowerShell)
    agy                                                       (first run opens the browser to sign in)

The translation rules for Gemini live in app/ai/gemini_rules.md (copy it to data/gemini_rules.md to
customise). The CLI runs in an empty folder outside the project whose GEMINI.md forbids tool use, so
the agent never reads or writes the user's files.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from .. import config
from ..logging_setup import get_logger
from .claude_code import LLMUsageLimit
from .llm_service import LLMAuthError, LLMEmptyResponse, LLMError, LLMRateLimit, LLMTimeout

log = get_logger("ANTIGRAVITY")

INSTALL_CMD = "irm https://antigravity.google/cli/install.ps1 | iex"
RULES_TEMPLATE = Path(__file__).with_name("gemini_rules.md")
WORKSPACE_RULE = """# Workspace chỉ dùng để dịch truyện
- Không dùng bất kỳ công cụ nào: không đọc/ghi file, không chạy lệnh terminal, không duyệt web, không tạo artifact hay kế hoạch.
- Mọi dữ liệu cần thiết nằm trong tin nhắn. Trả lời ngay, đúng định dạng JSON được yêu cầu.
"""
_LOGIN = re.compile(r"not (?:logged|signed) in|sign[ -]?in|log[ -]?in required|unauthenticated|authenticat|"
                    r"credential|keyring|oauth|permission_denied", re.IGNORECASE)
_QUOTA = re.compile(r"resource[_ ]exhausted|quota|out of credits|credits? (?:exhausted|used up)|usage limit|"
                    r"limit (?:reached|exceeded)|exceeded your", re.IGNORECASE)
_RATE = re.compile(r"\b429\b|\b503\b|rate[ -]?limit|overloaded|unavailable|try again later", re.IGNORECASE)
_SAFETY = re.compile(r"safety|prohibited[_ ]content|blocked|recitation|blocklist|harm[_ ]category|policy", re.IGNORECASE)
_BAD_MODEL = re.compile(r"unknown model|model .*not (?:found|available|supported)|invalid model", re.IGNORECASE)
_SLUG = re.compile(r"\b((?:gemini|claude|gpt)-[a-z0-9.\-]*\d[a-z0-9.\-]*)", re.IGNORECASE)
_INTERRUPTED = re.compile(r"stream was interrupted|connection (?:was )?(?:reset|closed|aborted|refused)|forcibly closed|unexpected eof|"
                          r"wsarecv|wsasend|broken pipe|i/o timeout|tls handshake timeout|no such host", re.IGNORECASE)
_EFFORT_SUFFIX = re.compile(r"-(high|medium|low)$")
_HANGUL = re.compile(r"[가-힣]")


def find_agy() -> Optional[str]:
    override = os.getenv("ANTIGRAVITY_CLI_PATH", "").strip()
    if override:
        return override
    found = shutil.which("agy")
    candidates = [Path(found)] if found else []
    local = os.getenv("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "agy" / "bin" / "agy.exe")
    candidates.append(Path.home() / ".local" / "bin" / ("agy.exe" if os.name == "nt" else "agy"))
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _workdir() -> Path:
    """Empty folder outside the project (no .git above it): the agent sees no project files, and the
    always-on GEMINI.md here forbids tool use."""
    d = Path(tempfile.gettempdir()) / "translate-tool-antigravity"
    d.mkdir(parents=True, exist_ok=True)
    rule = d / "GEMINI.md"
    if not rule.exists() or rule.read_text(encoding="utf-8") != WORKSPACE_RULE:
        rule.write_text(WORKSPACE_RULE, encoding="utf-8")
    return d


def rules(style: str) -> str:
    """The Gemini translation rules with the user's style guide filled in."""
    custom = config.DATA_DIR / "gemini_rules.md"
    text = (custom if custom.exists() else RULES_TEMPLATE).read_text(encoding="utf-8")
    return text.replace("{style}", style.strip() or "(không có)")


# --------------------------------------------------------------------------- status & models
_cache: tuple[float, dict] = (0.0, {})


def status(probe: bool = False, force: bool = False) -> dict:
    """{"installed", "path", "models", "checked", "error"}. Only `probe=True` runs `agy models` (the CLI may
    open the browser to sign in, so it is not done on every page load). A missing sign-in otherwise shows
    up as an ANTIGRAVITY_LOGIN error on the first chapter."""
    global _cache
    exe = find_agy()
    if _cache[1].get("path") == exe and (not probe or _cache[1].get("checked")) and not force             and time.time() - _cache[0] < 600:
        return _cache[1]
    st = {"installed": bool(exe), "path": exe, "models": [], "checked": False, "error": None, "install_cmd": INSTALL_CMD}
    if exe and not probe:
        return st
    if exe:
        st["checked"] = True
        try:
            out = subprocess.run([exe, "models"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=60, cwd=_workdir())
            text = (out.stdout or "") + "\n" + (out.stderr or "")
            st["models"] = list(dict.fromkeys(m.lower() for m in _SLUG.findall(out.stdout or "")))
            if not st["models"]:
                st["error"] = ("Chưa đăng nhập Antigravity CLI — mở terminal, chạy  agy  một lần để đăng nhập Google."
                               if _LOGIN.search(text) else text.strip()[-200:] or None)
        except (OSError, subprocess.TimeoutExpired) as e:
            st["error"] = str(e)[:200]
    _cache = (time.time(), st)
    return st


def _version(slug: str) -> tuple:
    m = re.search(r"(\d+(?:\.\d+)*)", slug)
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def pick_model(tier: str, available: list[str], effort: str = "high") -> Optional[str]:
    """Best Gemini slug for a tier ("pro" / "flash"): newest version, then the variant closest to the
    wanted effort (slugs carry it: gemini-3.1-pro-high / -low). .env ANTIGRAVITY_MODEL_PRO / _FLASH win.
    None → let the CLI use its default model."""
    override = os.getenv(f"ANTIGRAVITY_MODEL_{tier.upper()}", "").strip()
    if override:
        return override
    pool = [m for m in available if m.startswith("gemini") and tier in m and "lite" not in m and "image" not in m]
    if not pool:
        return None
    newest = max(_version(m) for m in pool)
    pool = [m for m in pool if _version(m) == newest]
    want = {"low": 1, "medium": 2}.get(effort, 3)

    def closeness(m: str) -> tuple:
        s = _EFFORT_SUFFIX.search(m)
        level = {"low": 1, "medium": 2, "high": 3}[s.group(1)] if s else 2
        return -abs(level - want), level      # nearest effort; on a tie the stronger variant
    return max(pool, key=closeness)


# --------------------------------------------------------------------------- the call
class AntigravityJSON:
    """Same interface as novel_translator.ClaudeJSON.call(), backed by `agy` headless (stream-json on stdin,
    so a long chapter never hits the Windows command-line length limit)."""
    timeout_min = 20

    def __init__(self, model: str, effort: str = "medium"):
        from .novel_translator import MODELS
        self.model = model
        self.tier = MODELS.get(model, {}).get("tier", "pro")
        self.effort = {"xhigh": "high"}.get(effort, effort) if effort in ("low", "medium", "high", "xhigh", "max") else ""
        self.exe = find_agy()
        if not self.exe:
            raise LLMAuthError(f"Chưa cài Antigravity CLI (lệnh `agy`). Mở PowerShell và chạy:  {INSTALL_CMD}  "
                               "rồi chạy  agy  một lần để đăng nhập Google.", code="ANTIGRAVITY_MISSING")
        self.slug = pick_model(self.tier, status(probe=True).get("models") or [], self.effort or "high")

    def _cmd(self, schema_path: Path) -> list[str]:
        cmd = [self.exe, "--input-format", "stream-json", "--output-format", "stream-json",
               "--json-schema", str(schema_path), "--print-timeout", f"{self.timeout_min}m", "--sandbox"]
        if self.slug:
            cmd += ["--model", self.slug]
        if self.effort and not (self.slug and _EFFORT_SUFFIX.search(self.slug)):   # the slug already fixes the effort
            cmd += ["--effort", self.effort]
        return cmd

    def call(self, *, system: str, user: str, schema: dict, max_tokens: int = 64000,
             refusal_message: str = "Gemini từ chối xử lý nội dung này") -> tuple[dict, dict, str]:
        wd = _workdir()
        schema_path = wd / f"schema_{hashlib.md5(json.dumps(schema, sort_keys=True).encode()).hexdigest()[:12]}.json"
        if not schema_path.exists():
            schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        prompt = f"{system.strip()}\n\n---\n\n{user.strip()}" if system.strip() else user
        line = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False) + "\n"
        for attempt in range(2):
            try:
                out = _run_agy(self._cmd(schema_path), line, wd, self.timeout_min * 60 + 120)
            except subprocess.TimeoutExpired as e:
                tail = e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
                log.warning("agy timeout after %d min, last output: %s", self.timeout_min, tail[-1500:].replace("\n", " | "))
                raise LLMTimeout(f"Antigravity không trả kết quả sau {self.timeout_min} phút.")
            except OSError as e:
                raise LLMError(f"Không chạy được Antigravity CLI: {e}", code="ANTIGRAVITY_MISSING")
            res = _result_event(out.stdout)
            if attempt == 0 and res and _INTERRUPTED.search(json.dumps(res.get("error"), ensure_ascii=False)):
                log.warning("agy stream interrupted mid-answer — asking again once")   # a dropped connection, not the text
                continue
            break
        u = (res or {}).get("usage") or {}
        usage = {"input_tokens": u.get("input_tokens", 0) or 0,
                 "output_tokens": (u.get("output_tokens", 0) or 0) + (u.get("thinking_tokens", 0) or 0),
                 "cache_read_input_tokens": u.get("cache_read_tokens", 0) or 0}
        status_ = str((res or {}).get("status") or "").upper()
        if res is None or status_ not in ("SUCCESS", ""):
            err = (res or {}).get("error")
            text = " ".join(str(x) for x in (json.dumps(err, ensure_ascii=False) if err else "", (res or {}).get("response") or "",
                                             out.stderr or "", "" if res else out.stdout or "") if x).strip()
            raise _map_error(text, status_, out.returncode, usage, refusal_message)
        response = str(res.get("response") or "")
        data = res.get("structured_output")
        if not isinstance(data, dict):
            data = _parse_json(response)
        if not isinstance(data, dict):
            if _SAFETY.search(response) and len(response) < 600:
                raise _map_error(response, "SAFETY", 0, usage, refusal_message)
            raise LLMEmptyResponse("Gemini trả về dữ liệu không đúng định dạng JSON.", details={"usage": usage})
        model_used = f"antigravity:{self.slug or 'default'}"
        log.info("agy ok: model=%s in=%s out=%s", model_used, usage["input_tokens"], usage["output_tokens"])
        return data, usage, model_used


def _run_agy(cmd: list[str], stdin: str, cwd: Path, timeout: float):
    """Like subprocess.run(capture_output=True), but returns as soon as the result event is printed: `agy` sometimes
    prints its result and then never exits, which used to hold a finished chapter until the timeout threw it away."""
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         encoding="utf-8", errors="replace", cwd=cwd)
    timed_out = threading.Event()
    timer = threading.Timer(timeout, lambda: (timed_out.set(), p.kill()))

    def feed():      # in a thread: a big prompt must not deadlock against agy's own output
        try:
            p.stdin.write(stdin)
            p.stdin.close()
        except OSError:
            pass
    lines: list[str] = []
    timer.start()
    threading.Thread(target=feed, daemon=True).start()
    try:
        for raw in p.stdout:
            lines.append(raw)
            if _result_event(raw) is not None:
                break
    finally:
        timer.cancel()
        if p.poll() is None:
            p.kill()
        p.wait()
    if timed_out.is_set():
        raise subprocess.TimeoutExpired(cmd, timeout, output="".join(lines))
    return subprocess.CompletedProcess(cmd, p.returncode, "".join(lines), "")


def _result_event(stdout: str) -> Optional[dict]:
    """The final result from stream-json (or plain json) output."""
    found = None
    for raw in (stdout or "").splitlines():
        raw = raw.strip()
        if not raw.startswith("{"):
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("event") == "result" and isinstance(obj.get("result"), dict):
            found = obj["result"]
        elif "status" in obj and ("response" in obj or "error" in obj):
            found = obj
    return found


def _parse_json(text: str):
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    for candidate in (cleaned, (m.group(0) if (m := re.search(r"\{.*\}", cleaned, re.S)) else "")):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _map_error(text: str, status_: str, returncode: int, usage: dict, refusal_message: str) -> LLMError:
    from .novel_translator import LLMRefused
    short = re.sub(r"\s+", " ", text)[:300] or f"không có phản hồi (mã {returncode}, trạng thái {status_ or '?'})"
    if _INTERRUPTED.search(text):       # first: the half-written answer in `text` may contain words like "blocked"
        return LLMTimeout(f"Mất kết nối tới Gemini giữa chừng: {short[:150]}")
    if _BAD_MODEL.search(text):
        return LLMError(f"Model Antigravity không hợp lệ: {short}. Xem danh sách bằng  agy models  và đặt "
                        "ANTIGRAVITY_MODEL_PRO / ANTIGRAVITY_MODEL_FLASH trong .env.", code="AI_BAD_MODEL")
    if _QUOTA.search(text):
        return LLMUsageLimit(f"Đã hết hạn mức Gemini của Antigravity: {short}")
    if _LOGIN.search(text):
        return LLMAuthError("Antigravity CLI chưa đăng nhập. Mở terminal, chạy  agy  một lần để đăng nhập Google.",
                            code="ANTIGRAVITY_LOGIN")
    if status_ == "SAFETY" or _SAFETY.search(text):
        return LLMRefused(f"{refusal_message} (bộ lọc an toàn của Gemini).", details={"usage": usage})
    if _RATE.search(text):
        return LLMRateLimit(f"Gemini đang quá tải / giới hạn tốc độ: {short[:150]}")
    if status_ in ("CANCELED", "INTERRUPTED", "WAITING", "RUNNING"):
        return LLMTimeout(f"Antigravity dừng giữa chừng (trạng thái {status_}).")
    return LLMError(f"Antigravity lỗi: {short}", code="ANTIGRAVITY_ERROR", details={"usage": usage})


# --------------------------------------------------------------------------- chapter translation
def build_message(heading: str, paragraphs: list[str], glossary_text: str, prev_tail: list[str], notes: str = "",
                  fix: str = "") -> str:
    """The per-chapter message: labelled sections, the chapter as JSON, the hard constraints repeated at
    the end (Gemini follows what it read last most reliably)."""
    n = len(paragraphs)
    parts = []
    if notes.strip():
        parts.append(f"## GHI CHÚ TRUYỆN (nhân vật, quan hệ, bối cảnh)\n{notes.strip()}")
    parts.append(f"## THUẬT NGỮ (bắt buộc dùng đúng)\n{glossary_text.strip() or '(trống)'}")
    if prev_tail:
        parts.append("## ĐOẠN CUỐI CHƯƠNG TRƯỚC (bản Việt — để nối mạch tên và xưng hô, KHÔNG dịch lại)\n" + "\n".join(prev_tail))
    parts.append(f"## CHƯƠNG CẦN DỊCH — N = {n} đoạn\n"
                 + json.dumps({"heading": heading, "paragraphs": paragraphs}, ensure_ascii=False))
    if fix:
        parts.append(f"## SỬA LỖI LẦN TRƯỚC\n{fix}")
    parts.append(f"Trả về đúng một JSON: \"heading\", \"paragraphs\" (ĐÚNG {n} phần tử, theo thứ tự), \"new_terms\". "
                 "Không còn chữ Hàn, không markdown, không lời dẫn.")
    return "\n\n".join(parts)


def _problems(data: dict, n: int) -> str:
    paras = data.get("paragraphs") or []
    issues = []
    if len(paras) != n:
        issues.append(f"Bạn trả về {len(paras)} đoạn nhưng đầu vào có {n} đoạn. Phải đúng {n} phần tử, mỗi đoạn gốc một phần tử, "
                      "không gộp/tách/bỏ đoạn.")
    joined = " ".join(str(p) for p in paras)
    if joined and len(_HANGUL.findall(joined)) > max(3, len(joined) * 0.005):
        issues.append("Bản dịch còn sót chữ Hàn (Hangul). Dịch hoặc phiên âm hết, không để lại ký tự Hangul.")
    return " ".join(issues)


class AntigravityTranslator(AntigravityJSON):
    def translate(self, *, heading: str, paragraphs: list[str], glossary_text: str, prev_tail: list[str],
                  style: str, notes: str = ""):
        from .novel_translator import SCHEMA, ChapterResult
        system = rules(style)
        total: dict = {}
        best, fix = None, ""
        for attempt in range(getattr(self, "max_attempts", 2)):   # Gemini sometimes merges paragraphs or leaves Hangul: one corrective retry
            data, usage, model = self.call(system=system, user=build_message(heading, paragraphs, glossary_text, prev_tail, notes, fix),
                                           schema=SCHEMA, refusal_message="Gemini từ chối dịch chương này")
            for k, v in usage.items():
                total[k] = total.get(k, 0) + v
            from .grok import _as_paragraphs
            data["paragraphs"] = _as_paragraphs(data.get("paragraphs"))
            if not data.get("paragraphs"):
                fix = "Lần trước bạn trả về bản dịch rỗng. Hãy dịch đầy đủ chương."
                continue
            fix = _problems(data, len(paragraphs))
            off = abs(len(data["paragraphs"]) - len(paragraphs))
            if best is None or not fix or off < best[2]:
                best = (data, model, off)
            if not fix:
                break
            log.info("agy retry (%s)", fix[:80])
        if best is None:
            raise LLMEmptyResponse("Gemini trả về bản dịch rỗng.", details={"usage": total})
        data, model, _ = best
        return ChapterResult(heading=str(data.get("heading") or "").strip(), paragraphs=[str(p) for p in data["paragraphs"]],
                             new_terms=[t for t in data.get("new_terms", []) if isinstance(t, dict) and t.get("source") and t.get("target")],
                             usage=total, model=model)
