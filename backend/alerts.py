"""Alerts: buy zone, breakout confirmation, stop approaching, target reached, volume
spike, important company news and trend reversal. Each alert rings ONCE (a dedupe
key per symbol, kind and day), like the trader-alerts skill.

Telegram: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (your own bot from
@BotFather and your chat id). Without them alerts stay in the app only."""

import json
import time
import urllib.parse
import urllib.request

from . import db
from .config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from .market import calendar as cal

BULL = {"STRONG BULLISH", "BULLISH"}
BEAR = {"BEARISH", "AVOID"}


def telegram(text):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        return False, "Telegram not configured"
    url = "https://api.telegram.org/bot%s/sendMessage" % TELEGRAM_BOT_TOKEN
    data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": text[:4000],
                                   "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=15) as r:
            ok = json.loads(r.read().decode()).get("ok", False)
        return ok, "sent" if ok else "Telegram refused the message"
    except Exception as e:  # noqa: BLE001
        return False, "Telegram error: %s" % str(e)[:100]


# Owners whose alerts may also go to the Telegram chat in TELEGRAM_CHAT_ID (set by main: the
# local user "" and the site owner's logins). Friends' alerts stay in the app.
TELEGRAM_OWNERS = {""}


def watchers():
    """{owner: set of symbols} from everyone's watchlist."""
    out = {}
    for r in db.q("SELECT owner, symbol FROM user_watchlist"):
        out.setdefault(r["owner"], set()).add(r["symbol"])
    return out


def notify(symbol, kind, message, level="info", key_extra=""):
    """Alert the current owner (db.owner())."""
    day = cal.now_ist().date().isoformat()
    if kind in ("buy_zone", "breakout", "volume_spike") and not cal.market_status()["open"]:
        message += " [market closed: based on the last close]"
    aid = db.add_alert(symbol, kind, message, level, dedupe="%s|%s|%s|%s" % (symbol, kind, day, key_extra))
    if aid and db.owner() in TELEGRAM_OWNERS:
        ok, _ = telegram("[%s] %s" % (kind.replace("_", " ").upper(), message))
        if ok:
            db.x("UPDATE alerts SET telegram=1 WHERE id=?", (aid,))
    return aid


def evaluate_scan(rows):
    """Alerts for each person's watchlist symbols from a fresh scan."""
    for who, watch in watchers().items():
        with db.as_owner(who):
            _evaluate_scan_for(rows, watch)


def _evaluate_scan_for(rows, watch):
    for r in rows:
        if r["symbol"] not in watch:
            continue
        sym, px, plan = r["symbol"], r["price"], r.get("plan") or {}
        if plan.get("entry_low") and plan.get("entry_high") and px and r["rating"] in BULL and \
                plan["entry_low"] <= px <= plan["entry_high"]:
            notify(sym, "buy_zone", "%s at %.2f is inside its entry range %.2f-%.2f (%s). Check the confirmation "
                                    "before acting." % (sym, px, plan["entry_low"], plan["entry_high"], plan["setup"]),
                   "bullish")
        for s in ("breakout_20d", "resistance_breakout", "orb_breakout"):
            if s in r["setups"]:
                notify(sym, "breakout", "%s: %s confirmed at %.2f" % (
                    sym, r["setup_labels"][r["setups"].index(s)], px), "bullish", s)
        if (r.get("vol_ratio") or 0) > 2 or "volume_spike" in r["setups"]:
            notify(sym, "volume_spike", "%s: unusual volume (%.1fx average)" % (sym, r.get("vol_ratio") or 0),
                   "caution")
        prev = db.q("SELECT rating FROM signals WHERE symbol=? AND mode='swing' ORDER BY ts DESC LIMIT 1 OFFSET 1",
                    (sym,), one=True)
        if prev and ((prev["rating"] in BULL and r["rating"] in BEAR) or (prev["rating"] in BEAR and
                                                                          r["rating"] in BULL)):
            notify(sym, "trend_reversal", "%s rating changed %s -> %s" % (sym, prev["rating"], r["rating"]),
                   "bullish" if r["rating"] in BULL else "bearish", r["rating"])


def evaluate_news(items):
    interest = watchers()
    for r in db.q("SELECT owner, symbol FROM paper_positions WHERE status='open'"):
        interest.setdefault(r["owner"], set()).add(r["symbol"])
    for who, symbols in interest.items():
        with db.as_owner(who):
            for it in items:
                hit = symbols & set(it["symbols"])
                if hit and it["impact"] in ("high", "medium") and it.get("age_hours", 99) <= 24:
                    for s in hit:
                        notify(s, "news", "%s: %s (%s, %s)" % (s, it["title"][:160], it["source"],
                                                               it["published_ist"]),
                               "bearish" if it["sentiment"] == "negative" else "bullish"
                               if it["sentiment"] == "positive" else "info", it["id"])


def recent(limit=100, unseen_only=False):
    sql = "SELECT * FROM alerts WHERE owner=? %s ORDER BY ts DESC LIMIT ?" % ("AND seen=0" if unseen_only else "")
    return db.q(sql, (db.owner(), limit))


def mark_seen(ids=None):
    if ids:
        for i in ids:
            db.x("UPDATE alerts SET seen=1 WHERE id=? AND owner=?", (i, db.owner()))
    else:
        db.x("UPDATE alerts SET seen=1 WHERE owner=?", (db.owner(),))
    return {"ok": True, "ts": time.time()}
