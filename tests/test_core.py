"""Offline unit tests (synthetic data, no network).
Run:  .venv/Scripts/python -m unittest discover -s tests -v
"""

import base64
import datetime
import math
import os
import random
import tempfile
import time
import unittest

os.environ["TRADER_DATA_DIR"] = tempfile.mkdtemp(prefix="trader-test-")
os.environ["TRADER_DISABLE_BACKGROUND"] = "1"

from backend import db  # noqa: E402
from backend.analysis import backtest, engine, indicators as ind, risk as rk, tradeplan  # noqa: E402
from backend.market import calendar as cal  # noqa: E402
from backend.market import yahoo  # noqa: E402
from backend.vendor.day_trader_skill import scan as skill_scan  # noqa: E402
from backend.vendor.day_trader_skill import ticket as skill_ticket  # noqa: E402
from backend.vendor.day_trader_skill import timemachine as tm  # noqa: E402

SETTINGS = dict(db.DEFAULT_SETTINGS)


def daily_bars(n=300, start=100.0, drift=0.001, vol=0.012, seed=1):
    rng = random.Random(seed)
    t0 = int(datetime.datetime(2025, 1, 1, 9, 15, tzinfo=cal.IST).timestamp())
    px, bars = start, []
    for i in range(n):
        o = px
        c = o * (1 + drift + rng.gauss(0, vol))
        h, l = max(o, c) * (1 + abs(rng.gauss(0, vol / 2))), min(o, c) * (1 - abs(rng.gauss(0, vol / 2)))
        bars.append({"t": t0 + i * 86400, "o": o, "h": h, "l": l, "c": c, "v": 1e6 * (1 + rng.random())})
        px = c
    return bars


def chart_of(bars, sym="TEST.NS", interval="1d"):
    last = bars[-1]
    meta = {"regularMarketTime": last["t"], "regularMarketPrice": last["c"], "gmtoffset": 19800,
            "exchangeTimezoneName": "Asia/Kolkata", "longName": "Test Ltd"}
    return {"symbol": sym, "meta": meta, "bars": bars, "fetched": last["t"], "interval": interval, "range": "2y"}


class Indicators(unittest.TestCase):
    def test_rsi_matches_skill(self):
        c = [b["c"] for b in daily_bars()]
        self.assertAlmostEqual(ind.rsi_series(c)[-1], skill_scan.rsi(c), places=9)

    def test_ema_sma(self):
        xs = list(range(1, 31))
        self.assertAlmostEqual(ind.sma(xs, 5)[-1], 28.0)
        self.assertIsNone(ind.ema(xs, 10)[8])
        self.assertAlmostEqual(ind.ema([5.0] * 40, 20)[-1], 5.0)

    def test_macd_constant_is_zero(self):
        line, sig, hist = ind.macd([10.0] * 80)
        self.assertAlmostEqual(line[-1], 0.0)
        self.assertAlmostEqual(hist[-1], 0.0)

    def test_vwap(self):
        bars = [{"h": 11, "l": 9, "c": 10, "v": 100}, {"h": 21, "l": 19, "c": 20, "v": 300}]
        self.assertAlmostEqual(ind.vwap_session(bars)[-1], (10 * 100 + 20 * 300) / 400)

    def test_levels_sides(self):
        bars = daily_bars(200, seed=3)
        sup, res = ind.levels(bars)
        px = bars[-1]["c"]
        self.assertTrue(all(s["price"] < px for s in sup))
        self.assertTrue(all(r["price"] > px for r in res))


class Engine(unittest.TestCase):
    def test_uptrend_is_bullish_downtrend_is_bearish(self):
        up = engine.analyse_daily(chart_of(daily_bars(300, drift=0.004, vol=0.008, seed=2)))
        dn = engine.analyse_daily(chart_of(daily_bars(300, drift=-0.004, vol=0.008, seed=2)))
        self.assertGreater(up["score"], 25)
        self.assertIn(up["rating"], ("STRONG BULLISH", "BULLISH", "HIGH RISK"))
        self.assertLess(dn["score"], -20)
        self.assertIn(dn["rating"], ("BEARISH", "AVOID", "HIGH RISK"))
        if dn["rating"] == "HIGH RISK":  # only as a counter-trend oversold bounce
            self.assertIn("oversold_bounce", [s["key"] for s in dn["setups"]])
        # every point is explained
        self.assertEqual(sum(r["points"] for r in up["reasons"]), max(-100, min(100, up["score"])))

    def test_insufficient_data(self):
        a = engine.analyse_daily(chart_of(daily_bars(40)))
        self.assertEqual(a["rating"], "INSUFFICIENT DATA")

    def test_confidence_bounds(self):
        a = engine.analyse_daily(chart_of(daily_bars(300, seed=5)))
        self.assertTrue(0 <= a["confidence"]["value"] <= 100)

    def test_weak_fundamentals_downgrade(self):
        r, why = engine.rate(60, [], ["breakout_20d"], "Weak fundamentals", "swing")
        self.assertEqual(r, "HIGH RISK")
        r, _ = engine.rate(60, [], ["breakout_20d"], "Weak fundamentals", "intraday")
        self.assertEqual(r, "STRONG BULLISH")

    def test_bearish_gets_no_trade(self):
        dn = engine.analyse_daily(chart_of(daily_bars(300, drift=-0.004, vol=0.008, seed=2)))
        p = tradeplan.swing_plan(dn, SETTINGS)
        self.assertFalse(p["actionable"])
        self.assertIn(p["status"], ("NO TRADE", "WAIT"))
        dn = dict(dn, rating="BEARISH")
        self.assertEqual(tradeplan.swing_plan(dn, SETTINGS)["status"], "NO TRADE")


class Risk(unittest.TestCase):
    def test_size_respects_max_loss(self):
        s = rk.size_position(500.0, 490.0, SETTINGS, target=520.0)
        self.assertGreater(s["qty"], 0)
        self.assertLessEqual(s["max_loss"], SETTINGS["capital"] * SETTINGS["risk_per_trade_pct"] / 100 + 0.01)
        self.assertLessEqual(s["capital_required"], SETTINGS["capital"] * SETTINGS["max_position_pct"] / 100)

    def test_size_refuses_stop_above_entry(self):
        self.assertEqual(rk.size_position(100.0, 101.0, SETTINGS)["qty"], 0)

    def test_size_refuses_too_wide_stop(self):
        s = rk.size_position(50000.0, 10000.0, SETTINGS)
        self.assertEqual(s["qty"], 0)
        self.assertIn("refused", s)

    def test_costs_positive_and_delivery_dearer(self):
        a = rk.round_trip_costs(100, 500, 510, "intraday")
        b = rk.round_trip_costs(100, 500, 510, "delivery")
        self.assertGreater(a["total"], 0)
        self.assertGreater(b["total"], a["total"])

    def test_gate(self):
        order = {"symbol": "X", "side": "buy", "qty": 10, "entry": 500, "stop": 490, "target1": 520, "mode": "swing"}
        self.assertEqual(rk.check_order(order, SETTINGS, [], [], 0)["verdict"], "PASS")
        self.assertEqual(rk.check_order(dict(order, stop=0), SETTINGS, [], [], 0)["verdict"], "BLOCK")
        self.assertEqual(rk.check_order(dict(order, qty=1000), SETTINGS, [], [], 0)["verdict"], "BLOCK")
        self.assertEqual(rk.check_order(dict(order, target1=505), SETTINGS, [], [], 0)["verdict"], "BLOCK")
        self.assertEqual(rk.check_order(dict(order, side="sell"), SETTINGS, [], [], 0)["verdict"], "BLOCK")
        three = [{"qty": 1, "entry_price": 1, "stop": 1}] * 3
        self.assertEqual(rk.check_order(order, SETTINGS, three, [], 0)["verdict"], "BLOCK")
        self.assertEqual(rk.check_order(order, SETTINGS, [], [], -1950)["verdict"], "BLOCK")

    def test_tick_rounding(self):
        self.assertAlmostEqual(rk.tick_down(100.07), 100.05)
        self.assertAlmostEqual(rk.tick_up(100.01), 100.05)


