"""Daily memo storage."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import Digest

router = APIRouter()


class DigestIn(BaseModel):
    period: str
    markdown: str


@router.post("/digests")
async def create_digest(body: DigestIn, db: AsyncSession = Depends(get_db)) -> dict:
    d = Digest(period=body.period, markdown=body.markdown, created_at=datetime.now(timezone.utc))
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return {"id": d.id, "period": d.period, "created_at": d.created_at.isoformat()}


@router.get("/digests/latest")
async def latest_digest(period: str | None = None, db: AsyncSession = Depends(get_db)) -> dict:
    """Latest stored markdown. Default = the daily memo; pass ?period=verdict for the
    dashboard verdict (a different artifact sharing this table)."""
    stmt = select(Digest)
    if period is not None:
        stmt = stmt.where(Digest.period == period)
    else:
        stmt = stmt.where(Digest.period != "verdict")
    stmt = stmt.order_by(desc(Digest.created_at)).limit(1)
    d = (await db.execute(stmt)).scalar_one_or_none()
    if not d:
        raise HTTPException(status_code=404, detail="no digests yet")
    return {"id": d.id, "period": d.period, "created_at": d.created_at.isoformat(), "markdown": d.markdown}
