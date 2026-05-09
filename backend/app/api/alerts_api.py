"""Alerts feed."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Alert

router = APIRouter()


@router.get("/alerts")
async def list_alerts(
    days: int = Query(7, le=90),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = (
        await db.execute(
            select(Alert).where(Alert.fired_at >= cutoff).order_by(desc(Alert.fired_at)).limit(100)
        )
    ).scalars().all()
    return [
        {
            "id": a.id,
            "fired_at": a.fired_at.isoformat() if a.fired_at else None,
            "dimension": a.dimension,
            "severity": a.severity,
            "headline": a.headline,
            "detail": a.detail,
        }
        for a in rows
    ]