class Calendar(unittest.TestCase):
    def test_sessions(self):
        sat = datetime.datetime(2026, 11, 14, 10, 0, tzinfo=cal.IST)
        self.assertFalse(cal.market_status(sat)["open"])
        mon = datetime.datetime(2026, 11, 16, 10, 0, tzinfo=cal.IST)
        self.assertTrue(cal.market_status(mon)["open"])
        self.assertEqual(cal.market_status(mon.replace(hour=9, minute=5))["state"], "preopen")
        self.assertEqual(cal.next_open(sat).date(), datetime.date(2026, 11, 16))
        self.assertFalse(cal.is_trading_day(datetime.date(2026, 1, 26)))


class SkillReuse(unittest.TestCase):
    def test_yes_parser(self):
        o = {"symbol": "INFY", "side": "buy", "qty": "10", "type": "market", "time_in_force": "day",
             "order_class": "bracket", "stop_loss_stop_price": 1500.0, "take_profit_limit_price": 1600.0,
             "plan": {"entry": 1550.0, "stop": 1500.0}}
        self.assertTrue(skill_ticket.parse_yes("yes", o)[0])
        self.assertFalse(skill_ticket.parse_yes("ok", o)[0])
        self.assertFalse(skill_ticket.parse_yes("yes to all", o)[0])
        self.assertFalse(skill_ticket.parse_yes("yes TCS", o, ["TCS", "INFY"])[0])

    def test_time_machine_nse_geometry(self):
        self.assertEqual(tm.SESSION_BARS, 75)
        rng = random.Random(4)
        sessions = []
        for d in range(25):
            p, D = 100.0, []
            for i in range(75):
                c = p * (1 + rng.gauss(0, 0.002))
                D.append({"o": p, "h": max(p, c) * 1.001, "l": min(p, c) * 0.999, "c": c, "date": "d%02d" % d,
                          "slot": i})
                p = c
            sessions.append(("2026-01-%02d" % (d + 1), D))
        pool = tm.build_pool(sessions)
        mc = tm.simulate(pool, 100.0, 99.0, 102.0, 3, 71, replays=2000, seed=19)
        self.assertAlmostEqual(mc["p_target"] + mc["p_stop"] + mc["p_neither"], 1.0, places=3)
        hist = tm.history_replay(sessions, {"or_minutes": 15, "stop_rule": "or_mid", "target_r": 2.0}, 71)
        self.assertEqual(hist["sessions"], 25)

    def test_fill_missing_bar(self):
        t0 = int(datetime.datetime(2026, 11, 13, 9, 15, tzinfo=cal.IST).timestamp())
        bars = [{"t": t0 + 300 * i, "o": 1, "h": 1, "l": 1, "c": 1, "v": 1} for i in range(75) if i != 73]
        st = {}
        out = backtest._nse_bars({"bars": bars}, st)
        self.assertEqual(len(out), 75)
        self.assertEqual(st["filled_bars"], 1)


class Quotes(unittest.TestCase):
    def test_prev_close_uses_exchange_date(self):
        # a US index: bar at 09:30 ET (20:00 IST), quote after midnight IST the same US day
        t_bar = int(datetime.datetime(2026, 11, 13, 14, 30, tzinfo=datetime.timezone.utc).timestamp())
        bars = [{"t": t_bar - 86400, "o": 1, "h": 1, "l": 1, "c": 100.0, "v": 0},
                {"t": t_bar, "o": 1, "h": 1, "l": 1, "c": 99.0, "v": 0}]
        meta = {"regularMarketTime": t_bar + 6.5 * 3600, "regularMarketPrice": 99.0, "gmtoffset": -18000,
                "chartPreviousClose": 50.0}
        q = yahoo.quote_from_chart({"symbol": "^GSPC", "meta": meta, "bars": bars, "interval": "1d",
                                    "fetched": 0})
        self.assertAlmostEqual(q["change_pct"], -1.0)

    def test_missing_latest_session_is_patched(self):
        bars = daily_bars(5)
        meta = {"regularMarketTime": bars[-1]["t"] + 86400 + 3600, "regularMarketPrice": 123.0,
                "regularMarketDayHigh": 125.0, "regularMarketDayLow": 120.0, "gmtoffset": 19800,
                "exchangeTimezoneName": "Asia/Kolkata"}
        yahoo._patch_latest_session(bars, meta)
        self.assertEqual(len(bars), 6)
        self.assertTrue(bars[-1]["synthetic"])
        self.assertEqual(bars[-1]["c"], 123.0)

    def test_never_live(self):
        f = yahoo.freshness({"regularMarketTime": 1})
        self.assertIn(f["label"], ("STALE", "CLOSED"))
        self.assertNotEqual(f["label"], "LIVE")


if __name__ == "__main__":
    unittest.main()


