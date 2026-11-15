"""Research platform: weekly 10-stock forecasts, immutable prediction history, daily
monitoring against official NSE closes, and model versions.

  data.py      official end-of-day data (NSE bhavcopy incl. delivery %), indices, bulk deals
  features.py  indicators for one stock on one date, using only data up to that date
  model.py     model versions; rules-v1 selection (10 stocks) and 1/3/5/7-day forecasts
  store.py     append-only prediction storage with a content hash (never edited)
  evaluate.py  predicted vs actual per horizon, accuracy dashboard metrics
  jobs.py      the scheduled jobs (GitHub Actions / "Run now"): daily, weekly, backfill, simulate
"""
