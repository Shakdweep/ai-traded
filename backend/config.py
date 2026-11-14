"""Settings. Secrets come from environment variables (or a local .env file that is
never served to the browser). Nothing here is sent to the frontend except the
`public()` view, which reports only WHETHER a provider is configured."""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("TRADER_DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)


def _load_dotenv(path=ROOT / ".env"):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if v[:1] in ('"', "'"):
            v = v[1:].split(v[0], 1)[0]
        else:
            v = v.split(" #", 1)[0].split("\t#", 1)[0].strip()  # inline comment
        os.environ.setdefault(k.strip(), v)


_load_dotenv()


def env(name, default=""):
    return os.environ.get(name, default)


DB_PATH = DATA_DIR / "trader.db"
HOST = env("TRADER_HOST", "127.0.0.1")
PORT = int(env("TRADER_PORT") or env("PORT", "8000"))  # PORT is set by hosts like Render

# Background refresh
BACKGROUND_UNIVERSE = env("TRADER_BACKGROUND_UNIVERSE", "nifty100")
SCAN_EVERY_MIN_OPEN = int(env("TRADER_SCAN_EVERY_MIN", "10"))
SCAN_EVERY_MIN_CLOSED = int(env("TRADER_SCAN_EVERY_MIN_CLOSED", "120"))
NEWS_EVERY_MIN = int(env("TRADER_NEWS_EVERY_MIN", "10"))
FETCH_WORKERS = int(env("TRADER_FETCH_WORKERS", "4"))
DISABLE_BACKGROUND = env("TRADER_DISABLE_BACKGROUND", "") == "1"
ENABLE_SCREENER = env("TRADER_ENABLE_SCREENER", "1") == "1"

# Notifications (optional)
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID")

# Licensed broker market-data APIs (optional; see README "Data providers")
BROKER_ENV = {
    "zerodha_kite": ["KITE_API_KEY", "KITE_ACCESS_TOKEN"],
    "upstox": ["UPSTOX_ACCESS_TOKEN"],
    "angel_smartapi": ["ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_JWT_TOKEN"],
}


def public():
    """Safe-to-show configuration: booleans only, never the values."""
    return {
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
        "brokers": {k: all(env(v) for v in vs) for k, vs in BROKER_ENV.items()},
        "background_universe": BACKGROUND_UNIVERSE,
        "screener_enabled": ENABLE_SCREENER,
    }