class TestAuth(unittest.TestCase):
    """Invite-only accounts: owner logins, email sign-up with a code, forgot password."""

    def setUp(self):
        from backend import auth, mailer
        self.auth, self.mailer = auth, mailer
        self._saved = (auth.USERS, auth.INVITE_CODE, auth.INVITE_REQUIRED, auth.SIGNUP_ENABLED,
                       auth.EMAIL_ENABLED, auth.FILE_USERS, auth.INVITE_HASH, mailer.send)
        auth.USERS = {"owner": "fixed-password-1"}
        auth.FILE_USERS, auth.INVITE_HASH = {}, ""
        auth.INVITE_CODE, auth.INVITE_REQUIRED, auth.SIGNUP_ENABLED, auth.EMAIL_ENABLED = "join-me", True, True, True
        self.outbox = []
        mailer.send = lambda to, subject, text, html: self.outbox.append((to, subject, text))
        db.db()
        for t in ("users", "accounts", "email_codes", "invites"):
            db.x("DELETE FROM %s" % t)

    def tearDown(self):
        a = self.auth
        (a.USERS, a.INVITE_CODE, a.INVITE_REQUIRED, a.SIGNUP_ENABLED, a.EMAIL_ENABLED, a.FILE_USERS,
         a.INVITE_HASH, self.mailer.send) = self._saved

    def last_code(self):
        import re
        return re.search(r"\b(\d{6})\b", self.outbox[-1][2]).group(1)

    def test_owner_login_and_token(self):
        a = self.auth
        self.assertEqual(a.check_password("Owner", "fixed-password-1"), "owner")
        self.assertIsNone(a.check_password("owner", "wrong"))
        self.assertIsNone(a.check_password("nobody", "x"))
        tok = a.make_token("owner")
        self.assertEqual(a.read_token(tok), "owner")
        self.assertIsNone(a.read_token(tok[:-4] + "AAAA"))           # tampered
        self.assertIsNone(a.read_token(a.make_token("owner", now=1)))  # expired
        legacy = base64.urlsafe_b64encode(("owner|%d|%s" % (int(time.time()) + 600, a._sign("owner|%d" % (
            int(time.time()) + 600)))).encode()).decode()
        self.assertEqual(a.read_token(legacy), "owner")                 # older seconds-based cookie still works
        self.assertIsNone(a.read_token("not-a-token"))

    def test_hashed_access_file_entries(self):
        a = self.auth
        a.FILE_USERS = {"boss": a.make_hash("boss-secret-pass")}
        a.INVITE_CODE, a.INVITE_HASH = "", a.make_hash("hashed-invite")
        self.assertNotIn("boss-secret-pass", a.FILE_USERS["boss"])
        self.assertEqual(a.check_password("BOSS", "boss-secret-pass"), "boss")
        self.assertIsNone(a.check_password("boss", "boss-secret-pas"))
        self.assertIn("invite", a.signup_start("Pal", "pal@example.com", "password123", "wrong")[1])
        self.assertIsNone(a.signup_start("Pal", "pal@example.com", "password123", " hashed-invite ")[1])

    def test_signup_with_emailed_code(self):
        a = self.auth
        self.assertIn("invite", a.signup_start("Ravi", "ravi@example.com", "password123", "nope")[1])
        self.assertIn("name", a.signup_start(" ", "ravi@example.com", "password123", "join-me")[1])
        self.assertIn("email", a.signup_start("Ravi", "not-an-email", "password123", "join-me")[1])
        self.assertIn("8", a.signup_start("Ravi", "ravi@example.com", "short", "join-me")[1])
        self.assertEqual(self.outbox, [])
        masked, err = a.signup_start("Ravi  Kumar", "Ravi@Example.com", "password123", "join-me")
        self.assertIsNone(err)
        self.assertEqual(masked, "ra••@example.com")
        self.assertEqual(self.outbox[-1][0], "ravi@example.com")
        code = self.last_code()
        row = db.q("SELECT * FROM email_codes", one=True)
        self.assertNotIn(code, row["code_hash"])            # only a keyed hash is stored
        self.assertNotIn("password123", row["payload"])     # and only the password hash
        self.assertIsNone(a.get_account("ravi@example.com"))  # not created until confirmed
        wrong = "000000" if code != "000000" else "111111"
        self.assertIn("4 tries left", a.signup_verify("ravi@example.com", wrong)[1])
        self.assertEqual(a.signup_verify("ravi@example.com", code), ("ravi@example.com", None))
        acct = a.get_account("ravi@example.com")
        self.assertEqual(acct["name"], "Ravi Kumar")
        self.assertEqual(acct["status"], "pending")
        self.assertIsNone(a.read_token(a.make_token("ravi@example.com")))  # no session while pending
        self.assertTrue(a.approve("ravi@example.com"))
        self.assertIn("expired", a.signup_verify("ravi@example.com", code)[1])  # one use only
        self.assertEqual(a.check_password("RAVI@example.com", "password123"), "ravi@example.com")
        self.assertIsNone(a.check_password("ravi@example.com", "password124"))
        self.assertEqual(a.read_token(a.make_token("ravi@example.com")), "ravi@example.com")
        self.assertEqual(a.display_name("ravi@example.com"), "Ravi Kumar")
        self.assertIn("already exists", a.signup_start("R", "ravi@example.com", "password123", "join-me")[1])

    def test_code_limits(self):
        a = self.auth
        t = 1_000_000.0
        self.assertIsNone(a.signup_start("A", "a@example.com", "password123", "join-me", now=t)[1])
        self.assertIn("wait a minute", a.signup_start("A", "a@example.com", "password123", "join-me", now=t + 10)[1])
        self.assertIsNone(a.signup_start("A", "a@example.com", "password123", "join-me", now=t + 61)[1])
        code = self.last_code()  # only the newest code works
        for i in range(2, 5):
            self.assertIsNone(a.signup_start("A", "a@example.com", "password123", "join-me", now=t + 61 * i)[1])
        self.assertIn("Too many codes", a.signup_start("A", "a@example.com", "password123", "join-me", now=t + 400)[1])
        newest = self.last_code()
        if newest != code:
            self.assertIsNotNone(a.signup_verify("a@example.com", code, now=t + 300)[1])
        self.assertIn("expired", a.signup_verify("a@example.com", newest, now=t + 61 * 4 + 601)[1])

    def test_five_wrong_guesses_kill_the_code(self):
        a = self.auth
        a.signup_start("B", "b@example.com", "password123", "join-me")
        code = self.last_code()
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(5):
            self.assertIsNotNone(a.signup_verify("b@example.com", wrong)[1])
        self.assertIn("Too many wrong tries", a.signup_verify("b@example.com", code)[1])

    def test_forgot_password(self):
        a = self.auth
        a.signup_start("Meera", "meera@example.com", "old-password", "join-me")
        a.signup_verify("meera@example.com", self.last_code())
        a.approve("meera@example.com")
        old_session = a.make_token("meera@example.com", now=time.time() - 60)
        self.assertEqual(a.read_token(old_session), "meera@example.com")
        # unknown email: same answer, but no email is sent
        sent = len(self.outbox)
        self.assertEqual(a.forgot_start("ghost@example.com"), ("gh•••@example.com", None))
        self.assertEqual(len(self.outbox), sent)
        self.assertIsNone(a.forgot_start("meera@example.com")[1])
        code = self.last_code()
        self.assertIn("8", a.forgot_verify("meera@example.com", code, "short")[1])
        self.assertEqual(a.forgot_verify("meera@example.com", code, "new-password-1"), ("meera@example.com", None))
        self.assertIsNone(a.check_password("meera@example.com", "old-password"))
        self.assertEqual(a.check_password("meera@example.com", "new-password-1"), "meera@example.com")
        self.assertIsNone(a.read_token(old_session))  # older sessions are signed out
        self.assertEqual(a.read_token(a.make_token("meera@example.com", now=time.time() + 5)), "meera@example.com")

    def test_email_off(self):
        a = self.auth
        a.EMAIL_ENABLED = False
        self.assertIn("Email isn't set up", a.signup_start("C", "c@example.com", "password123", "join-me")[1])
        self.assertIn("Email isn't set up", a.forgot_start("c@example.com")[1])

    def test_rate_limit(self):
        a = self.auth
        ip = "203.0.113.9"
        a.clear_fails(ip)
        for _ in range(a.MAX_FAILS):
            a.record_fail(ip)
        self.assertTrue(a.too_many_fails(ip))
        a.clear_fails(ip)
        self.assertFalse(a.too_many_fails(ip))


