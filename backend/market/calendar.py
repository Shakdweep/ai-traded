"""NSE/BSE trading calendar in India Standard Time (UTC+5:30, no daylight saving).

Replaces the NYSE calendar used throughout the day-trader skills.
Regular session 09:15-15:30 IST, pre-open 09:00-09:15 (order entry 09:00-09:08).

HOLIDAYS: the 2026 list below is from NSE's published trading-holiday circular as
best known when this was written; it is NOT verified against the live circular.
Edit data/holidays.json to correct or extend it. The app also cross-checks data
freshness: if the clock says "open" but no new bars arrive, the UI says so.
"""

import datetime
import json

from ..config import DATA_DIR

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "IST")
OPEN = datetime.time(9, 15)
CLOSE = datetime.time(15, 30)
PRE_OPEN = datetime.time(9, 0)
SESSION_BARS_5M = 75  # 09:15 -> 15:30 in 5-minute bars
OPEN_MIN = 9 * 60 + 15

_DEFAULT_HOLIDAYS = {
    "2026-01-26": "Republic Day", "2026-03-03": "Holi", "2026-03-26": "Shri Ram Navami",
    "2026-03-31": "Shri Mahavir Jayanti", "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti", "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id", "2026-06-26": "Muharram", "2026-09-14": "Ganesh Chaturthi",
    "2026-10-02": "Mahatma Gandhi Jayanti", "2026-10-20": "Dussehra",
    "2026-11-10": "Diwali Balipratipada", "2026-11-24": "Prakash Gurpurb Sri Guru Nanak Dev",
    "2026-12-25": "Christmas",
}
_HOLIDAY_FILE = DATA_DIR / "holidays.json"


def holidays():
    if not _HOLIDAY_FILE.exists():
        _HOLIDAY_FILE.write_text(json.dumps({"_note": "Verify against the NSE trading-holiday circular; "
                                             "dates here are not verified.", **_DEFAULT_HOLIDAYS}, indent=2))
    try:
        data = json.loads(_HOLIDAY_FILE.read_text())
    except ValueError:
        data = _DEFAULT_HOLIDAYS
    return {k: v for k, v in data.items() if not k.startswith("_")}


def now_ist():
    return datetime.datetime.now(IST)


def to_ist(ts):
    return datetime.datetime.fromtimestamp(ts, IST)


def is_trading_day(d):
    return d.weekday() < 5 and d.isoformat() not in holidays()


def market_status(now=None):
    now = now or now_ist()
    d = now.date()
    t = now.time()
    if not is_trading_day(d):
        why = holidays().get(d.isoformat(), "weekend")
        return {"state": "closed", "label": "Closed (%s)" % why, "open": False, "now": now.isoformat(),
                "next_open": next_open(now).isoformat()}
    if t < PRE_OPEN:
        state, label = "pre", "Pre-market"
    elif t < OPEN:
        state, label = "preopen", "Pre-open session"
    elif t < CLOSE:
        state, label = "open", "Market open"
    else:
        state, label = "closed", "Closed for the day"
    return {"state": state, "label": label, "open": state == "open", "now": now.isoformat(),
            "next_open": next_open(now).isoformat()}


def next_open(now):
    d = now.date()
    if now.time() >= OPEN or not is_trading_day(d):
        d += datetime.timedelta(days=1)
        while not is_trading_day(d):
            d += datetime.timedelta(days=1)
    return datetime.datetime.combine(d, OPEN, IST)


def last_trading_day(now=None):
    now = now or now_ist()
    d = now.date()
    if not (is_trading_day(d) and now.time() >= OPEN):
        d -= datetime.timedelta(days=1)
        while not is_trading_day(d):
            d -= datetime.timedelta(days=1)
    return d


def hhmm(text):
    h, m = text.split(":")
    return datetime.time(int(h), int(m))
