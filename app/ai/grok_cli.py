"""Translate with Grok through the official Grok Build CLI (`grok`) in headless mode, using the grok.com
account you signed in with — no API key to paste into the tool.

Setup (once):
    irm https://x.ai/cli/install.ps1 | iex      (PowerShell)
    grok                                         (first run opens the browser to sign in)

The CLI runs in an empty folder outside the project with every built-in tool removed (`--tools ""`) and web
search off, so the agent never reads or writes the user's files. The prompt goes through `--prompt-file`
(headless mode does not read stdin) and the answer comes back as `structuredOutput` (`--json-schema`).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from ..logging_setup import get_logger
from .claude_code import LLMUsageLimit
from .grok import GrokTranslator
from .llm_service import LLMAuthError, LLMEmptyResponse, LLMError, LLMRateLimit, LLMTimeout

log = get_logger("GROK-CLI")

INSTALL_CMD = "irm https://x.ai/cli/install.ps1 | iex"
SYSTEM_OVERRIDE = ("You are a professional literary translator working inside a text-only sandbox. Never use any tool, "
                   "never browse, never ask questions. Answer immediately with the JSON object requested in the message.")
_LOGIN = re.compile(r"not (?:logged|signed) in|sign[ -]?in|log[ -]?in|unauthori[sz]ed|unauthenticated|authenticat|"
                    r"credential|\b401\b|\b403\b|invalid (?:api )?key", re.IGNORECASE)
_REFUSED = re.compile(r"can'?t (?:help|assist)|cannot (?:help|assist)|unable to (?:help|assist)|content (?:policy|moderation|filter)", re.IGNORECASE)
_QUOTA = re.compile(r"usage limit|limit (?:reached|exceeded)|quota|out of credits|credits? (?:exhausted|used up)|"
                    r"exceeded your|spending limit|subscription", re.IGNORECASE)
_RATE = re.compile(r"\b429\b|\b50[0-4]\b|rate[ -]?limit|overloaded|unavailable|try again|timed? ?out", re.IGNORECASE)
_BAD_MODEL = re.compile(r"unknown model|model .*not (?:found|available|supported)|invalid model|unsupported model", re.IGNORECASE)
_LOGGED_IN = re.compile(r"logged in", re.IGNORECASE)
_MODEL_LINE = re.compile(r"^\s*\*?\s*(grok[-\w.]*)", re.IGNORECASE | re.MULTILINE)


def find_grok() -> Optional[str]:
    override = os.getenv("GROK_CLI_PATH", "").strip()
    if override:
        return override
    found = shutil.which("grok")
    if found:
        return found
    home = Path(os.getenv("GROK_HOME", "") or Path.home() / ".grok")
    for c in (home / "bin" / ("grok.exe" if os.name == "nt" else "grok"),
              Path.home() / ".local" / "bin" / ("grok.exe" if os.name == "nt" else "grok")):
        if c.exists():
            return str(c)
    return None


def _workdir() -> Path:
    """Empty folder outside the project (no .git above it) so the agent has nothing to read."""
    d = Path(tempfile.gettempdir()) / "translate-tool-grok"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _env(home: Optional[str] = None) -> dict:
    env = dict(os.environ)
    env["GROK_MEMORY"] = "0"                # no cross-session memory leaking between chapters
    env["GROK_DISABLE_AUTOUPDATER"] = "1"
    if home:                                # another account = another GROK_HOME (its own auth.json)
        env["GROK_HOME"] = home
    return env


# --------------------------------------------------------------------------- accounts
# GROK_CLI_HOMES="default;D:\path\acc2;D:\path\acc3": one GROK_HOME per grok.com account. Calls are spread over the
# accounts (parts of a chapter run on different accounts) and an account that is rate limited or out of quota is
# skipped for a while. Empty → just the default login (~/.grok).
_pool_lock = threading.Lock()
_rr = 0
_cooldown: dict[str, float] = {}
RATE_COOLDOWN, QUOTA_COOLDOWN = 45, 1800


def accounts() -> list[dict]:
    parts = [p.strip().strip('"') for p in (os.getenv("GROK_CLI_HOMES", "") or "").split(";") if p.strip()]
    out = []
    for p in parts:
        if p.lower() == "default":
            out.append({"id": "default", "name": "mặc định", "home": None})
        elif not any(a["id"] == p for a in out):
            out.append({"id": p, "name": Path(p).name or p, "home": p})
    return out or [{"id": "default", "name": "mặc định", "home": None}]


def _next_account(skip: set) -> Optional[dict]:
    global _rr
    now = time.time()
    with _pool_lock:
        ready = [a for a in accounts() if a["id"] not in skip and _cooldown.get(a["id"], 0) <= now]
        if not ready:
            return None
        acc = ready[_rr % len(ready)]
        _rr += 1
        return acc


def _rest(acc: dict, seconds: float) -> None:
    with _pool_lock:
        _cooldown[acc["id"]] = time.time() + seconds


# --------------------------------------------------------------------------- status
_cache: tuple[float, dict] = (0.0, {})


def _probe(exe: str, acc: dict) -> dict:
    st = {"id": acc["id"], "name": acc["name"], "logged_in": False, "models": [], "error": None}
    try:
        out = subprocess.run([exe, "models"], capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=60, cwd=_workdir(), env=_env(acc["home"]))
        text = (out.stdout or "") + "\n" + (out.stderr or "")
        st["models"] = list(dict.fromkeys(m.lower() for m in _MODEL_LINE.findall(out.stdout or "")))
        st["logged_in"] = bool(_LOGGED_IN.search(text)) and not re.search(r"not logged in", text, re.I) and bool(st["models"])
        if not st["logged_in"]:
            st["error"] = ("Chưa đăng nhập — chạy  grok login  với GROK_HOME của tài khoản này." if acc["home"]
                           else "Chưa đăng nhập Grok CLI — mở terminal, chạy  grok  một lần để đăng nhập tài khoản grok.com.")
    except (OSError, subprocess.TimeoutExpired) as e:
        st["error"] = str(e)[:200]
    return st


def status(probe: bool = False, force: bool = False) -> dict:
    """{"installed", "path", "logged_in", "models", "accounts", "checked", "error", "install_cmd"}. Only `probe=True`
    runs `grok models` for each account (cheap, no model call). `logged_in` = at least one account is signed in."""
    global _cache
    exe = find_grok()
    accs = accounts()
    key = (exe, tuple(a["id"] for a in accs))
    if _cache[1].get("key") == key and (not probe or _cache[1].get("checked")) and not force and time.time() - _cache[0] < 300:
        return _cache[1]
    st = {"installed": bool(exe), "path": exe, "logged_in": None, "models": [], "checked": False, "error": None,
          "install_cmd": INSTALL_CMD, "key": key, "accounts": [{"id": a["id"], "name": a["name"], "logged_in": None} for a in accs]}
    if exe and not probe:
        return st
    if exe:
        st["checked"] = True
        st["accounts"] = [_probe(exe, a) for a in accs]
        st["logged_in"] = any(a["logged_in"] for a in st["accounts"])
        st["models"] = list(dict.fromkeys(m for a in st["accounts"] for m in a["models"]))
        if not st["logged_in"]:
            st["error"] = next((a["error"] for a in st["accounts"] if a["error"]), None)
    _cache = (time.time(), st)
    return st


def pick_model() -> Optional[str]:
    """GROK_CLI_MODEL wins; otherwise the CLI's default model (None → no -m flag)."""
    return os.getenv("GROK_CLI_MODEL", "").strip() or None