class TestPhoneAccounts(TestAuth):
    """Mobile-number sign-up (no OTP) and the owner's Members tools."""

    def test_clean_phone(self):
        c = self.auth.clean_phone
        for raw in ("9876543210", "98765 43210", "+91 98765-43210", "09876543210", "919876543210", "(+91) 98765 43210"):
            self.assertEqual(c(raw), "+919876543210", raw)
        self.assertEqual(c("+14155550123"), "+14155550123")
        for bad in ("12345", "5876543210", "+91 5876543210", "98765432", "abcdefghij", "", None, "+9198765432101"):
            self.assertIsNone(c(bad), bad)

    def test_phone_signup_and_login(self):
        a = self.auth
        self.assertIn("invite", a.phone_signup("Kiran", "9876543210", "password123", "nope")[1])
        self.assertIn("mobile", a.phone_signup("Kiran", "12345", "password123", "join-me")[1])
        self.assertIn("8", a.phone_signup("Kiran", "9876543210", "short", "join-me")[1])
        self.assertEqual(a.phone_signup(" Kiran  R ", "98765 43210", "password123", "join-me"), ("+919876543210", None))
        self.assertEqual(self.outbox, [])  # no OTP
        self.assertEqual(a.account_status("+919876543210"), "pending")
        self.assertIsNone(a.read_token(a.make_token("+919876543210")))
        a.approve("+919876543210")
        self.assertEqual(a.account_status("+919876543210"), "active")
        self.assertIn("already exists", a.phone_signup("K", "+91 98765 43210", "password123", "join-me")[1])
        for typed in ("9876543210", "+91 98765 43210", "09876543210"):
            self.assertEqual(a.check_password(typed, "password123"), "+919876543210", typed)
        self.assertIsNone(a.check_password("9876543210", "password124"))
        self.assertEqual(a.read_token(a.make_token("+919876543210")), "+919876543210")
        self.assertEqual(a.display_name("+919876543210"), "Kiran R")
        self.assertEqual(a.check_password("owner", "fixed-password-1"), "owner")  # owner username still works

    def test_owner_tools(self):
        a = self.auth
        a.phone_signup("Dev", "9123456789", "password123", "join-me")
        a.approve("+919123456789")
        self.assertTrue(a.is_owner("owner"))
        self.assertFalse(a.is_owner("+919123456789"))
        self.assertFalse(a.is_owner(None))
        self.assertEqual([r["id"] for r in a.list_accounts()], ["+919123456789"])
        old = a.make_token("+919123456789", now=time.time() - 60)
        temp = a.owner_reset_password("+919123456789")
        self.assertEqual(len(temp), 10)
        self.assertIsNone(a.check_password("9123456789", "password123"))
        self.assertEqual(a.check_password("9123456789", temp), "+919123456789")
        self.assertIsNone(a.read_token(old))  # signed out by the reset
        self.assertIsNone(a.owner_reset_password("+910000000000"))
        self.assertTrue(a.owner_remove("+919123456789"))
        self.assertIsNone(a.check_password("9123456789", temp))
        self.assertFalse(a.owner_remove("+919123456789"))


class TestInvitesAndApproval(TestAuth):
    """Single-use invite codes, owner approval, optional email for password reset."""

    def test_single_use_codes(self):
        a = self.auth
        a.INVITE_CODE = ""  # no shared code: only personal codes work
        self.assertIn("invite", a.phone_signup("X", "9000000001", "password123", "join-me")[1])
        code = a.create_invite("Rahul")
        self.assertRegex(code, r"^ait-[a-z2-9]{4}-[a-z2-9]{4}$")
        row = db.q("SELECT * FROM invites", one=True)
        self.assertNotIn(code, row["code_hash"])  # stored hashed
        self.assertEqual((row["hint"], row["note"]), (code[-4:], "Rahul"))
        self.assertEqual(a.phone_signup("Rahul", "9000000001", "password123", code.upper()), ("+919000000001", None))
        self.assertEqual(a.list_invites()[0]["used_by"], "+919000000001")
        self.assertIn("already been used", a.phone_signup("Y", "9000000002", "password123", code)[1])
        self.assertIsNone(a.get_account("+919000000002"))
        acct = a.list_accounts()[0]
        self.assertEqual((acct["status"], acct["invite_note"]), ("pending", "Rahul"))
        self.assertFalse(a.revoke_invite(row["id"]))  # used codes can't be cancelled
        code2 = a.create_invite("")
        inv2 = [i for i in a.list_invites() if i["hint"] == code2[-4:]][0]
        self.assertTrue(a.revoke_invite(inv2["id"]))
        self.assertIn("invite", a.phone_signup("Z", "9000000003", "password123", code2)[1])

    def test_email_signup_uses_invite_once(self):
        a = self.auth
        a.INVITE_CODE = ""
        code = a.create_invite("Sam")
        self.assertIsNone(a.signup_start("Sam", "sam@example.com", "password123", code)[1])
        sam_code = self.last_code()
        # someone else uses the same invite first (mobile) -> the email confirmation is refused
        self.assertIsNone(a.phone_signup("Other", "9000000009", "password123", code)[1])
        self.assertIn("already been used", a.signup_verify("sam@example.com", sam_code)[1])
        self.assertIsNone(a.get_account("sam@example.com"))

    def test_pending_login_and_owner_active(self):
        a = self.auth
        a.phone_signup("P", "9000000004", "password123", "join-me")
        self.assertEqual(a.check_password("9000000004", "password123"), "+919000000004")  # password is right ...
        self.assertEqual(a.account_status("+919000000004"), "pending")                  # ... but not approved yet
        self.assertEqual(a.account_status("owner"), "active")
        self.assertIsNone(a.account_status("nobody"))
        self.assertFalse(a.approve("+910000000000"))

    def test_optional_email_lets_mobile_users_reset(self):
        a = self.auth
        self.assertIn("email", a.phone_signup("Q", "9000000005", "password123", "join-me", contact_email="bad@")[1])
        self.assertIsNone(a.phone_signup("Q", "9000000005", "password123", "join-me", contact_email="Q@Example.com")[1])
        a.approve("+919000000005")
        self.assertIsNone(a.forgot_start("q@example.com")[1])
        self.assertEqual(self.outbox[-1][0], "q@example.com")
        self.assertEqual(a.forgot_verify("q@example.com", self.last_code(), "new-password-9"), ("+919000000005", None))
        self.assertEqual(a.check_password("9000000005", "new-password-9"), "+919000000005")


class TestPerUserData(unittest.TestCase):
    """Each person has their own settings, watchlist, alerts and paper account."""

    def setUp(self):
        db.db()
        for t in ("user_settings", "user_watchlist", "alerts", "paper_orders", "paper_positions", "journal"):
            db.x("DELETE FROM %s" % t)

    def test_settings_are_separate(self):
        with db.as_owner("+911111111111"):
            db.save_settings({"capital": 50000.0, "cash": 50000.0})
            self.assertEqual(db.get_settings()["capital"], 50000.0)
        with db.as_owner("+922222222222"):
            self.assertEqual(db.get_settings()["capital"], 100000.0)  # defaults for a new person
        self.assertEqual(db.owner(), "")  # context restored

    def test_alerts_and_paper_are_separate(self):
        from backend import alerts, paper
        for who, sym in (("a@x.com", "INFY"), ("b@x.com", "TCS")):
            with db.as_owner(who):
                db.x("INSERT INTO user_watchlist(owner, symbol, added_at) VALUES(?,?,?)", (who, sym, 1.0))
                alerts.notify(sym, "test", "hello " + sym)
                alerts.notify(sym, "test", "hello again " + sym)  # rings once per person
                db.x("INSERT INTO paper_positions(order_id, symbol, qty, entry_price, stop, mode, opened_at, owner) "
                     "VALUES(NULL,?,?,?,?,?,?,?)", (sym, 1, 100.0, 90.0, "swing", 1.0, who))
        self.assertEqual(alerts.watchers(), {"a@x.com": {"INFY"}, "b@x.com": {"TCS"}})
        with db.as_owner("a@x.com"):
            self.assertEqual([r["symbol"] for r in alerts.recent()], ["INFY"])
            alerts.mark_seen()
        with db.as_owner("b@x.com"):
            self.assertEqual([r["seen"] for r in alerts.recent()], [0])  # A's "mark seen" didn't touch B
            self.assertEqual(paper._today_state()[1][0]["symbol"], "TCS")
        self.assertEqual(sorted(paper.active_owners()), ["a@x.com", "b@x.com"])

    def test_old_single_user_data_moves_to_local_owner(self):
        db.x("DELETE FROM settings")
        db.x("INSERT INTO settings(key, value) VALUES('capital', '77777.0')")
        db._migrate(db.db())
        self.assertEqual(db.get_settings("")["capital"], 77777.0)
        db._migrate(db.db())  # running again changes nothing
        self.assertEqual(len(db.q("SELECT * FROM user_settings WHERE owner='' AND key='capital'")), 1)


