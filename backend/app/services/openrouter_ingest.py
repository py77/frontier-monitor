"""OpenRouter model-pricing ingest → inference cost trajectory.

Tracks blended $/MTok for a curated frontier-class model list. The median across
that list becomes the "frontier inference price" — the load-bearing input for
the Inference Cost dimension of the scoreboard.
"""
import logging
import re
from datetime import datetime, timezone
from statistics import median

import httpx
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session
from app.models import Source, TimeseriesPoint

logger = logging.getLogger(__name__)


# Frontier-class models worth tracking. Names match OpenRouter `id` exactly
# (case-insensitive). New flagship releases get added here.
FRONTIER_MODELS = [
    "anthropic/claude-sonnet-4.5",
    "anthropic/claude-opus-4",
    "openai/gpt-5",
    "openai/gpt-4o",
    "openai/o1",
    "google/gemini-2.5-pro",
    "google/gemini-pro-1.5",
    "meta-llama/llama-3.1-405b-instruct",
    "x-ai/grok-4",
    "deepseek/deepseek-chat",
]


def _safe_series(model_id: str) -> str:
    """Convert 'anthropic/claude-sonnet-4.5' → 'oprt_anthropic_claude_sonnet_4_5'."""
    return "oprt_" + re.sub(r"[^a-zA-Z0-9]+", "_", model_id).strip("_").lower()


def _blended_price(pricing: dict) -> float | None:
    """Typical chat workload: 40% prompt + 60% completion. Returns $/MTok."""
    try:
        prompt = float(pricing.get("prompt", 0) or 0) * 1_000_000
        comp = float(pricing.get("completion", 0) or 0) * 1_000_000
    except (TypeError, ValueError):
        return None
    if prompt <= 0 and comp <= 0:
        return None
    return 0.4 * prompt + 0.6 * comp


async def ingest_source(source_id: str) -> dict:
    async with async_session() as db:
        src = await db.get(Source, source_id)
        if not src:
            return {"source_id": source_id, "status": "missing"}
        if not src.enabled:
            return {"source_id": source_id, "status": "disabled"}

        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=True,
                                         headers={"User-Agent": "Frontier-Monitor/1.0"}) as c:
                resp = await c.get(src.url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.error("OpenRouter fetch failed: %s", e)
            return {"source_id": source_id, "status": "fetch_error", "error": str(e)}

        models_by_id = {m.get("id", "").lower(): m for m in data.get("data", [])}
        now = datetime.now(timezone.utc)
        per_model = []
        prices: list[float] = []

        for fid in FRONTIER_MODELS:
            m = models_by_id.get(fid.lower())
            if not m:
                per_model.append({"model": fid, "status": "not_found"})
                continue
            price = _blended_price(m.get("pricing") or {})
            if price is None:
                per_model.append({"model": fid, "status": "no_price"})
                continue

            stmt = pg_insert(TimeseriesPoint).values(
                series=_safe_series(fid), ts=now, value=price,
                meta={"model": fid, "name": m.get("name"), "source_id": source_id},
            ).on_conflict_do_update(
                index_elements=["series", "ts"],
                set_={"value": price, "meta": {"model": fid, "name": m.get("name"), "source_id": source_id}},
            )
            await db.execute(stmt)
            prices.append(price)
            per_model.append({"model": fid, "blended_per_mtok": round(price, 2)})

        # Aggregates: median (typical) and minimum (cheapest frontier-class option)
        if prices:
            for series_id, value in [
                ("oprt_frontier_median", median(prices)),
                ("oprt_frontier_min", min(prices)),
            ]:
                stmt = pg_insert(TimeseriesPoint).values(
                    series=series_id, ts=now, value=value,
                    meta={"source_id": source_id, "n_models": len(prices)},
                ).on_conflict_do_update(
                    index_elements=["series", "ts"],
                    set_={"value": value, "meta": {"source_id": source_id, "n_models": len(prices)}},
                )
                await db.execute(stmt)

        src.last_fetched_at = now
        await db.commit()
        logger.info("openrouter_ingest: tracked=%d median=$%.2f/MTok", len(prices),
                    median(prices) if prices else 0)
        return {
            "source_id": source_id,
            "status": "ok",
            "tracked": len(prices),
            "median_per_mtok": round(median(prices), 2) if prices else None,
            "min_per_mtok": round(min(prices), 2) if prices else None,
            "models": per_model,
        }
