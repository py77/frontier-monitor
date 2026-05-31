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
from app.models import Digest, TimeseriesPoint
from app.services.gpu_rental_ingest import MODELS, SOURCE_VENUE, gpu_class
from app.services.score_engine import sparkline

router = APIRouter()

# Reserved digest period for the GPU-market verdict — separate from the home page's
# composite `verdict` so the `/gpu` slash command and `/refresh` never clobber each other.
GPU_VERDICT_PERIOD = "gpu_verdict"


async def _latest_gpu_verdict(db) -> dict | None:
    """Latest narrative GPU-market verdict (written by the `/gpu` command), or None."""
    d = (
        await db.execute(
            select(Digest).where(Digest.period == GPU_VERDICT_PERIOD)
            .order_by(desc(Digest.created_at)).limit(1)
        )
    ).scalar_one_or_none()
    return {"markdown": d.markdown, "created_at": d.created_at.isoformat()} if d else None


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


async def _latest_per_source_ondemand(
    db,
) -> tuple[dict[str, dict[str, dict]], dict[str, datetime]]:
    """Latest on-demand price + its timestamp from each per-source series.

    Returns ``(by_model, src_updated)`` where
    ``by_model[canonical_model][source] = {"value": $/GPU/hr, "ts": datetime}`` and
    ``src_updated[source] = latest datetime seen for that source across all models``
    (so the page can show one explicit update date per dataset).
    """
    rows = (
        await db.execute(
            select(TimeseriesPoint.series, TimeseriesPoint.value,
                   TimeseriesPoint.ts, TimeseriesPoint.meta)
            .where(TimeseriesPoint.series.like("gpu_%_ondemand"),
                   TimeseriesPoint.series.notlike("gpu_%_ondemand_median"))
            .order_by(TimeseriesPoint.series, desc(TimeseriesPoint.ts))
        )
    ).all()
    by_model: dict[str, dict[str, dict]] = {}
    src_updated: dict[str, datetime] = {}
    seen: set[str] = set()
    for r in rows:
        if r.series in seen:
            continue  # first row per series = latest (ordered desc)
        seen.add(r.series)
        meta = r.meta or {}
        src, model = meta.get("source"), meta.get("model")
        if not (src and model):
            continue
        s = src.removeprefix("gpu_")
        by_model.setdefault(model, {})[s] = {"value": float(r.value), "ts": r.ts}
        if s not in src_updated or r.ts > src_updated[s]:
            src_updated[s] = r.ts
    return by_model, src_updated


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

    per_source, src_updated = await _latest_per_source_ondemand(db)
    out_models = []
    for key in sorted(set(models) | set(per_source)):
        med_series = f"gpu_{key}_ondemand_median"
        latest = await _latest(db, med_series)
        ondemand_median = latest[0] if latest else None
        ratio = await _latest(db, f"gpu_{key}_spot_ratio")
        avail = await _latest(db, f"gpu_{key}_avail_count")
        wow = await _wow(db, med_series, ondemand_median) if ondemand_median is not None else None
        srcs = per_source.get(key, {})  # {source: {"value", "ts"}}

        # 30d trend: prefer the marketplace blend, but fall back to the consensus
        # (ComputePrices) or hyperscaler (Azure) per-source series so models that never
        # reach the marketplaces — e.g. GB200, B300 — still draw a real line instead of a
        # dotted blank. `basis` tells the UI which tier the line reflects so a non-blend
        # trend isn't silently compared against a blended one.
        if ondemand_median is not None:
            trend_series, basis = med_series, "blend"
        elif "computeprices" in srcs:
            trend_series, basis = f"gpu_computeprices_{key}_ondemand", "consensus"
        elif "azure" in srcs:
            trend_series, basis = f"gpu_azure_{key}_ondemand", "hyperscaler"
        else:
            trend_series, basis = None, None

        # as_of for the row = the most recent point feeding any of its displayed numbers.
        stamps = [v["ts"] for v in srcs.values()]
        if latest:
            stamps.append(latest[1])
        as_of = max(stamps) if stamps else None

        out_models.append({
            "key": key,
            "display": MODELS.get(key, {}).get("display", key),
            "class": gpu_class(key),
            "ondemand_median": round(ondemand_median, 4) if ondemand_median is not None else None,
            "ondemand_delta_wow": round(wow, 4) if wow is not None else None,
            # consensus = cross-provider aggregator (ComputePrices); hyperscaler = Azure — both
            # shown for context but kept OUT of the marketplace blend (different price tiers).
            "consensus": round(srcs["computeprices"]["value"], 4) if "computeprices" in srcs else None,
            "hyperscaler": round(srcs["azure"]["value"], 4) if "azure" in srcs else None,
            "spot_ratio": round(ratio[0], 3) if ratio else None,
            "avail_count": int(avail[0]) if avail else None,
            "sources": {s: round(v["value"], 4) for s, v in srcs.items()},
            "sources_as_of": {s: v["ts"].isoformat() for s, v in srcs.items()},
            "n_sources": len(srcs),
            "as_of": as_of.isoformat() if as_of else None,
            "sparkline_basis": basis,
            "sparkline": await sparkline(trend_series, days=30) if trend_series else [],
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
        # Narrative GPU-market verdict, rendered above the tables on /gpu (null until the
        # first `/gpu` run). Separate slot from the home page's composite verdict.
        "verdict": await _latest_gpu_verdict(db),
        # One explicit last-updated timestamp per dataset (source), newest first.
        "sources_updated": {
            s: ts.isoformat()
            for s, ts in sorted(src_updated.items(), key=lambda kv: kv[1], reverse=True)
        },
        # Most recent point across every source — the real "market as of" for the page,
        # as opposed to wall-clock page-load time.
        "data_as_of": max(src_updated.values()).isoformat() if src_updated else None,
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }
