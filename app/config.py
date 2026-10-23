"""Central configuration. Values come from environment variables / `.env`."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", BASE_DIR / "output"))
UPLOAD_DIR = DATA_DIR / "uploads"
PROJECTS_DIR = DATA_DIR / "projects"
LOG_DIR = DATA_DIR / "logs"
WATTPAD_PROFILE_DIR = Path(os.getenv("WATTPAD_PROFILE_DIR", DATA_DIR / "wattpad_profile"))

HOST = os.getenv("APP_HOST", "127.0.0.1")
PORT = int(os.getenv("APP_PORT", "8765"))

# --- AI ---
# LLM_PROVIDER: anthropic | openai | local | mock | (empty = disabled)
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "").strip().lower()
LLM_MODEL = os.getenv("LLM_MODEL", "").strip()
LLM_TIMEOUT = float(os.getenv("LLM_TIMEOUT", "90"))
LLM_EFFORT = os.getenv("LLM_EFFORT", "").strip().lower()  # anthropic only: low|medium|high|xhigh|max
LOCAL_LLM_BASE_URL = os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:11434/v1").strip()

# --- Wattpad ---
# extension  = publish through the helper extension installed in your everyday Edge (default)
# playwright = a separate automated Edge window driven by the tool
WATTPAD_MODE = os.getenv("WATTPAD_MODE", "extension").strip().lower()
WATTPAD_BASE_URL = os.getenv("WATTPAD_BASE_URL", "https://www.wattpad.com").strip()
WATTPAD_BROWSER_CHANNEL = os.getenv("WATTPAD_BROWSER_CHANNEL", "").strip()  # "", chrome, msedge
# Optional proxy for the automation browser (a VPN app's local proxy), e.g. http://127.0.0.1:7890
WATTPAD_PROXY = os.getenv("WATTPAD_PROXY", "").strip()
WATTPAD_HEADLESS = os.getenv("WATTPAD_HEADLESS", "false").strip().lower() == "true"
WATTPAD_LOGIN_TIMEOUT = int(os.getenv("WATTPAD_LOGIN_TIMEOUT", "600"))
WATTPAD_PUBLISH_WAIT = int(os.getenv("WATTPAD_PUBLISH_WAIT", "30"))  # seconds to confirm a publish
# Claude Code (subscription): stop sending work once this share of the 5-hour window is used (keeps the rest for you)
CLAUDE_CODE_STOP_AT = float(os.getenv("CLAUDE_CODE_STOP_AT", "0.8"))


def ensure_dirs() -> None:
    for d in (DATA_DIR, OUTPUT_DIR, UPLOAD_DIR, PROJECTS_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
