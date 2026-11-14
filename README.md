# AI Traded: personal trading intelligence for NSE / BSE

A locally run web app that scans Indian stocks every few minutes and answers, each
morning: what is the market doing, which stocks to investigate, which to avoid,
where the entry/stop/targets are, how much to put in, what the downside is, and
whether the setup has historically worked on that stock after Indian charges.
If nothing passes every check, it says **NO TRADE**.

**Analysis and paper trading only.** No real orders are ever sent. Prices come from
an unofficial, delayed source and are never labelled live. Ratings are transparent
rules, not predictions, and nothing here is investment advice.

---

## 👋 For testers: run it in 3 steps

1. Install **Python 3.10 or newer** from [python.org](https://www.python.org/downloads/) (on Windows, tick **"Add Python to PATH"**).
2. Click the green **Code** button above → **Download ZIP**, and unzip it.
3. **Windows:** double-click `run.bat`. **Mac/Linux:** open a terminal in the folder and run `sh run.sh`, then open http://127.0.0.1:8000.

The first start takes a minute (it installs two packages and runs the first scan). Everything runs on your own
computer; no account or API key is needed. Start on the **"What should I do today?"** tab. Please report anything
confusing or broken as a GitHub issue.

---

## Quick start

Windows: double-click **`run.bat`** (first run creates `.venv` and installs two packages),
then open http://127.0.0.1:8000.

Manually (any OS, Python 3.10+):

```bash
python -m venv .venv
```

```bash
.venv/Scripts/python -m pip install -r requirements.txt
```

```bash
.venv/Scripts/python -m backend
```

(macOS/Linux: `.venv/bin/python`, or run `./run.sh`.)

Optional settings live in `.env` (copy `.env.example`). The first NIFTY 100 scan
takes about 10 seconds; NIFTY 500 takes about a minute.

### Tests

```bash
.venv/Scripts/python -m unittest discover -s tests -t . -v
```

```bash
.venv/Scripts/python tests/smoke_live.py http://127.0.0.1:8000
```

The first runs 23 offline unit tests. The second hits a running server with real
data (status, overview, scan, swing and intraday deep dives, news, briefing,
watchlist, safety guards).

---

## What's in it

| Area | What it does |
| --- | --- |
| **What should I do today?** | Daily briefing: market verdict (Bullish / Bearish / Sideways from NIFTY vs EMA20/50 + breadth + VIX), a decision (TRADE SELECTIVELY / CAUTION / WAIT / NO TRADE), opportunities that pass every rule *and* a positive backtest, stocks to avoid, NIFTY/BANK NIFTY support and resistance, high-impact news, global cues, holidays. |
| **Scanner** | NIFTY 50 / Next 50 / 100 / 200 / 500, Bank Nifty, Midcap 100/150, Smallcap 100/250, or your watchlist. Swing (daily) or intraday (5-min). Filters: sector, market-cap bucket, price, volume, setup, rating. Click any column to sort. |
| **Setups** | 20-day breakout/breakdown, bull/bear flag, RSI oversold/overbought, oversold bounce, MACD crosses, EMA 20/50 and 50/200 crosses, VWAP breakout/breakdown, unusual volume, momentum, support/resistance breaks, trend reversals, approaching breakout; intraday: 15-min opening-range breakout/breakdown, VWAP reclaim/loss, 5-min volume spikes, intraday momentum. |
| **Rating engine** | STRONG BULLISH, BULLISH, WAIT, HIGH RISK, BEARISH, AVOID (or INSUFFICIENT DATA). Every point of the score is listed with its reason; the confidence formula is shown with its parts. |
| **Trade plans** | Entry range, stop, Target 1/2, reward:risk, holding period, quantity, capital, max loss incl. charges, confirmation needed, invalidation conditions. Long only. Marked actionable only if rating, R:R, sizing, data freshness *and* the backtest gate all pass. |
| **Backtesting** | Intraday: the skill's Time Machine (10,000 Monte Carlo replays + every past session). Swing: 5-year event study on daily bars. Both deduct Indian charges and slippage and report sample size, period, target/stop frequency, avg R, max drawdown. |
| **Fundamentals** | Revenue/profit growth, EPS, P/E, P/B, ROE, ROCE, D/E, margins, cash flow vs profit, promoter/FII/DII holding and changes, pledge (if disclosed), quarterly results, peer comparison. Strong/Average/Weak + valuation vs peer median P/E. Banks are handled separately. |
| **News** | NSE announcements and results, SEBI, RBI, ET/Business Standard/Mint markets, Google News per stock. Keyword sentiment (the deciding words are shown), impact level, affected stocks. Older than 72h is never shown as current. |
| **Modes** | Intraday (1/5/15-min charts, VWAP, ORB), Swing (daily/weekly, EMA 20/50/200), Long-term (fundamentals only). An intraday signal is never presented as an investment recommendation. |
| **Capital & risk** | Capital, cash, risk per trade, daily loss limit, trades/day, position cap, min R:R, slippage, brokerage, intraday entry window and square-off time. Position-size calculator. |
| **Paper trading** | Propose from any plan → risk gate → you type **yes** for that one order (checked by the skill's own yes-parser) → risk gate again → fill at the latest delayed quote + slippage only inside the entry range. Outside market hours a swing order queues for the next open. Exits: stop, T1 (half off, stop to break-even), T2, square-off, or manual. P&L after charges, equity curve, journal. |
| **Watchlist & alerts** | Buy zone, breakout confirmed, stop approaching, target reached, volume spike, company news, trend reversal. Each rings once per day; optional Telegram. |
| **Data status** | Every source, its freshness, failures and the last scans. |

---

## How the installed skills were reused

| Skill | Used for | How |
| --- | --- | --- |
| `trader-timemachine` | Intraday probability | **Vendored unchanged.** `build_pool`, `simulate`, `history_replay`, `verdict` run on NSE bars; only the session constants are set for NSE (75 five-minute bars, 09:15 open). |
| `trader-scanner` | Daily setups | **Vendored.** `find_setups()` (20-day breakout, bull flag, reclaim MA20, oversold, bounce) and `rsi()` are called directly; the rest are added beside them. |
| `trader-broker` | Paper-order approval | **Vendored.** `parse_yes()` decides whether your reply is an explicit yes for that exact order. |
| `trader-entry` / `trader-exit` | Intraday plan | Same rules re-implemented for IST: 15-min opening range, first completed bar closing above it, stop at the range midpoint, 2R target. |
| `trader-budget` / `trader-risk` | Sizing and risk gate | Same limits and "says which limit decided" logic, rewritten in INR for NSE cash equity (the originals hard-code USD, US tickers and Alpaca's order format). |
| `trader-alerts` | Alerts | Same "ring once" model, IST calendar, plus Telegram. |
| `trader-news` | News | Same approach (tag, dedupe, newest first, recent only) with Indian sources instead of Yahoo-US RSS and SEC 8-Ks. |
| `trader-analyst` | Fundamentals | Replaced: SEC EDGAR has no Indian companies; Screener.in pages are used instead. |
| `trader-pelosi` | – | Not applicable (US Congress disclosures). |

The vendored files and their MIT licence are in `backend/vendor/day_trader_skill/`.

---

## Architecture and why

```
frontend/  index.html · app.js · styles.css   vanilla JS + TradingView Lightweight Charts (CDN)
backend/
  main.py          FastAPI routes + background refresh loop
  services.py      scans, overview, deep dive, briefing, backtest gate
  market/          calendar.py (IST, NSE hours/holidays) · yahoo.py (prices + freshness) · universe.py (index lists)
  analysis/        indicators · engine (rating) · tradeplan · risk (sizing, charges, gate) · backtest · fundamentals
  news/feeds.py    RSS monitor + sentiment
  alerts.py        alert rules + Telegram
  paper.py         paper trading engine
  db.py            SQLite: settings, watchlist, signal history, alerts, paper orders/positions, journal
  vendor/          the reused skill code
data/              trader.db, cached index lists, last scans, holidays.json (created at run time)
```

- **Python + FastAPI**: the skills are standard-library Python, so the engine reuses them directly. FastAPI adds a typed JSON API and serves the page; only two dependencies.
- **SQLite**: watchlist, signal history, alerts, paper trades and journal in one file. Nothing to install.
- **No build step on the frontend**: one HTML/JS/CSS bundle, with TradingView's open-source Lightweight Charts for the financial charts.
- **Background thread**: news every 10 min, scans every 10 min while NSE is open (2 h when closed), intraday NIFTY 50 scan every 5 min while open, paper positions checked every minute.

---

## Data providers: what is used, what is not, and what you'd need

| Source | Used for | Status |
| --- | --- | --- |
| Yahoo Finance chart API (`.NS`, `^NSEI`, …) | Prices, OHLCV, intraday bars | **Unofficial, free, typically delayed.** Labelled DELAYED / STALE / CLOSED / UNAVAILABLE, never LIVE. Yahoo omits the 15:20 IST bar and sometimes lags the latest daily bar; both are patched and reported. |
| niftyindices.com constituent CSVs | Index membership, sectors | Official public files, cached daily. |
| NSE archives `EQUITY_L.csv` | Search across all NSE stocks | Official public file. |
| Screener.in company pages | Fundamentals, peers | Unofficial, cached 12h, one request per 1.5s. Allowed by its robots.txt. Disable with `TRADER_ENABLE_SCREENER=0`. |
| NSE / SEBI / RBI RSS, ET / BS / Mint RSS, Google News RSS | News | Public feeds. |
| **BSE announcements API** | – | **Not used**: it blocks non-browser clients. |
| **NSE JSON APIs** (live quotes, option chain) | – | **Not used**: they require browser cookies; working around that isn't appropriate. |

**For genuinely real-time, exchange-grade data you need a licensed broker API.**
None is wired in this version (the Data status page shows which keys are present,
and nothing is faked). What each needs:

- **Zerodha Kite Connect**: a Zerodha account, a Kite Connect developer app (paid subscription; check current pricing), the API key and secret, and a daily access token from the login flow (tokens expire every day).
- **Upstox API**: an Upstox account and a developer app (API key and secret), plus a daily OAuth access token.
- **Angel One SmartAPI**: an Angel One account, a SmartAPI key, your client code, and a session made with PIN + TOTP.

Adding one means writing a provider next to `market/yahoo.py` that returns the same
`{meta, bars}` shape. Freshness labels would then show LIVE for that feed only.

---

## Method details

**Score (≈ −100…+100)**: trend structure (price vs EMA 20/50/200, EMA stacking, ±35), momentum (RSI zone, MACD vs signal, histogram slope, ±20), volume (up/down day on ≥ 1.5× volume, ±10), setups (±20), 20-day relative strength vs NIFTY 50 (±10).
**Rating**: ≥ 55 with a confirming setup and no major risk flag → STRONG BULLISH; ≥ 25 → BULLISH, or HIGH RISK if volatility / overextension / illiquidity flags apply; ≤ −45 → AVOID; ≤ −20 → BEARISH (HIGH RISK if an oversold bounce is under way, AVOID if illiquid); otherwise WAIT. Weak fundamentals downgrade swing/long-term ratings.
**Confidence**: 50% evidence strength (|score|) + 30% indicator agreement + 20% data completeness. It measures consistency of evidence, **not** a probability of profit.
**Backtest gate**: a plan is actionable only if the same setup fired at least **15 times** on that stock in the test period and averaged at least **+0.10R after costs**.
**Charges (approximate; verify with your broker)**: brokerage min(₹20, 0.03%) per order, STT (0.025% sell intraday, 0.1% both sides delivery), exchange 0.00297%, SEBI 0.0001%, stamp duty (0.003% / 0.015% on buy), GST 18%, DP ₹15.93 per delivery sell, plus 0.05% slippage per fill.
**NSE holidays**: `data/holidays.json` ships with the 2026 list as best known. **It isn't verified against NSE's circular**, so check and edit it.

---

## Telegram alerts

1. Message **@BotFather** on Telegram, create a bot, copy its token.
2. Send your bot any message, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
3. Put both in `.env` as `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`, restart, and press **Send test** on the Watchlist & Alerts page.

---

## Deployment

- **Local (recommended)**: `run.bat`. The server listens on 127.0.0.1 only.
- **Docker**:

```bash
docker build -t ai-traded .
```

```bash
docker run -p 127.0.0.1:8000:8000 -v ai-traded-data:/data --env-file .env ai-traded
```

- **A server / VPS**: this app has **no login**. Don't expose it to the internet as is. Put it behind a reverse proxy with authentication (or a VPN / Tailscale) and keep `.env` on the server only.

---

## Safety rules built in

- Paper trading only; no broker order API exists in the code. Paper mode can't be switched off from the UI or API.
- Every proposed trade needs a stop-loss; long only; short selling, margin, leverage and options can't be enabled (the API refuses).
- Risk gate on propose **and** again at fill: max loss per trade, daily loss limit (realised + open risk), trades per day, R:R minimum, cash, position cap, intraday entry window.
- One explicit "yes" per order; "ok", "sure", "yes to all" or a yes naming a different stock are refused.
- Stale or unavailable prices block fills and are flagged on every plan.
- API keys are read from environment variables on the server and never sent to the browser.

## Known limitations

- Yahoo data is unofficial and delayed; intraday signals during market hours lag reality. Use a licensed feed before relying on intraday timing.
- Sentiment is a keyword lexicon, not language understanding.
- Fundamentals depend on Screener.in's page layout; if it changes, that panel shows "unavailable" rather than wrong numbers.
- Backtests use the stock's own history, so sample sizes are small (often 15–35 trades); they are a reality check, not a forecast.
- Most day traders lose money. This tool is meant to make that visible before you risk anything.
