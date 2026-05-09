"""Merchant AI-silicon revenue from a curated multi-ticker config file.

Source-of-truth lives in `config/merchant_ai_silicon.json`, populated by hand
from primary SEC filings: NVDA 10-Q segment revenue tables, AMD 8-K Ex99.1
segment summaries + 10-Q tables, AVGO earnings press releases that call out
'AI semiconductor revenue'. After each issuer's earnings release, append the
new quarter to the relevant ticker section and re-run the ingest.

Writes per-ticker quarterly revenue into the `timeseries` table — the
Infrastructure dimension reads `{ticker}_dc_revenue_quarterly` (NVDA, AMD)
and `avgo_ai_revenue_quarterly` (AVGO is AI-revenue, not DC-segment).

Why only NVDA + AMD + AVGO: GOOGL Cloud and AMZN AWS don't break out
TPU/Trainium revenue. AVGO designs the custom AI silicon + AI networking
for hyperscalers (notably Google TPU networking), making it the closest
available proxy for hyperscaler ASIC supply.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session
from app.models import Source, TimeseriesPoint

logger = logging.getLogger(__name__)

CONFIG_PATH = next(
    (p for p in [
        Path("/config/merchant_ai_silicon.json"),
        Path(__file__).resolve().parent.parent.parent.parent / "config" / "merchant_ai_silicon.json",
    ] if p.exists()),
    Path("/config/merchant_ai_silicon.json"),
)

# Per-ticker series naming. AVGO reports "AI semiconductor revenue" (not DC segment),
# so its series name reflects that to avoid future confusion.
SERIES_NAME = {
    "NVDA": "nvda_dc_revenue_quarterly",
    "AMD": "amd_dc_revenue_quarterly",
    "AVGO": "avgo_ai_revenue_quarterly",
}


def _quarter_to_dt(period: str) -> datetime:
    """'2025Q3' → end of that quarter (Mar 31, Jun 30, Sep 30, Dec 31)."""
    year = int(period[:4])
    q = int(period[5])
    month, day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[q]
    return datetime(year, month, day, tzinfo=timezone.utc)


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


async def ingest_source(source_id: str) -> dict:
    cfg = _load_config()
    tickers = [k for k in cfg.keys() if not k.startswith("_") and k in SERIES_NAME]
    if not tickers:
        return {"source_id": source_id, "status": "config_empty", "path": str(CONFIG_PATH)}

    async with async_session() as db:
        src = await db.get(Source, source_id)
        if not src:
            return {"source_id": source_id, "status": "missing"}
        if not src.enabled:
            return {"source_id": source_id, "status": "disabled"}

        now = datetime.now(timezone.utc)
        inserted = 0
        per_ticker_summary = []

        for tkr in tickers:
            quarters = cfg[tkr]
            series = SERIES_NAME[tkr]
            for q in quarters:
                ts = _quarter_to_dt(q["period"])
                stmt = pg_insert(TimeseriesPoint).values(
                    series=series,
                    ts=ts,
                    value=float(q["revenue_usd_m"]),
                    meta={
                        "ticker": tkr,
                        "period": q["period"],
                        "fiscal_label": q.get("fiscal_label"),
                        "source": q.get("source"),
                        "note": q.get("note"),
                    },
                ).on_conflict_do_update(
                    index_elements=["series", "ts"],
                    set_={"value": float(q["revenue_usd_m"])},
                )
                result = await db.execute(stmt)
                if result.rowcount:
                    inserted += 1

            latest = max(quarters, key=lambda x: x["period"])
            per_ticker_summary.append({
                "ticker": tkr,
                "latest_period": latest["period"],
                "latest_revenue_m": latest["revenue_usd_m"],
                "n_quarters": len(quarters),
            })

        src.last_fetched_at = now
        await db.commit()
        logger.info("merchant_ai_ingest: %d points inserted across %d tickers",
                    inserted, len(tickers))
        return {
            "source_id": source_id,
            "status": "ok",
            "tickers": per_ticker_summary,
            "config_path": str(CONFIG_PATH),
        }