class TestPostgresTranslation(unittest.TestCase):
    """The SQL used with SQLite is translated for Postgres (Neon) when DATABASE_URL is set."""

    def test_schema_types(self):
        s = db.pg_schema()
        self.assertNotIn("AUTOINCREMENT", s)
        self.assertEqual(s.count("BIGSERIAL PRIMARY KEY"), db.SCHEMA.count("AUTOINCREMENT"))
        self.assertNotIn(" REAL", s)  # REAL is 4-byte in Postgres: timestamps would lose seconds
        self.assertIn("ts DOUBLE PRECISION NOT NULL", s)

    def test_placeholders_and_ids(self):
        self.assertEqual(db.pg_sql("SELECT * FROM a WHERE x=? AND y LIKE '5%'"),
                         "SELECT * FROM a WHERE x=%s AND y LIKE '5%%'")
        self.assertTrue(db._returns_id("INSERT INTO paper_orders(ts) VALUES(?)"))
        self.assertTrue(db._returns_id("  insert into alerts(ts) values(?)"))
        self.assertFalse(db._returns_id("INSERT INTO users(username) VALUES(?)"))
        self.assertFalse(db._returns_id("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=1"))
        self.assertFalse(db._returns_id("UPDATE alerts SET seen=1"))

    def test_sqlite_still_works_for_upsert_and_batch(self):
        db.db()
        db.x("DELETE FROM watchlist")
        sql = ("INSERT INTO watchlist(symbol, added_at, note, mode) VALUES(?,?,?,?) "
               "ON CONFLICT(symbol) DO UPDATE SET added_at=excluded.added_at, note=excluded.note, mode=excluded.mode")
        db.x(sql, ("INFY", 1.0, "a", "swing"))
        db.x(sql, ("INFY", 2.0, "b", "intraday"))
        rows = db.q("SELECT * FROM watchlist")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["note"], rows[0]["mode"]), ("b", "intraday"))
        db.x("DELETE FROM signals")
        db.xmany("INSERT INTO signals(ts, symbol, mode, rating) VALUES(?,?,?,?)",
                 [(1.0, "A", "swing", "WAIT"), (2.0, "B", "swing", "WAIT")])
        self.assertEqual(len(db.q("SELECT * FROM signals")), 2)
        db.xmany("INSERT INTO signals(ts, symbol, mode, rating) VALUES(?,?,?,?)", [])  # no-op


class TestSignInWithAddedEmail(TestAuth):
    def test_mobile_account_signs_in_with_its_email(self):
        a = self.auth
        a.phone_signup("Arti", "92342 37102", "secret-pass-1", "join-me", contact_email="Arti@Example.com")
        a.approve("+919234237102")
        self.assertEqual(a.check_password("arti@example.com", "secret-pass-1"), "+919234237102")
        self.assertEqual(a.check_password(" ARTI@example.com ", "secret-pass-1"), "+919234237102")
        self.assertIsNone(a.check_password("arti@example.com", "wrong-pass"))
        self.assertEqual(a.check_password("9234237102", "secret-pass-1"), "+919234237102")
        self.assertIsNotNone(a.get_account("+919234237102")["last_login"])


class TestExitCheck(unittest.TestCase):
    """HOLD / TIGHTEN STOP / BOOK PARTIAL / EXIT on synthetic charts (no network)."""

    def setUp(self):
        from backend.analysis import exitcheck
        self.ec = exitcheck
        self.now = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=cal.IST)  # long after the synthetic bars

    def pos(self, bars, back=30, stop_pct=0.93, t1=None, t2=None, qty=10, mode="swing"):
        entry = bars[-back]["c"]
        return {"entry_price": entry, "stop": round(entry * stop_pct, 2), "target1": t1, "target2": t2, "qty": qty,
                "mode": mode, "opened_at": bars[-back]["t"]}

    def run_check(self, bars, pos, price=None, orig_stop=None):
        return self.ec.evaluate(pos, bars[-1]["c"] if price is None else price, daily_chart={"bars": bars},
                                settings=dict(db.DEFAULT_SETTINGS), orig_stop=orig_stop, now=self.now)

    def test_uptrend_trails_the_stop(self):
        bars = daily_bars(300, drift=0.004, vol=0.008, seed=3)
        pos = self.pos(bars, back=40)
        r = self.run_check(bars, pos)
        self.assertGreater(bars[-1]["c"], pos["entry_price"])
        self.assertEqual(r["verdict"], "TIGHTEN STOP")
        self.assertGreater(r["suggested_stop"], pos["stop"])
        self.assertLess(r["suggested_stop"], bars[-1]["c"])
        self.assertGreater(r["r_multiple"], 1)
        self.assertEqual(r["sessions_held"], 40)

    def test_stop_hit_is_exit(self):
        bars = daily_bars(300, drift=0.002, seed=4)
        pos = self.pos(bars)
        r = self.run_check(bars, pos, price=pos["stop"] - 1)
        self.assertEqual(r["verdict"], "EXIT")
        self.assertTrue(any("at or below your stop" in t for _, t in r["reasons"]))

    def test_targets(self):
        bars = daily_bars(300, drift=0.002, seed=5)
        px = bars[-1]["c"]
        pos = self.pos(bars, t1=px * 0.99, t2=px * 1.2)
        self.assertEqual(self.run_check(bars, pos)["verdict"], "BOOK PARTIAL")
        pos = self.pos(bars, t1=px * 0.9, t2=px * 0.99)
        self.assertEqual(self.run_check(bars, pos)["verdict"], "EXIT")
        pos = self.pos(bars, t1=px * 0.99, qty=1)  # one share: nothing to split
        self.assertNotEqual(self.run_check(bars, pos)["verdict"], "BOOK PARTIAL")

    def test_broken_trend_while_losing_is_exit(self):
        up = daily_bars(260, drift=0.003, seed=6)
        down = daily_bars(40, start=up[-1]["c"], drift=-0.006, vol=0.006, seed=7)
        for i, b in enumerate(down):
            b["t"] = up[-1]["t"] + (i + 1) * 86400
        bars = up + down
        pos = self.pos(bars, back=30, stop_pct=0.6)  # wide stop, so the trend rule decides
        r = self.run_check(bars, pos)
        self.assertLess(bars[-1]["c"], pos["entry_price"])
        self.assertEqual(r["verdict"], "EXIT")
        self.assertTrue(any("EMA 50" in t for _, t in r["reasons"]))

    def test_short_history_and_no_price(self):
        bars = daily_bars(40, seed=8)
        r = self.run_check(bars, self.pos(bars, back=10))
        self.assertEqual(r["verdict"], "HOLD")
        self.assertTrue(any("Not enough daily history" in t for _, t in r["reasons"]))
        r = self.ec.evaluate(self.pos(bars, back=10), None, daily_chart={"bars": bars})
        self.assertIsNone(r["verdict"])

    def test_intraday_square_off(self):
        bars = daily_bars(5, seed=9)
        late = datetime.datetime(2026, 1, 2, 15, 5, tzinfo=cal.IST)
        pos = {"entry_price": 100.0, "stop": 98.0, "target1": None, "target2": None, "qty": 5, "mode": "intraday",
               "opened_at": int(datetime.datetime(2026, 1, 2, 10, 0, tzinfo=cal.IST).timestamp())}
        r = self.ec.evaluate(pos, 101.0, bars5=[], settings={"intraday_flat_by": "15:15"}, now=late)
        self.assertEqual(r["verdict"], "EXIT")
        self.assertTrue(any("Square-off" in t for _, t in r["reasons"]))
        early = datetime.datetime(2026, 1, 2, 11, 0, tzinfo=cal.IST)
        self.assertEqual(self.ec.evaluate(pos, 101.0, bars5=[], settings={"intraday_flat_by": "15:15"},
                                          now=early)["verdict"], "HOLD")

    def test_raise_stop_only_up_and_below_price(self):
        from backend import paper
        db.db()
        db.x("DELETE FROM paper_positions")
        with db.as_owner("t@x.com"):
            pid = db.x("INSERT INTO paper_positions(order_id, symbol, qty, entry_price, stop, mode, opened_at, owner) "
                       "VALUES(NULL,'INFY',10,100,90,'swing',1,'t@x.com')")
            saved = (paper.yahoo.chart, paper.yahoo.quote_from_chart)
            paper.yahoo.chart = lambda *a, **k: {}
            paper.yahoo.quote_from_chart = lambda ch: {"price": 110.0}
            try:
                self.assertIn("only be raised", paper.raise_stop(pid, 85)["error"])
                self.assertIn("below the current price", paper.raise_stop(pid, 111)["error"])
                self.assertEqual(paper.raise_stop(pid, 101.5), {"ok": True, "stop": 101.5})
            finally:
                paper.yahoo.chart, paper.yahoo.quote_from_chart = saved
        with db.as_owner("someone-else"):
            self.assertIn("No open position", paper.raise_stop(pid, 105)["error"])  # not your position


