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

# Cache-bust the stylesheet link by its file mtime so a CSS edit is picked up on the next
# page load. StaticFiles sends no Cache-Control, so browsers heuristically serve a stale
# app.css off Last-Modified without revalidating — a versioned href sidesteps that. Computed
# at import (i.e. per container start / uvicorn reload), which is exactly when the CSS can change.
try:
    _asset_v = str(int((BASE_DIR / "static" / "css" / "app.css").stat().st_mtime))
except OSError:
    _asset_v = "0"
templates.env.globals["asset_v"] = _asset_v


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

    from app.services.data_recency import recency_status
    rows = (await db.execute(select(Source).order_by(Source.pillar, Source.id))).scalars().all()
    now = datetime.now(timezone.utc)
    sources = []
    for s in rows:
        rec = recency_status(s.kind, now)
        sources.append({
            "id": s.id,
            "pillar": s.pillar,
            "kind": s.kind,
            "enabled": s.enabled,
            "last_fetched_at": s.last_fetched_at,
            "stale": s.is_stale(now),
            "stale_threshold_hours": s.stale_threshold_hours,
            "behind": bool(rec and rec["behind"]),
            "expected_period": rec["expected"] if rec else None,
            "missing_tickers": rec["missing"] if rec else [],
        })
    return templates.TemplateResponse("sources.html", {"request": request, "sources": sources})
