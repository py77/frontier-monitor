"""Scoreboard endpoint — drives the home page."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Digest, RawItem, Signal, TimeseriesPoint
from app.services.score_engine import (
    DIMENSION_WEIGHTS,
    compute_scores,
    sparkline,
)

router = APIRouter()


@router.get("/scoreboard")
async def scoreboard(db: AsyncSession = Depends(get_db)) -> dict:
    """One call powers the home page: index, dimensions, sparklines, what-changed feed.

    Read-only — does NOT persist a timeseries row. The scheduler's hourly score_job is the
    sole writer; otherwise every dashboard refresh would inflate the daily-bucketed sparkline."""
    state = await compute_scores()

    # Sparklines per dimension (and composite). Each is a list of {day, value} — one per day.
    state["sparkline_index"] = await sparkline("score_index", days=30)
    for dim in state["dimensions"]:
        state["dimensions"][dim]["sparkline"] = await sparkline(f"score_{dim}", days=30)

    # WoW delta on the composite Index + per-dimension (rendered only when ≥7 days of data)
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)

    async def _wow_delta(series: str, current: float) -> float | None:
        row = (
            await db.execute(
                select(TimeseriesPoint.value)
                .where(TimeseriesPoint.series == series, TimeseriesPoint.ts <= week_ago)
                .order_by(desc(TimeseriesPoint.ts))
                .limit(1)
            )
        ).first()
        return (current - float(row[0])) if row else None

    state["index_delta_wow"] = await _wow_delta("score_index", state["index"])
    for dim_key, dim in state["dimensions"].items():
        dim["delta_wow"] = await _wow_delta(f"score_{dim_key}", dim["score"])

    # Days of history (drives front-end "Nd" caption + line/dot mode)
    state["days_tracked"] = len(state["sparkline_index"])

    # What changed: top 8 signals by importance for articles dated in last 7d.
    # Filter by article date (published_at, falling back to fetched_at), NOT signal
    # created_at — otherwise a fresh /refresh on a backlog of year-old articles surfaces
    # them as "new this week."
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    article_ts = func.coalesce(RawItem.published_at, RawItem.fetched_at)
    sigs = (
        await db.execute(
            select(Signal, RawItem)
            .join(RawItem, Signal.raw_item_id == RawItem.id)
            .where(Signal.analyst_version == settings.analyst_version, article_ts >= cutoff)
            .order_by(desc(article_ts))
        )
    ).all()
    scored = [
        s for s in sigs
        if not s[0].payload.get("skipped")
        and float(s[0].payload.get("importance_0_5", 0) or 0) >= 1
    ]
    scored.sort(key=lambda r: float(r[0].payload.get("importance_0_5", 0) or 0), reverse=True)
    state["what_changed"] = [
        {
            "raw_item_id": r.id,
            "title": r.title,
            "tldr": s.payload.get("tldr") or s.payload.get("thesis"),
            "importance": s.payload.get("importance_0_5"),
            "tickers": s.payload.get("market_relevance", []),
            "pillar": s.pillar,
            "dimension_tags": list(set(s.payload.get("pillar_tags") or [])),
        }
        for s, r in scored[:8]
    ]

    # Analyst verdict: the narrative synthesis written by /refresh (period="verdict"),
    # rendered full-width above the dimensions table. Latest one wins.
    verdict_row = (
        await db.execute(
            select(Digest)
            .where(Digest.period == "verdict")
            .order_by(desc(Digest.created_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    state["analyst_verdict"] = (
        {"markdown": verdict_row.markdown, "created_at": verdict_row.created_at.isoformat()}
        if verdict_row
        else None
    )

    return state


@router.get("/scoreboard/weights")
async def get_weights() -> dict:
    return DIMENSION_WEIGHTS