class TestAdminAccuracy(unittest.TestCase):
    """Accuracy maths for the admin panel (synthetic data, no network)."""

    def test_segment_scoring(self):
        from backend import admin
        seg = admin._Segment("x", "y")
        for r, ret in [("BULLISH", 0.02), ("BULLISH", -0.01), ("STRONG BULLISH", 0.03), ("BEARISH", -0.02),
                       ("AVOID", 0.01), ("WAIT", 0.05), ("HIGH RISK", -0.03)]:
            seg.add(r, ret, 0.0)
        out = seg.out()
        self.assertEqual(out["observations"], 7)
        self.assertEqual(out["direction"]["calls"], 5)           # WAIT / HIGH RISK make no direction call
        self.assertEqual(out["direction"]["hit_pct"], 60.0)      # 2 bullish up + 1 bearish down of 5
        self.assertAlmostEqual(out["base_up_pct"], 57.1)         # 4 of 7 went up
        self.assertAlmostEqual(out["bullish_hit_pct"], 66.7)     # 2 of 3 bullish calls
        self.assertAlmostEqual(out["bullish_edge_pct"], 9.6)
        rows = {r["rating"]: r for r in out["rows"]}
        self.assertIsNone(rows["WAIT"]["hit_pct"])
        self.assertFalse(rows["BULLISH"]["enough"])               # below MIN_SAMPLE

    def test_replay_runs_on_history(self):
        from backend import admin
        bars = daily_bars(560, drift=0.001, seed=11)
        bench = {"bars": daily_bars(560, drift=0.0005, seed=12)}
        rep = admin.replay(["AAA"], bench, {"AAA": {"bars": bars}}, days=60)
        self.assertEqual(rep["stocks"], 1)
        self.assertEqual([s["name"] for s in rep["segments"]],
                         ["Swing · 5 trading days", "Swing · 10 trading days", "Swing · 20 trading days"])
        self.assertGreater(rep["segments"][0]["observations"], 50)
        self.assertEqual(admin.replay(["BBB"], bench, {"BBB": {"bars": bars[:100]}})["stocks"], 0)  # too short

    def test_live_tracking_uses_first_call_per_day(self):
        from backend import admin
        db.db()
        db.x("DELETE FROM signals")
        bars = daily_bars(80, drift=0.002, seed=13)
        d0 = bars[40]
        for k in range(3):  # the scan repeats; only the first call of the day counts
            db.x("INSERT INTO signals(ts, symbol, mode, rating, price) VALUES(?,?,?,?,?)",
                 (d0["t"] + 60 * k, "AAA", "swing", "BULLISH" if k == 0 else "BEARISH", d0["c"]))
        db.x("INSERT INTO signals(ts, symbol, mode, rating, price) VALUES(?,?,?,?,?)",
             (bars[-1]["t"], "AAA", "swing", "BULLISH", bars[-1]["c"]))  # too recent: still pending
        res = admin.live({"AAA": {"bars": bars}}, {"bars": daily_bars(80, seed=14)})
        self.assertEqual(res["calls"], 2)
        self.assertEqual(res["pending"], 1)
        swing5 = [s for s in res["segments"] if s["name"].startswith("Swing · 5")][0]
        self.assertEqual(swing5["observations"], 1)
        self.assertEqual(swing5["rows"][0]["rating"], "BULLISH")
        expect_up = bars[45]["c"] > d0["c"]
        self.assertEqual(swing5["direction"]["hit_pct"], 100.0 if expect_up else 0.0)


