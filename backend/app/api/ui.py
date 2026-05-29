"""Server-rendered UI: scoreboard home + drill-downs."""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import RawItem, Signal, Source

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    return templates.TemplateResponse("home.html", {"request": request})


@router.get("/gpu", response_class=HTMLResponse)
async def gpu_page(request: Request):
    """GPU rental-rate monitor — per-model $/GPU/hr, spot ratio, availability, 30d trend."""
    return templates.TemplateResponse("gpu.html", {"request": request})


@router.get("/signal/{raw_item_id}", response_class=HTMLResponse)
async def signal_detail(raw_item_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    item = await db.get(RawItem, raw_item_id)
    if not item:
        raise HTTPException(status_code=404, detail="raw item not found")
    sigs = (
        await db.execute(
            select(Signal).where(Signal.raw_item_id == raw_item_id).order_by(desc(Signal.created_at))
        )
    ).scalars().all()
    return templates.TemplateResponse(
        "signal_detail.html",
        {"request": request, "item": item, "signals": sigs},
    )


@router.get("/sources", response_class=HTMLResponse)
async def sources_page(request: Request, db: AsyncSession = Depends(get_db)):
    from datetime import datetime, timezone
    rows = (await db.execute(select(Source).order_by(Source.pillar, Source.id))).scalars().all()
    now = datetime.now(timezone.utc)
    sources = []
    for s in rows:
        sources.append({
            "id": s.id,
            "pillar": s.pillar,
            "kind": s.kind,
            "enabled": s.enabled,
            "last_fetched_at": s.last_fetched_at,
            "stale": bool(s.last_fetched_at and (now - s.last_fetched_at).total_seconds() > 48 * 3600),
        })
    return templates.TemplateResponse("sources.html", {"request": request, "sources": sources})
