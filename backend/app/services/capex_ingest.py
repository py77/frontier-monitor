"""Hyperscaler quarterly capex from a curated config file.

Source-of-truth data lives in `config/hyperscaler_capex.json`, populated by
hand from MSFT/GOOGL/META/AMZN 8-K Ex99.1 cash flow statements. After each
earnings cycle, append a new quarter to that file and re-run the ingest.

Writes per-ticker quarterly + per-ticker TTM + aggregate-total TTM into the
`timeseries` table — the Hyperscaler $ dimension reads these.
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session
from app.models import Source, TimeseriesPoint

logger = logging.getLogger(__name__)

# Mounted at /config in the container (read-only volume); falls back to repo path for non-Docker runs.
CONFIG_PATH = next(
    (p for p in [
        Path("/config/hyperscaler_capex.json"),
        Path(__file__).resolve().parent.parent.parent.parent / "config" / "hyperscaler_capex.json",
    ] if p.exists()),
    Path("/config/hyperscaler_capex.json"),
)


def _quarter_to_dt(period: str) -> datetime:
    """'2026Q1' → end of that quarter (Mar 31, Jun 30, Sep 30, Dec 31)."""
    year = int(period[:4])
    q = int(period[5])
    month, day = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}[q]
    return datetime(year, month, day, tzinfo=timezone.utc)


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _ttm_series(quarters: list[dict]) -> list[tuple[str, float]]:
    """Compute trailing-4-quarter sum for each available quarter."""
    sorted_q = sorted(quarters, key=lambda x: x["period"])
    by_period = {q["period"]: q["capex_usd_m"] for q in sorted_q}
    out = []
    periods = [q["period"] for q in sorted_q]
    for i, p in enumerate(periods):
        if i < 3:
            continue  # need 4 quarters for TTM
        ttm = sum(by_period[periods[j]] for j in range(i - 3, i + 1))
        out.append((p, ttm))
    return out


async def ingest_source(source_id: str) -> dict:
    cfg = _load_config()
    tickers = [k for k in cfg.keys() if not k.startswith("_")]
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

        # Per-ticker quarterly + TTM
        for tkr in tickers:
            quarters = cfg[tkr]
            for q in quarters:
                ts = _quarter_to_dt(q["period"])
                stmt = pg_insert(TimeseriesPoint).values(
                    series=f"capex_{tkr}_quarterly",
                    ts=ts,
                    value=float(q["capex_usd_m"]),
                    meta={"period": q["period"], "source": q.get("source"), "fiscal_label": q.get("fiscal_label")},
                ).on_conflict_do_update(
                    index_elements=["series", "ts"],
                    set_={"value": float(q["capex_usd_m"])},
                )
                result = await db.execute(stmt)
                if result.rowcount:
                    inserted += 1

            for period, ttm_value in _ttm_series(quarters):
                ts = _quarter_to_dt(period)
                stmt = pg_insert(TimeseriesPoint).values(
                    series=f"capex_{tkr}_ttm",
                    ts=ts,
                    value=float(ttm_value),
                    meta={"period": period, "ticker": tkr},
                ).on_conflict_do_update(
                    index_elements=["series", "ts"],
                    set_={"value": float(ttm_value)},
                )
                await db.execute(stmt)

            latest = max(quarters, key=lambda x: x["period"])
            per_ticker_summary.append({
                "ticker": tkr,
                "latest_period": latest["period"],
                "latest_capex_m": latest["capex_usd_m"],
                "n_quarters": len(quarters),
            })

        # Aggregate quarterly across all 4 hyperscalers (only periods where ALL 4 reported)
        all_periods: dict[str, dict[str, float]] = {}
        for tkr in tickers:
            for q in cfg[tkr]:
                all_periods.setdefault(q["period"], {})[tkr] = q["capex_usd_m"]
        complete = {p: vals for p, vals in all_periods.items() if len(vals) == len(tickers)}
        for period, vals in complete.items():
            ts = _quarter_to_dt(period)
            total = sum(vals.values())
            stmt = pg_insert(TimeseriesPoint).values(
                series="capex_total_quarterly",
                ts=ts,
                value=float(total),
                meta={"period": period, "components": vals},
            ).on_conflict_do_update(
                index_elements=["series", "ts"],
                set_={"value": float(total), "meta": {"period": period, "components": vals}},
            )
            await db.execute(stmt)

        src.last_fetched_at = now
        await db.commit()
        logger.info("capex_ingest: %d points inserted across %d tickers (%d complete-quarter aggregates)",
                    inserted, len(tickers), len(complete))
        return {
            "source_id": source_id,
            "status": "ok",
            "tickers": per_ticker_summary,
            "complete_quarters": sorted(complete.keys()),
            "config_path": str(CONFIG_PATH),
        }