class TestPredictionOutcomes(unittest.TestCase):
    """Did each prediction come true? Target before stop, direction after N days, intraday by the close."""

    def setUp(self):
        from backend import admin
        self.admin = admin
        self.t0 = int(datetime.datetime(2026, 3, 2, 15, 0, tzinfo=cal.IST).timestamp())  # a Monday afternoon

    def days(self, highs_lows_closes):
        """Daily bars on the sessions AFTER the call (call day first)."""
        out = [{"t": self.t0 - 5 * 3600, "o": 100, "h": 101, "l": 99, "c": 100, "v": 1}]
        for i, (h, l, c) in enumerate(highs_lows_closes):
            out.append({"t": self.t0 - 5 * 3600 + (i + 1) * 86400, "o": c, "h": h, "l": l, "c": c, "v": 1})
        return out

    def call(self, rating="BULLISH", target=110.0, stop=95.0, mode="swing", ts=None):
        return {"ts": ts or self.t0, "symbol": "AAA", "mode": mode, "rating": rating, "price": 100.0,
                "target1": target, "stop": stop}

    def test_target_first_is_met(self):
        r = self.admin.judge(self.call(), self.days([(104, 99, 103), (111, 102, 109)]))
        self.assertEqual((r["outcome"], r["days"], r["kind"]), ("met", 2, "target/stop"))
        self.assertIn("Target", r["text"])

    def test_stop_first_is_missed_and_same_day_counts_stop(self):
        self.assertEqual(self.admin.judge(self.call(), self.days([(102, 94, 96)]))["outcome"], "missed")
        self.assertEqual(self.admin.judge(self.call(), self.days([(112, 94, 100)]))["outcome"], "missed")

    def test_open_then_expired(self):
        r = self.admin.judge(self.call(), self.days([(103, 98, 101)] * 3))
        self.assertEqual((r["outcome"], r["days"]), ("open", 3))
        r = self.admin.judge(self.call(), self.days([(103, 98, 102)] * 25))
        self.assertEqual((r["outcome"], r["days"]), ("expired", 20))
        self.assertEqual(r["move_pct"], 2.0)

    def test_direction_calls(self):
        no_plan = self.call(target=None, stop=None)
        self.assertEqual(self.admin.judge(no_plan, self.days([(101, 99, 100)] * 4 + [(104, 100, 103)]))["outcome"], "met")
        bear = self.call(rating="AVOID")
        r = self.admin.judge(bear, self.days([(101, 95, 96)] * 5))
        self.assertEqual((r["outcome"], r["kind"]), ("met", "direction"))
        self.assertEqual(self.admin.judge(bear, self.days([(105, 99, 104)] * 5))["outcome"], "missed")
        self.assertEqual(self.admin.judge(self.call(rating="WAIT"), self.days([]))["outcome"], "none")

    def test_intraday_uses_bars_after_the_call(self):
        ts = int(datetime.datetime(2026, 3, 2, 10, 0, tzinfo=cal.IST).timestamp())
        five = [{"t": ts - 600, "o": 100, "h": 120, "l": 80, "c": 100, "v": 1},   # before the call: ignored
                {"t": ts + 300, "o": 100, "h": 101, "l": 99.5, "c": 100.5, "v": 1},
                {"t": ts + 600, "o": 100.5, "h": 102.2, "l": 100, "c": 102, "v": 1}]
        c = self.call(mode="intraday", target=102.0, stop=98.0, ts=ts)
        r = self.admin.judge(c, None, five)
        self.assertEqual(r["outcome"], "met")
        self.assertIn("10:10", r["text"])
        c = self.call(mode="intraday", target=105.0, stop=98.0, ts=ts)
        self.assertEqual(self.admin.judge(c, None, five)["outcome"], "expired")  # a past day, neither hit

    def test_summary_and_saved_plans(self):
        items = [{"segment": "swing", "rating": "BULLISH", "outcome": o} for o in ("met", "met", "missed", "open")]
        items.append({"segment": "swing", "rating": "WAIT", "outcome": "none"})
        s = self.admin.summarize(items)
        self.assertEqual((s["swing"]["met"], s["swing"]["finished"], s["swing"]["met_pct"]), (2, 3, 66.7))
        self.assertNotIn("swing|WAIT", s)
        db.db()
        db.x("DELETE FROM signals")
        db.x("INSERT INTO signals(ts, symbol, mode, rating, price, entry, stop, target1, target2) "
             "VALUES(?,?,?,?,?,?,?,?,?)", (self.t0, "AAA", "swing", "BULLISH", 100, 100, 95, 110, 115))
        db.x("INSERT INTO signals(ts, symbol, mode, rating, price) VALUES(?,?,?,?,?)",
             (self.t0 + 600, "AAA", "swing", "BEARISH", 101))  # later the same day: not the first call
        first = list(self.admin._first_calls().values())
        self.assertEqual(len(first), 1)
        self.assertEqual((first[0]["rating"], first[0]["target1"]), ("BULLISH", 110))

    def test_breakout_plans_wait_for_their_entry(self):
        c = self.call(target=110.0, stop=95.0)
        c["entry"] = 104.0
        # dips to 94 BEFORE the entry triggers: not a miss; then triggers at 104 and reaches 110
        r = self.admin.judge(c, self.days([(102, 94, 100), (105, 101, 104), (111, 103, 110)]))
        self.assertEqual((r["outcome"], r["days"]), ("met", 3))
        r = self.admin.judge(c, self.days([(102, 99, 101)] * 4))
        self.assertEqual(r["outcome"], "open")
        self.assertIn("for entry", r["text"])
        r = self.admin.judge(c, self.days([(102, 99, 101)] * 22))
        self.assertEqual(r["outcome"], "not_triggered")
        s = self.admin.summarize([dict(r, segment="swing")])
        self.assertEqual((s["swing"]["not_triggered"], s["swing"]["finished"]), (1, 0))

    def test_intraday_calls_outside_market_hours_are_not_predictions(self):
        sat = int(datetime.datetime(2026, 3, 7, 11, 0, tzinfo=cal.IST).timestamp())     # Saturday
        evening = int(datetime.datetime(2026, 3, 2, 18, 0, tzinfo=cal.IST).timestamp())  # Monday after close
        for ts in (sat, evening):
            r = self.admin.judge(self.call(mode="intraday", ts=ts), None, [])
            self.assertEqual(r["outcome"], "none")


