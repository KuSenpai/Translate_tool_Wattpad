"""Use the local Claude Code CLI (`claude -p`) instead of the API: the work is counted against the
user's Claude subscription (Pro/Max) — no API key, no per-token billing.

Requirements: Claude Code installed and signed in with the subscription:
    claude auth login --claudeai
ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are removed from the CLI's environment, otherwise the CLI
would use the API key (API billing) instead of the subscription.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from .. import config
from ..logging_setup import get_logger
from .llm_service import LLMAuthError, LLMEmptyResponse, LLMError, LLMRateLimit, LLMTimeout

log = get_logger("CLAUDE-CODE")
_ENV_DROP = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
             "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_PROFILE")
_USAGE_LIMIT = re.compile(r"usage limit|limit reached|hit your (?:\w+ )?limit|5-hour limit|weekly limit|out of (?:extra )?usage",
                          re.IGNORECASE)


class LLMUsageLimit(LLMError):
    """The subscription's usage window is used up — retrying now will not help."""
    status = 429
    code = "AI_USAGE_LIMIT"


def find_claude() -> Optional[str]:
    """Path of the claude executable. The npm `claude.cmd` wrapper is skipped (cmd.exe would mangle
    quotes in the prompt/JSON arguments); its claude.exe is called directly."""
    override = os.getenv("CLAUDE_CODE_PATH", "").strip()
    if override:
        return override
    found = shutil.which("claude")
    candidates = []
    if found:
        p = Path(found)
        candidates.append(p.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")
        if p.suffix.lower() == ".exe" or os.name != "nt":
            candidates.append(p)
    appdata = os.getenv("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")
    candidates.append(Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude"))
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in _ENV_DROP}
    env.setdefault("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "64000")
    return env


def _workdir() -> Path:
    d = config.DATA_DIR / "claude_code_cwd"   # empty folder: no project CLAUDE.md / settings get loaded
    d.mkdir(parents=True, exist_ok=True)
    return d


_status_cache: tuple[float, dict] = (0.0, {})
# Last 5-hour window reported by the CLI ("rate_limit_event"): {"utilization": 0..1, "resetsAt": epoch}
five_hour: dict = {}


def status(force: bool = False) -> dict:
    """{"installed", "logged_in", "auth_method", "subscription"} — from `claude auth status` (free)."""
    global _status_cache
    if not force and time.time() - _status_cache[0] < 30:
        return _status_cache[1]
    exe = find_claude()
    st = {"installed": bool(exe), "logged_in": False, "auth_method": None, "subscription": None, "path": exe}
    if exe:
        try:
            out = subprocess.run([exe, "auth", "status"], capture_output=True, text=True, encoding="utf-8",
                                 errors="replace", timeout=30, env=_env(), cwd=_workdir())
            data = json.loads(out.stdout or "{}")
            st.update(logged_in=bool(data.get("loggedIn")), auth_method=data.get("authMethod"),
                      subscription=data.get("subscriptionType") or data.get("subscription"))
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
            st["error"] = str(e)[:200]
    _status_cache = (time.time(), st)
    return st


class ClaudeCodeJSON:
    """Same interface as novel_translator.ClaudeJSON.call(), backed by `claude -p`."""

    def __init__(self, model: str, effort: str = "medium"):
        from .novel_translator import MODELS
        self.model = model
        self.alias = MODELS.get(model, {}).get("alias", "opus")
        self.effort = effort
        self.exe = find_claude()
        if not self.exe:
            raise LLMAuthError("Không tìm thấy Claude Code (lệnh `claude`). Cài: npm install -g @anthropic-ai/claude-code",
                               code="CLAUDE_CODE_MISSING")
        st = status(force=True)
        if not st["logged_in"]:
            raise LLMAuthError("Claude Code chưa đăng nhập. Mở terminal và chạy:  claude auth login --claudeai  "
                               "(đăng nhập bằng tài khoản Claude có gói Pro/Max).", code="CLAUDE_CODE_LOGIN")

    def call(self, *, system: str, user: str, schema: dict, max_tokens: int = 64000,
             refusal_message: str = "Claude từ chối xử lý nội dung này") -> tuple[dict, dict, str]:
        used, resets = five_hour.get("utilization") or 0, five_hour.get("resetsAt") or 0
        if used >= config.CLAUDE_CODE_STOP_AT and resets > time.time():
            # "|epoch" lets the jobs' reset_delay() resume right when the window resets
            raise LLMUsageLimit(f"Đã dùng {used:.0%} hạn mức 5 tiếng của gói Claude (ngưỡng dừng "
                                f"{config.CLAUDE_CODE_STOP_AT:.0%}) — chờ tới {time.strftime('%H:%M', time.localtime(resets))}|{int(resets)}")
        cmd = [self.exe, "-p", "--output-format", "stream-json", "--verbose", "--tools", "", "--no-session-persistence",
               "--setting-sources", "user", "--model", self.alias, "--system-prompt", system,
               "--json-schema", json.dumps(schema, ensure_ascii=False)]
        if self.effort:
            cmd += ["--effort", self.effort]
        try:
            out = subprocess.run(cmd, input=user, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 timeout=1200, env=_env(), cwd=_workdir())
        except subprocess.TimeoutExpired:
            raise LLMTimeout("Claude Code không trả kết quả sau 20 phút.")
        except OSError as e:
            raise LLMError(f"Không chạy được Claude Code: {e}", code="CLAUDE_CODE_MISSING")
        obj = {}
        for line in (out.stdout or "").splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "result":
                obj = ev
            elif ev.get("type") == "rate_limit_event":
                w = ((ev.get("rate_limit_info") or {}).get("unifiedWindows") or {}).get("five_hour")
                if w:
                    five_hour.update(w)
        if not obj:
            tail = (out.stderr or out.stdout or "").strip()[-300:]
            raise LLMError(f"Claude Code lỗi (mã {out.returncode}): {tail or 'không có phản hồi'}", code="CLAUDE_CODE_ERROR")
        u = obj.get("usage") or {}
        usage = {"input_tokens": u.get("input_tokens", 0) or 0, "output_tokens": u.get("output_tokens", 0) or 0,
                 "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0) or 0,
                 "cache_read_input_tokens": u.get("cache_read_input_tokens", 0) or 0}
        text = str(obj.get("result") or "")
        if obj.get("is_error"):
            if "not logged in" in text.lower() or "/login" in text:
                raise LLMAuthError("Claude Code chưa đăng nhập. Chạy:  claude auth login --claudeai", code="CLAUDE_CODE_LOGIN")
            if "does not support this model" in text or "claude update" in text:
                raise LLMAuthError("Claude Code quá cũ cho model này. Chạy:  npm install -g @anthropic-ai/claude-code@latest",
                                   code="CLAUDE_CODE_UPDATE")
            if _USAGE_LIMIT.search(text):
                raise LLMUsageLimit(f"Đã hết lượt dùng của gói Claude: {text[:200]}")
            if obj.get("api_error_status") in (429, 529) or "overloaded" in text.lower() or "rate limit" in text.lower():
                raise LLMRateLimit(f"Claude đang quá tải / giới hạn tốc độ: {text[:150]}")
            raise LLMError(f"Claude Code lỗi: {text[:300]}", code="CLAUDE_CODE_ERROR", details={"usage": usage})
        if obj.get("stop_reason") == "refusal":
            from .novel_translator import LLMRefused
            raise LLMRefused(f"{refusal_message}.", details={"usage": usage})
        data = obj.get("structured_output")
        if not isinstance(data, dict):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
            try:
                data = json.loads(cleaned)
            except json.JSONDecodeError:
                m = re.search(r"\{.*\}", cleaned, re.S)
                try:
                    data = json.loads(m.group(0)) if m else None
                except json.JSONDecodeError:
                    data = None
        if not isinstance(data, dict):
            raise LLMEmptyResponse("Claude Code trả về dữ liệu không đúng định dạng.", details={"usage": usage})
        model_used = next(iter(obj.get("modelUsage") or {}), self.alias)
        log.info("claude -p ok: model=%s in=%s out=%s", model_used, usage["input_tokens"], usage["output_tokens"])
        return data, usage, model_used
