"""GPU rental-rate read surface — drives the /gpu page.

Reads the gpu_* timeseries written by services/gpu_rental_ingest.py and assembles a
per-canonical-model view: blended on-demand median, spot/on-demand ratio, available
inventory, WoW delta, a 30d sparkline (reusing score_engine.sparkline), and the latest
per-source breakdown. Read-only — the scheduler is the sole writer.
"""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import TimeseriesPoint
from app.services.gpu_rental_ingest import MODELS, SOURCE_VENUE, gpu_class
from app.services.score_engine import sparkline

router = APIRouter()


async def _latest(db, series: str) -> tuple[float, datetime] | None:
    row = (
        await db.execute(
            select(TimeseriesPoint.value, TimeseriesPoint.ts)
            .where(TimeseriesPoint.series == series)
            .order_by(desc(TimeseriesPoint.ts)).limit(1)
        )
    ).first()
    return (float(row.value), row.ts) if row else None


async def _wow(db, series: str, current: float) -> float | None:
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    row = (
        await db.execute(
            select(TimeseriesPoint.value)
            .where(TimeseriesPoint.series == series, TimeseriesPoint.ts <= week_ago)
            .order_by(desc(TimeseriesPoint.ts)).limit(1)
        )
    ).first()
    return (current - float(row[0])) if row else None


async def _latest_per_source_ondemand(db) -> dict[str, dict[str, float]]:
    """{canonical_model: {source_id: latest on-demand $/GPU/hr}} from per-source series."""
    rows = (
        await db.execute(
            select(TimeseriesPoint.series, TimeseriesPoint.value, TimeseriesPoint.meta)
            .where(TimeseriesPoint.series.like("gpu_%_ondemand"),
                   TimeseriesPoint.series.notlike("gpu_%_ondemand_median"))
            .order_by(TimeseriesPoint.series, desc(TimeseriesPoint.ts))
        )
    ).all()
    out: dict[str, dict[str, float]] = {}
    seen: set[str] = set()
    for r in rows:
        if r.series in seen:
            continue
        seen.add(r.series)
        meta = r.meta or {}
        src, model = meta.get("source"), meta.get("model")
        if src and model:
            out.setdefault(model, {})[src.removeprefix("gpu_")] = float(r.value)
    return out


@router.get("/gpu/rates")
async def gpu_rates(db: AsyncSession = Depends(get_db)) -> dict:
    """One call powers the /gpu page: per-model headline metrics + per-source breakdown."""
    # Discover which canonical models have a headline median series.
    median_series = (
        await db.execute(
            select(TimeseriesPoint.series).distinct()
            .where(TimeseriesPoint.series.like("gpu_%_ondemand_median"))
        )
    ).scalars().all()
    models = [s.removeprefix("gpu_").removesuffix("_ondemand_median") for s in median_series]

    per_source = await _latest_per_source_ondemand(db)
    out_models = []
    for key in sorted(set(models) | set(per_source)):
        med_series = f"gpu_{key}_ondemand_median"
        latest = await _latest(db, med_series)
        ondemand_median = latest[0] if latest else None
        ratio = await _latest(db, f"gpu_{key}_spot_ratio")
        avail = await _latest(db, f"gpu_{key}_avail_count")
        wow = await _wow(db, med_series, ondemand_median) if ondemand_median is not None else None
        srcs = per_source.get(key, {})
        out_models.append({
            "key": key,
            "display": MODELS.get(key, {}).get("display", key),
            "class": gpu_class(key),
            "ondemand_median": round(ondemand_median, 4) if ondemand_median is not None else None,
            "ondemand_delta_wow": round(wow, 4) if wow is not None else None,
            # consensus = cross-provider aggregator (ComputePrices); hyperscaler = Azure — both
            # shown for context but kept OUT of the marketplace blend (different price tiers).
            "consensus": round(srcs["computeprices"], 4) if "computeprices" in srcs else None,
            "hyperscaler": round(srcs["azure"], 4) if "azure" in srcs else None,
            "spot_ratio": round(ratio[0], 3) if ratio else None,
            "avail_count": int(avail[0]) if avail else None,
            "sources": srcs,
            "n_sources": len(srcs),
            "sparkline": await sparkline(med_series, days=30),
        })

    # Stable display order: datacenter first (priciest first), then consumer. Rank by an
    # EFFECTIVE price — marketplace blend, else consensus, else hyperscaler — so a GPU that
    # only has aggregator/hyperscaler data (e.g. brand-new B300, not on marketplaces yet)
    # still ranks by its real price instead of sinking to the bottom as $0.
    _class_rank = {"datacenter": 0, "consumer": 1, "unknown": 2}

    def _eff(m):
        return m["ondemand_median"] or m.get("consensus") or m.get("hyperscaler") or 0

    out_models.sort(key=lambda m: (_class_rank.get(m["class"], 2), -_eff(m)))

    return {
        "models": out_models,
        "venues": SOURCE_VENUE,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