class TestResearchLab(unittest.TestCase):
    """Official data parsing, split adjustment, no look-ahead, weekly 10, immutable storage, evaluation."""

    def setUp(self):
        from backend.research import data, evaluate, features, model, store
        self.data, self.ev, self.fx, self.model, self.store = data, evaluate, features, model, store
        db.db()
        for t in ("daily_prices", "cohorts", "predictions", "outcomes", "data_runs", "model_versions"):
            db.x("DELETE FROM %s" % t)

    def seed(self, n_stocks=14, n_days=300, sectors=5):
        """Synthetic official-style history for n stocks plus NIFTY, on real trading days."""
        days = self.data.trading_days(datetime.date(2025, 1, 1), datetime.date(2026, 6, 30))[:n_days]
        rows = []
        bench = daily_bars(len(days), drift=0.0004, vol=0.009, seed=99)
        for i, d in enumerate(days):
            b = bench[i]
            rows.append({"symbol": "^NSEI", "date": d.isoformat(), "open": b["o"], "high": b["h"], "low": b["l"],
                         "close": b["c"], "volume": 0, "source": self.data.SOURCE_YAHOO})
        meta = {}
        for k in range(n_stocks):
            sym = "S%02d" % k
            meta[sym] = {"symbol": sym, "name": "Stock %d" % k, "sector": "Sector %d" % (k % sectors)}
            bars = daily_bars(len(days), start=100 + 10 * k, drift=0.0005 * (k % 4), vol=0.012, seed=200 + k)
            for i, d in enumerate(days):
                b = bars[i]
                rows.append({"symbol": sym, "date": d.isoformat(), "open": b["o"], "high": b["h"], "low": b["l"],
                             "close": b["c"], "prev_close": bars[i - 1]["c"] if i else None, "volume": 2e6,
                             "deliv_qty": 1e6, "deliv_pct": 50.0, "source": self.data.SOURCE_BHAV})
        self.data.save_prices([dict(r, prev_close=r.get("prev_close"), deliv_qty=r.get("deliv_qty"),
                                    deliv_pct=r.get("deliv_pct")) for r in rows])
        return days, meta

    def test_parse_official_file(self):
        raw = (b"SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, LAST_PRICE, CLOSE_PRICE, "
               b"AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS, NO_OF_TRADES, DELIV_QTY, DELIV_PER\n"
               b"INFY, EQ, 13-Nov-2026, 1150.00, 1151.00, 1160.50, 1139.00, 1141.00, 1141.50, 1149.2, 5000000, "
               b"57460.1, 90000, 2600000, 52.00\n"
               b"INFY, BL, 13-Nov-2026, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, -, -\n"
               b"XYZ, EQ, 13-Nov-2026, 10, 10, 10, 10, 10, -, 10, 1, 1, 1, -, -\n")
        rows = self.data.parse_bhavcopy(raw)
        self.assertEqual(len(rows), 1)  # block-deal series and rows without a close are skipped
        r = rows[0]
        self.assertEqual((r["symbol"], r["date"], r["close"], r["deliv_pct"], r["volume"]),
                         ("INFY", "2026-11-13", 1141.5, 52.0, 5000000.0))

    def test_split_adjustment_and_gaps(self):
        rows = [{"date": "2026-03-02", "open": 1000, "high": 1010, "low": 990, "close": 1000, "prev_close": 990,
                 "volume": 100, "deliv_pct": 40},
                {"date": "2026-03-03", "open": 500, "high": 505, "low": 495, "close": 502, "prev_close": 500,
                 "volume": 200, "deliv_pct": 40}]  # 1:2 split: NSE reports the adjusted previous close 500
        adj = self.data.adjust(rows)
        self.assertAlmostEqual(adj[0]["c"], 500.0)
        self.assertAlmostEqual(adj[0]["v"], 200.0)
        self.assertAlmostEqual(adj[1]["c"], 502.0)
        gap = [dict(rows[0]), dict(rows[1], date="2026-03-05")]  # a missing day in between: no adjustment
        self.assertAlmostEqual(self.data.adjust(gap)[0]["c"], 1000.0)

    def test_no_look_ahead(self):
        days, meta = self.seed(n_stocks=2, n_days=260)
        st = self.fx.Store(None)
        cut = days[240]
        self.assertEqual(st.upto("S00", cut)[-1]["date"], cut.isoformat())
        self.assertTrue(all(b["date"] <= cut.isoformat() for b in st.upto("S00", cut)))
        self.assertEqual(st.after("S00", cut, 3)[0]["date"], days[241].isoformat())
        f = self.fx.compute(st, "S00", cut)
        self.assertEqual(f["as_of"], cut.isoformat())
        self.assertEqual(f["close"], st.upto("S00", cut)[-1]["c"])
        self.assertIsNone(self.fx.compute(st, "S00", days[100]))  # not enough history yet

    def test_weekly_ten_is_saved_once_and_tamper_evident(self):
        from backend.research import jobs
        days, meta = self.seed()
        st = self.fx.Store(None)
        as_of = days[260]
        week = self.ev.week_of(as_of + datetime.timedelta(days=3))
        cid, made = jobs.make_cohort(st, meta, week, as_of, "simulated")
        self.assertTrue(made)
        preds = self.store.predictions(cohort_id=cid)
        self.assertEqual(len(preds), 10)
        sectors = [p["forecast"]["sector"] for p in preds]
        self.assertTrue(all(sectors.count(x) <= 2 for x in sectors))  # at most two per sector
        fc = preds[0]["forecast"]
        for h in ("1", "3", "5", "7"):
            r = fc["ranges"][h]
            self.assertLess(r["low"], r["mid"])
            self.assertLess(r["mid"], r["high"])
        self.assertGreater(fc["ranges"]["7"]["high"] - fc["ranges"]["7"]["low"],
                           fc["ranges"]["1"]["high"] - fc["ranges"]["1"]["low"])  # wider further out
        self.assertIn(fc["direction"], ("Bullish", "Bearish", "Sideways"))
        self.assertTrue(fc["invalidation"])
        cid2, made2 = jobs.make_cohort(st, meta, week, as_of, "simulated")
        self.assertEqual((cid2, made2), (cid, False))  # never made twice, never overwritten
        self.assertEqual(self.store.verify(), (10, []))
        db.x("UPDATE predictions SET payload=? WHERE id=?", ("{}", preds[0]["id"]))  # simulate tampering
        self.assertEqual(self.store.verify()[1], [preds[0]["id"]])

    def test_evaluation_against_actuals(self):
        from backend.research import jobs
        days, meta = self.seed()
        st = self.fx.Store(None)
        as_of = days[260]
        cid, _ = jobs.make_cohort(st, meta, self.ev.week_of(as_of), as_of, "simulated")
        saved = self.ev.evaluate_pending(st)
        self.assertEqual(saved, 40)  # 10 stocks x 4 horizons, all in the past
        self.assertEqual(self.ev.evaluate_pending(st), 0)  # insert-once
        o = db.q("SELECT * FROM outcomes WHERE horizon=7", one=False)
        p = self.store.predictions(cohort_id=cid)[0]
        mine = [x for x in o if x["prediction_id"] == p["id"]][0]
        actual = st.after(p["symbol"], p["as_of"], 7)[-1]
        self.assertEqual(mine["eval_date"], actual["date"])
        self.assertAlmostEqual(mine["actual_close"], round(actual["c"], 2))
        rng = p["forecast"]["ranges"]["7"]
        self.assertEqual(mine["in_range"], int(rng["low"] <= actual["c"] <= rng["high"]))
        s = self.ev.summary("simulated", 7)
        self.assertEqual((s["predictions"], s["evaluated"], s["pending"]), (10, 10, 0))
        self.assertEqual(s["correct"] + s["incorrect"], 10)
        self.assertIn("coverage", s["overall"])
        v = s["vs_nifty"]
        self.assertEqual(v["n"], sum(1 for r in db.q("SELECT * FROM outcomes WHERE horizon=7")
                                     if r["predicted_dir"] != "Sideways"))
        rows7 = [r for r in db.q("SELECT * FROM outcomes WHERE horizon=7") if r["predicted_dir"] != "Sideways"]
        expect = sum((1 if r["predicted_dir"] == "Bullish" else -1) * (r["actual_ret_pct"] - r["nifty_ret_pct"])
                     for r in rows7) / len(rows7)  # bearish calls "beat" NIFTY by falling more than it
        self.assertAlmostEqual(v["avg_excess_pct"], expect, places=2)
        self.assertEqual(self.ev.summary("live", 7)["overall"], {"n": 0})  # live and simulated never mix
        self.assertTrue(all(len(x["label"]) for x in s["indicators"]))
        detail = self.ev.cohort_detail(st, cid)
        self.assertEqual(len(detail["items"]), 10)
        self.assertEqual(len(detail["items"][0]["outcomes"]), 4)

    def test_weekly_job_refuses_stale_data(self):
        from backend.research import jobs
        self.seed(n_stocks=3, n_days=260)
        with self.assertRaises(RuntimeError) as e:
            jobs.weekly(today=datetime.date(2026, 11, 16))  # data ends long before this Monday
        self.assertIn("isn't available yet", str(e.exception))