# --------------------------------------------------------------------------- the call
class GrokCLIJSON:
    """Same interface as novel_translator.ClaudeJSON.call(), backed by `grok` headless."""
    label = "Grok CLI"

    def __init__(self, model: str, effort: str = "medium"):
        self.model = model
        self.effort = effort if effort in ("low", "medium", "high", "xhigh") else ""
        self.exe = find_grok()
        if not self.exe:
            raise LLMAuthError(f"Chưa cài Grok CLI (lệnh `grok`). Mở PowerShell và chạy:  {INSTALL_CMD}  "
                               "rồi chạy  grok  một lần để đăng nhập.", code="GROK_CLI_MISSING")
        self.slug = pick_model()

    def _cmd(self, prompt_file: Path) -> list[str]:
        # No --json-schema: with it the CLI sometimes never returns (same prompt: >2×10 min hung vs 3 min without).
        # The schema goes into the prompt instead and the answer is parsed from the text.
        cmd = [self.exe, "--prompt-file", str(prompt_file), "--output-format", "json", "--cwd", str(_workdir()), "--tools", "", "--disable-web-search",
               "--no-subagents", "--no-plan", "--max-turns", "3", "--no-auto-update",
               "--system-prompt-override", SYSTEM_OVERRIDE]
        if self.slug:
            cmd += ["-m", self.slug]
        if self.effort:
            cmd += ["--effort", self.effort]
        return cmd

    def call(self, *, system: str, user: str, schema: dict, max_tokens: int = 64000,
             refusal_message: str = "Grok từ chối xử lý nội dung này") -> tuple[dict, dict, str]:
        """One headless call. With several accounts (GROK_CLI_HOMES) the call goes to the next free account and, when
        that one is rate limited / out of quota, straight to another; with a single account a rate limit is retried
        after a pause (xAI allows only a few requests per second per account)."""
        multi = len(accounts()) > 1
        skip: set = set()
        last: Optional[LLMError] = None
        while (acc := _next_account(skip)) is not None:
            try:
                if multi:
                    return self._call_once(acc, system=system, user=user, schema=schema, max_tokens=max_tokens,
                                           refusal_message=refusal_message)
                return self._call_patient(acc, system=system, user=user, schema=schema, max_tokens=max_tokens,
                                          refusal_message=refusal_message)
            except (LLMRateLimit, LLMUsageLimit) as e:
                last = e
                skip.add(acc["id"])
                if multi:
                    _rest(acc, QUOTA_COOLDOWN if isinstance(e, LLMUsageLimit) else RATE_COOLDOWN)
                    log.warning("grok cli account “%s” unavailable (%s) — trying another", acc["name"], e.message[:100])
        if last is not None:
            raise last
        raise LLMRateLimit("Tất cả tài khoản Grok CLI đang bị giới hạn — thử lại sau ít phút.")

    def _call_patient(self, acc: dict, **kw) -> tuple[dict, dict, str]:
        for attempt, wait in enumerate((8, 25, None)):
            try:
                return self._call_once(acc, **kw)
            except LLMRateLimit as e:
                if wait is None:
                    raise
                log.warning("grok cli rate limited (%s) — retry %d in %ds", e.message[:120], attempt + 1, wait)
                time.sleep(wait)
        raise AssertionError("unreachable")

    def _call_once(self, acc: dict, *, system: str, user: str, schema: dict, max_tokens: int = 64000,
                   refusal_message: str = "Grok từ chối xử lý nội dung này") -> tuple[dict, dict, str]:
        wd = _workdir()
        prompt_file = wd / f"prompt_{uuid.uuid4().hex[:10]}.txt"
        prompt = f"{system.strip()}\n\n---\n\n{user.strip()}" if system.strip() else user
        prompt += ("\n\n---\n\nReply with ONLY one JSON object (no markdown, no commentary) matching this JSON Schema:\n"
                   + json.dumps(schema, ensure_ascii=False))
        prompt_file.write_text(prompt, encoding="utf-8")
        log.info("grok cli call started (account=%s, effort=%s, prompt %d chars) — a long chapter takes several minutes",
                 acc["name"], self.effort or "default", prompt_file.stat().st_size)
        try:
            # A healthy call takes 1–6 min; a hung stream sits idle forever, so cut it at 10 min and retry once.
            for attempt in (1, 2):
                try:
                    out = subprocess.run(self._cmd(prompt_file), capture_output=True, text=True, encoding="utf-8",
                                         errors="replace", timeout=600, cwd=wd, env=_env(acc["home"]))
                    break
                except subprocess.TimeoutExpired:
                    if attempt == 2:
                        raise LLMTimeout("Grok CLI không trả kết quả sau 2 lần chờ 10 phút.")
                    log.warning("grok cli hung >10 min (account=%s) — retrying once", acc["name"])
        except OSError as e:
            raise LLMError(f"Không chạy được Grok CLI: {e}", code="GROK_CLI_MISSING")
        finally:
            prompt_file.unlink(missing_ok=True)
        res = _json_object(out.stdout)
        usage = _usage(res)
        if out.returncode != 0 or res is None or res.get("type") == "error":
            text = " ".join(x for x in ((res or {}).get("message", ""), out.stderr or "", "" if res else out.stdout or "") if x).strip()
            raise _map_error(text, out.returncode, usage, refusal_message)
        stop = str(res.get("stopReason") or "")
        if stop == "refusal":
            from .novel_translator import LLMRefused
            raise LLMRefused(f"{refusal_message}.", details={"usage": usage})
        if stop == "cancelled" and not res.get("structuredOutput"):   # xAI moderation cuts the stream mid-answer
            from .novel_translator import LLMRefused
            raise LLMRefused(f"{refusal_message} — xAI ngắt giữa chừng (bộ lọc nội dung). "
                             "Bật dịch theo phần để tự chia nhỏ, hoặc dịch chương này bằng model khác.", details={"usage": usage})
        if stop == "max_tokens":
            raise LLMEmptyResponse("Kết quả bị cắt (quá dài cho một lần gọi).", details={"usage": usage})
        data = res.get("structuredOutput") or res.get("structured_output")
        if isinstance(data, str):
            data = _parse_json(data)
        if not isinstance(data, dict):
            data = _parse_json(str(res.get("text") or res.get("result") or ""))
        if not isinstance(data, dict):
            log.warning("grok cli bad JSON (stop=%s, keys=%s): %.800s", stop, list(res), out.stdout)
            raise LLMEmptyResponse("Grok trả về dữ liệu không đúng định dạng JSON.", details={"usage": usage})
        model_used = f"grok-cli:{next(iter(res.get('modelUsage') or {}), None) or self.slug or 'default'}"
        log.info("grok cli ok (account=%s): model=%s in=%s out=%s cost=%s", acc["name"], model_used, usage["input_tokens"], usage["output_tokens"],
                 res.get("total_cost_usd"))
        return data, usage, model_used


