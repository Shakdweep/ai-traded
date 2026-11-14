"""Offline unit tests (synthetic data, no network).
Run:  .venv/Scripts/python -m unittest discover -s tests -v
"""

import datetime
import math
import os
import random
import tempfile
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
