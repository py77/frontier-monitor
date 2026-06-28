"""Source health + manual ingest trigger."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import RawItem, Source

router = APIRouter()


@router.get("/sources")
async def list_sources(db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(Source).order_by(Source.pillar, Source.id))).scalars().all()
    counts = dict(
        (r[0], r[1])
        for r in (
            await db.execute(select(RawItem.source_id, func.count(RawItem.id)).group_by(RawItem.source_id))
        ).all()
    )
    now = datetime.now(timezone.utc)
    out = []
    for s in rows:
        out.append({
            "id": s.id,
            "pillar": s.pillar,
            "kind": s.kind,
            "url": s.url,
            "enabled": s.enabled,
            "last_fetched_at": s.last_fetched_at.isoformat() if s.last_fetched_at else None,
            "item_count": counts.get(s.id, 0),
            "stale": s.is_stale(now),
            "stale_threshold_hours": s.stale_threshold_hours,
        })
    return out


@router.post("/ingest/{source_id}")
async def trigger_ingest(source_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    src = await db.get(Source, source_id)
    if not src:
        raise HTTPException(status_code=404, detail="source not found")
    if src.kind == "anthropic_html":
        from app.services.anthropic_html_ingest import ingest_source
        return await ingest_source(source_id)
    if src.kind == "openrouter":
        from app.services.openrouter_ingest import ingest_source
        return await ingest_source(source_id)
    if src.kind == "capex":
        from app.services.capex_ingest import ingest_source
        return await ingest_source(source_id)
    if src.kind == "merchant_ai":
        from app.services.merchant_ai_ingest import ingest_source
        return await ingest_source(source_id)
    if src.kind == "enterprise_roi":
        from app.services.enterprise_roi_ingest import ingest_source
        return await ingest_source(source_id)
    if src.kind == "gpu_rental":
        from app.services.gpu_rental_ingest import ingest_source, recompute_aggregates
        result = await ingest_source(source_id)
        await recompute_aggregates()  # refresh headline cross-source series after a manual pull
        return result
    raise HTTPException(status_code=400, detail=f"no ingester for kind={src.kind}")


@router.post("/admin/backfill-anthropic-dates")
async def backfill_anthropic_dates() -> dict:
    """Re-resolve published_at for every anthropic_html raw_item from live HTML.

    Use after the date-extraction logic changes (e.g. Anthropic site redesign breaking
    sitemap lastmod). Idempotent — only writes when the new resolved value differs.
    """
    from app.services.anthropic_html_ingest import backfill_published_at
    return await backfill_published_at()


@router.post("/admin/backfill-anthropic-bodies")
async def backfill_anthropic_bodies() -> dict:
    """Re-fetch each anthropic_html / claude_blog raw_item and replace raw_text with body text.

    Use after extending the ingest to capture article body content (instead of just
    og:description). Idempotent — skips rows that already have raw_json.body_extracted=True.
    """
    from app.services.anthropic_html_ingest import backfill_body_text
    return await backfill_body_text()


@router.post("/sources/{source_id}/toggle")
async def toggle_source(source_id: str, db: AsyncSession = Depends(get_db)):
    src = await db.get(Source, source_id)
    if not src:
        raise HTTPException(status_code=404, detail="source not found")
    src.enabled = not src.enabled
    await db.commit()
    return {"id": src.id, "enabled": src.enabled}
