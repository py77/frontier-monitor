"""HTTP surface that Claude Code calls during /refresh."""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import RawItem, Signal
from app.schemas.signals import PendingItem, SignalIn, SignalOut

router = APIRouter()


@router.get("/pending", response_model=list[PendingItem])
async def list_pending(
    analyst_version: str = Query("v1"),
    pillar: str | None = Query(None),
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
) -> list[PendingItem]:
    """Raw items that have no signal at the given analyst_version yet."""
    sub = (
        select(Signal.raw_item_id)
        .where(Signal.analyst_version == analyst_version)
        .where(Signal.raw_item_id == RawItem.id)
        .exists()
    )
    stmt = select(RawItem).where(~sub)
    if pillar:
        stmt = stmt.where(RawItem.pillar == pillar)
    stmt = stmt.order_by(desc(RawItem.published_at).nulls_last(), desc(RawItem.fetched_at)).limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return [
        PendingItem(
            id=r.id,
            source_id=r.source_id,
            pillar=r.pillar,
            url=r.url,
            title=r.title,
            author=r.author,
            published_at=r.published_at,
            raw_text=r.raw_text,
            raw_json=r.raw_json,
        )
        for r in rows
    ]


@router.post("/signals")
async def create_signal(
    body: SignalIn,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Idempotent insert. Returns inserted=true/false."""
    item = await db.get(RawItem, body.raw_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="raw_item_id not found")

    stmt = pg_insert(Signal).values(
        raw_item_id=body.raw_item_id,
        signal_type=body.signal_type,
        analyst_version=body.analyst_version,
        pillar=body.pillar,
        payload=body.payload,
    ).on_conflict_do_nothing(constraint="uq_signal_idem")
    result = await db.execute(stmt)
    await db.commit()
    return {"inserted": bool(result.rowcount)}


@router.get("/signals/top", response_model=list[SignalOut])
async def top_signals(
    days: int = Query(7, le=90),
    pillar: str | None = Query(None),
    analyst_version: str = Query("v1"),
    limit: int = Query(20, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[SignalOut]:
    """Signals ordered by importance_0_5 within the last N days."""
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stmt = select(Signal).where(
        Signal.created_at >= cutoff,
        Signal.analyst_version == analyst_version,
    )
    if pillar:
        stmt = stmt.where(Signal.pillar == pillar)
    rows = (await db.execute(stmt)).scalars().all()

    def importance(s: Signal) -> float:
        try:
            return float(s.payload.get("importance_0_5", 0))
        except (TypeError, ValueError):
            return 0.0

    rows = sorted(rows, key=importance, reverse=True)[:limit]
    return [
        SignalOut(
            id=s.id,
            raw_item_id=s.raw_item_id,
            signal_type=s.signal_type,
            analyst_version=s.analyst_version,
            pillar=s.pillar,
            payload=s.payload,
            created_at=s.created_at,
        )
        for s in rows
    ]
