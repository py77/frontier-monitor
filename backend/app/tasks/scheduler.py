"""APScheduler — ingest + score + alerts + memo cadence for the scoreboard."""
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()


async def anthropic_html_job():
    """Scrape /research and claude.com/blog (no native RSS). Caps to 30 articles per run."""
    from sqlalchemy import select
    from app.database import async_session
    from app.models import Source
    from app.services.anthropic_html_ingest import ingest_source
    try:
        async with async_session() as db:
            ids = [r[0] for r in (await db.execute(
                select(Source.id).where(Source.kind == "anthropic_html", Source.enabled.is_(True))
            )).all()]
        for sid in ids:
            await ingest_source(sid)
    except Exception as e:
        logger.error("anthropic_html_job failed: %s", e)


async def openrouter_job():
    from app.services.openrouter_ingest import ingest_source
    try:
        result = await ingest_source("openrouter_pricing")
        logger.info("openrouter_job: median=$%s/MTok across %d models",
                    result.get("median_per_mtok"), result.get("tracked", 0))
    except Exception as e:
        logger.error("openrouter_job failed: %s", e)


async def capex_job():
    """Reload hyperscaler capex from the curated JSON config (cheap; safe on every boot)."""
    from app.services.capex_ingest import ingest_source
    try:
        result = await ingest_source("hyperscaler_capex")
        logger.info("capex_job: %s", result.get("status"))
    except Exception as e:
        logger.error("capex_job failed: %s", e)


async def merchant_ai_job():
    """Reload merchant AI-silicon revenue (NVDA + AMD + AVGO) from the curated JSON config."""
    from app.services.merchant_ai_ingest import ingest_source
    try:
        result = await ingest_source("merchant_ai_silicon")
        tickers = result.get("tickers", [])
        summary = ", ".join(f"{t['ticker']}={t['latest_period']}/${t['latest_revenue_m']/1000:.1f}B" for t in tickers)
        logger.info("merchant_ai_job: %s [%s]", result.get("status"), summary)
    except Exception as e:
        logger.error("merchant_ai_job failed: %s", e)


async def enterprise_roi_job():
    """Reload enterprise AI-agent ROI surveys from the curated JSON config."""
    from app.services.enterprise_roi_ingest import ingest_source
    try:
        result = await ingest_source("enterprise_roi")
        logger.info("enterprise_roi_job: %s latest=%s/%s%%",
                    result.get("status"), result.get("latest_date"), result.get("latest_roi_pct"))
    except Exception as e:
        logger.error("enterprise_roi_job failed: %s", e)


async def gpu_rental_job():
    """Poll every enabled gpu_rental source, then recompute headline cross-source aggregates.
    Normalizes GPU rental rates to $/GPU/hr → gpu_* timeseries (Infrastructure dimension)."""
    from sqlalchemy import select
    from app.database import async_session
    from app.models import Source
    from app.services.gpu_rental_ingest import ingest_source, recompute_aggregates
    try:
        async with async_session() as db:
            ids = [r[0] for r in (await db.execute(
                select(Source.id).where(Source.kind == "gpu_rental", Source.enabled.is_(True))
            )).all()]
        for sid in ids:
            result = await ingest_source(sid)
            logger.info("gpu_rental_job: %s → %s (%s models)",
                        sid, result.get("status"), result.get("models_written", 0))
        await recompute_aggregates()
    except Exception as e:
        logger.error("gpu_rental_job failed: %s", e)


async def score_job():
    """Recompute and persist the 6 dimensional scores + Acceleration Index."""
    from app.services.score_engine import compute_and_persist
    try:
        result = await compute_and_persist()
        logger.info("score_job: index=%s dims=%s", result.get("index"), {k: v["score"] for k, v in result.get("dimensions", {}).items()})
    except Exception as e:
        logger.error("score_job failed: %s", e)


async def alerts_job():
    """Detect threshold crossings on score deltas and write Alert rows."""
    from app.services.alerts_engine import scan_and_fire
    try:
        fired = await scan_and_fire()
        if fired:
            logger.info("alerts_job: fired %d alerts", fired)
    except Exception as e:
        logger.error("alerts_job failed: %s", e)


def start_scheduler() -> None:
    # Anthropic /news + /engineering + /research + claude.com/blog: every 2 hours (covers all
    # narrative sources via anthropic.com sitemap + claude.com listing).
    scheduler.add_job(anthropic_html_job, "interval", hours=2, id="anthropic_html_ingest",
                      misfire_grace_time=None, coalesce=True)
    # OpenRouter pricing: daily 03:00 UTC
    scheduler.add_job(openrouter_job, "cron", hour=3, minute=0, id="openrouter_ingest",
                      misfire_grace_time=None, coalesce=True)
    # Hyperscaler capex: weekly (data only changes after earnings cycles).
    scheduler.add_job(capex_job, "cron", day_of_week="sun", hour=4, minute=0, id="capex_ingest",
                      misfire_grace_time=None, coalesce=True)
    # Merchant AI silicon (NVDA + AMD + AVGO): weekly (data only changes after earnings releases).
    scheduler.add_job(merchant_ai_job, "cron", day_of_week="sun", hour=4, minute=15, id="merchant_ai_ingest",
                      misfire_grace_time=None, coalesce=True)
    # Enterprise ROI surveys: weekly (new surveys publish irregularly; cheap to re-load curated JSON).
    scheduler.add_job(enterprise_roi_job, "cron", day_of_week="sun", hour=4, minute=30, id="enterprise_roi_ingest",
                      misfire_grace_time=None, coalesce=True)
    # GPU rental rates: every 6 hours (live marketplace + list-price sources).
    scheduler.add_job(gpu_rental_job, "interval", hours=6, id="gpu_rental_ingest",
                      misfire_grace_time=None, coalesce=True)
    # Score recomputation: hourly (cheap, derived)
    scheduler.add_job(score_job, "interval", hours=1, id="score",
                      misfire_grace_time=None, coalesce=True)
    # Alerts: every 15 minutes after score updates
    scheduler.add_job(alerts_job, "interval", minutes=15, id="alerts",
                      misfire_grace_time=None, coalesce=True)
    scheduler.start()
    logger.info("Scheduler started: anthropic_html(2h), openrouter(03:00), capex/merchant_ai(weekly), gpu_rental(6h), score(1h), alerts(15m)")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
    logger.info("Scheduler stopped")
