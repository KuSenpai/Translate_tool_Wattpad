"""Subprocess entry point for Wattpad automation.

    stdin : one JSON command  {"action": "login" | "stories" | "publish", ...}
    stdout: JSON lines        {"type": "progress", "message": ...} ... {"type": "result", "ok": bool, ...}

Running Playwright in its own process keeps the web server responsive and isolated from
browser crashes / event-loop conflicts.
"""
from __future__ import annotations

import json
import sys
import traceback

from .. import config
from .wattpad_publisher import WattpadError, WattpadPublisher


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def run(cmd: dict) -> dict:
    progress = lambda m: emit({"type": "progress", "message": m})
    action = cmd.get("action")
    with WattpadPublisher(progress) as wp:
        if action == "login":
            return wp.login(int(cmd.get("timeout") or config.WATTPAD_LOGIN_TIMEOUT))
        if action == "stories":
            return wp.list_stories()
        if action == "publish":
            return wp.publish(story_id=str(cmd["story_id"]), title=cmd["title"], blocks=cmd["blocks"],
                              mode=cmd.get("mode", "publish"), part_id=cmd.get("part_id"), update=bool(cmd.get("update")))
    raise WattpadError("WATTPAD_BAD_COMMAND", f"Unknown action: {action}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        cmd = json.loads(sys.stdin.read())
        emit({"type": "result", "ok": True, **run(cmd)})
    except WattpadError as e:
        emit({"type": "result", "ok": False, "error_code": e.code, "message": e.message, **e.extra})
    except ImportError as e:
        emit({"type": "result", "ok": False, "error_code": "WATTPAD_BROWSER",
              "message": f"Chưa cài Playwright: {e}. Chạy: pip install playwright"})
    except Exception as e:  # never let the worker die silently
        msg = str(e).splitlines()[0] if str(e) else type(e).__name__
        code = "WATTPAD_NETWORK" if "net::ERR_" in str(e) else "WATTPAD_ERROR"
        if "Target" in type(e).__name__ or "closed" in str(e).lower():
            code, msg = "WATTPAD_BROWSER", "Cửa sổ trình duyệt đã bị đóng giữa chừng."
        traceback.print_exc(file=sys.stderr)
        emit({"type": "result", "ok": False, "error_code": code, "message": msg})


if __name__ == "__main__":
    main()