def _json_object(stdout: str) -> Optional[dict]:
    text = (stdout or "").strip()
    for candidate in (text, text[text.find("{"):] if "{" in text else ""):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    for raw in reversed(text.splitlines()):
        raw = raw.strip()
        if raw.startswith("{"):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                continue
    return None


# Without --json-schema the model now and then closes a JSON string with a typographic quote: `…hay.’”,"next"`.
# An unescaped `”,"` / `”]` / `”}` can't occur inside a valid JSON string, so it can only be a broken delimiter.
_CURLY_DELIM = re.compile(r'[”“](?=\s*(?:,\s*"|\]|\}))')


def _parse_json(text: str):
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    whole = m.group(0) if (m := re.search(r"\{.*\}", cleaned, re.S)) else ""
    for candidate in (cleaned, whole, _CURLY_DELIM.sub('"', whole)):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


def _usage(res: Optional[dict]) -> dict:
    u = (res or {}).get("usage") or {}
    return {"input_tokens": u.get("input_tokens", 0) or 0, "output_tokens": u.get("output_tokens", 0) or 0,
            "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens": u.get("cache_read_input_tokens", 0) or 0}


def _map_error(text: str, returncode: int, usage: dict, refusal_message: str) -> LLMError:
    short = re.sub(r"\s+", " ", text)[:300] or f"không có phản hồi (mã {returncode})"
    if _BAD_MODEL.search(text):
        return LLMError(f"Model Grok CLI không hợp lệ: {short}. Xem danh sách bằng  grok models  và đặt GROK_CLI_MODEL trong .env.",
                        code="AI_BAD_MODEL")
    if _REFUSED.search(text):       # before the login check: a refusal also arrives as "403 permission-denied"
        from .novel_translator import LLMRefused
        return LLMRefused(f"{refusal_message} — xAI từ chối nội dung này (403: “I can't help with that request”). "
                          "Thử chia nhỏ chương hoặc dịch phần đó bằng model khác.", details={"usage": usage})
    if _QUOTA.search(text):
        return LLMUsageLimit(f"Đã hết hạn mức Grok: {short}")
    if _LOGIN.search(text):
        return LLMAuthError("Grok CLI chưa đăng nhập. Mở terminal, chạy  grok  một lần để đăng nhập tài khoản grok.com.",
                            code="GROK_CLI_LOGIN")
    if _RATE.search(text):
        lim = re.search(r"\(actual/limit\):\s*(\d+/\d+)", text)
        return LLMRateLimit("Grok chạm giới hạn tốc độ của tài khoản" + (f" ({lim.group(1)} yêu cầu/giây)" if lim else "")
                            + f" — đừng chạy nhiều lượt Grok cùng lúc. {short[:120]}")
    return LLMError(f"Grok CLI lỗi: {short}", code="GROK_CLI_ERROR", details={"usage": usage})


class GrokCLITranslator(GrokCLIJSON):
    translate = GrokTranslator.translate   # same chapter flow as the API version (one corrective retry on a bad paragraph count)
