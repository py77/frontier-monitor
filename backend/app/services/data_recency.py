"""Data-recency check for curated quarterly sources.

Answers the question the fetch-time "stale" badge cannot: do we hold the latest
quarter the issuers have *officially reported* by now? Liveness ("did the loader
run", = last_fetched_at) and recency ("is the data the newest reported", = this
module) are different signals — a source can be freshly fetched yet hold a quarter
that's a filing behind.

"behind" = a calendar quarter whose earnings are reliably public (quarter-end +
a per-kind reporting-lag cushion) is newer than the latest period in the config.
Only the curated quarterly kinds listed in _REPORT_LAG_DAYS get a check; everything
else returns None (no recency concept) and falls back to the liveness badge.
"""
from datetime import datetime, timezone

from app.services import capex_ingest

# Days after a calendar quarter-end by which every issuer a `kind` covers has
# reliably reported. Mega-caps file ~28-32 days after quarter-end; the cushion
# avoids a false "behind" during the gap between quarter-end and earnings. Tune
# per kind as coverage expands (merchant_ai issuers report later — NVDA/AVGO file
# a calendar quarter in late May / early June — so it would need a larger lag).
_REPORT_LAG_DAYS: dict[str, int] = {
    "capex": 40,  # MSFT/GOOGL/META/AMZN report ~late Apr / Jul / Oct / Jan
}

_Q_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


def _quarter_end(year: int, q: int) -> datetime:
    mo, day = _Q_END[q]
    return datetime(year, mo, day, tzinfo=timezone.utc)


def latest_reportable_quarter(today: datetime, lag_days: int) -> str:
    """Most recent calendar quarter ('YYYYQn') whose earnings are reliably public
    as of `today` (i.e. quarter-end + lag_days has passed)."""
    y = today.year
    q = (today.month - 1) // 3 + 1
    for _ in range(8):  # walk back at most two years
        if (today - _quarter_end(y, q)).days >= lag_days:
            return f"{y}Q{q}"
        q -= 1
        if q == 0:
            q, y = 4, y - 1
    return f"{y}Q{q}"


def _held_periods_by_ticker(kind: str) -> dict[str, str] | None:
    """Latest period present per ticker in the curated config for this kind, or
    None if the kind isn't a curated quarterly source."""
    if kind == "capex":
        cfg = capex_ingest._load_config()
        return {
            tkr: max(q["period"] for q in rows)
            for tkr, rows in cfg.items()
            if not tkr.startswith("_") and rows
        }
    return None


def recency_status(kind: str, today: datetime) -> dict | None:
    """For a curated quarterly source, return whether the config is behind the
    latest officially-reported quarter:
        {"behind": bool, "expected": "YYYYQn", "held": "YYYYQn", "missing": [tickers]}
    Returns None for kinds without a recency concept (period strings sort
    lexicographically, which is correct for the 'YYYYQn' format)."""
    lag = _REPORT_LAG_DAYS.get(kind)
    if lag is None:
        return None
    held = _held_periods_by_ticker(kind)
    if not held:
        return None
    expected = latest_reportable_quarter(today, lag)
    missing = sorted(tkr for tkr, p in held.items() if p < expected)
    return {
        "behind": bool(missing),
        "expected": expected,
        "held": min(held.values()),
        "missing": missing,
    }
