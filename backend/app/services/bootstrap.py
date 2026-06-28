"""Idempotent bootstrap of default sources for the Acceleration scoreboard."""
import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session
from app.models import Source

logger = logging.getLogger(__name__)


# Six dimensions of acceleration. Sources feed dimensions, not pillars —
# `pillar` here is repurposed as the dimension key.
DEFAULT_SOURCES: list[dict] = [
    # Capability + Recursive AI — all Anthropic-side content discovered via anthropic.com/sitemap.xml
    # plus claude.com/blog listing. No third-party RSS mirrors. OpenAI/DeepMind sources removed
    # 2026-05-09 (low signal). Discovery logic in anthropic_html_ingest.py.
    {
        "id": "anthropic_news",
        "pillar": "capability",
        "kind": "anthropic_html",
        "url": "https://www.anthropic.com/news",
        "enabled": True,
    },
    {
        "id": "anthropic_engineering",
        "pillar": "capability",
        "kind": "anthropic_html",
        "url": "https://www.anthropic.com/engineering",
        "enabled": True,
    },
    {
        "id": "anthropic_research",
        "pillar": "recursive_ai",
        "kind": "anthropic_html",
        "url": "https://www.anthropic.com/research",
        "enabled": True,
    },
    {
        "id": "claude_blog",
        "pillar": "capability",
        "kind": "anthropic_html",
        "url": "https://claude.com/blog",
        "enabled": True,
    },
    # Inference cost — OpenRouter frontier-model pricing
    {
        "id": "openrouter_pricing",
        "pillar": "inference_cost",
        "kind": "openrouter",
        "url": "https://openrouter.ai/api/v1/models",
        "enabled": True,
    },
    # Hyperscaler $ — quarterly capex curated from MSFT/GOOGL/META/AMZN 8-K cash flow statements
    {
        "id": "hyperscaler_capex",
        "pillar": "hyperscaler",
        "kind": "capex",
        "url": "file:///app/../config/hyperscaler_capex.json",
        "enabled": True,
    },
    # Infrastructure — merchant AI-silicon revenue (NVDA DC + AMD DC + AVGO AI). Supply-side proxy
    # for AI buildout. AVGO is included because it designs custom AI silicon + AI networking for
    # hyperscalers (notably Google TPU networking) — closest available proxy for ASIC supply since
    # GOOGL/AMZN don't separately disclose TPU/Trainium revenue.
    {
        "id": "merchant_ai_silicon",
        "pillar": "infrastructure",
        "kind": "merchant_ai",
        "url": "file:///app/../config/merchant_ai_silicon.json",
        "enabled": True,
    },
    # Enterprise ROI — survey-driven demand-side measure (% of enterprises reporting measurable ROI
    # from AI agent deployments). Curated from primary surveys (Anthropic, BCG, McKinsey, IBM,
    # Gartner, Stanford AI Index). Companion to Hyperscaler $ which tracks supply-side capex spend.
    {
        "id": "enterprise_roi",
        "pillar": "enterprise_roi",
        "kind": "enterprise_roi",
        "url": "file:///app/../config/enterprise_roi.json",
        "enabled": True,
    },
    # Infrastructure (live GPU rental rates) — compute-demand / scarcity signal. Companion to
    # the quarterly merchant-silicon supply-side series. kind="gpu_rental" sources are polled
    # every 6h by gpu_rental_job; normalized to $/GPU/hr and written as gpu_* timeseries.
    # Dispatch + per-provider parsing live in services/gpu_rental_ingest.py (COLLECTORS).
    # Phase 1 ships Akash first (no-auth, pre-aggregated, no unit trap); more providers added
    # to gpu_rental_ingest.COLLECTORS get their source row appended here.
    {
        "id": "gpu_akash",
        "pillar": "infrastructure",
        "kind": "gpu_rental",
        "url": "https://console-api.akash.network/v1/gpu-prices",
        "enabled": True,
    },
    {
        "id": "gpu_clore",
        "pillar": "infrastructure",
        "kind": "gpu_rental",
        "url": "https://api.clore.ai/v1/marketplace",
        "enabled": True,
    },
    # TensorDock decommissioned its no-auth v0 marketplace host in 2026 (404); the v2 API is
    # key-gated. Ships DISABLED like Vast — set TENSORDOCK_API_KEY in .env, then enable via
    # POST /api/sources/gpu_tensordock/toggle. Collector: _collect_tensordock_v2.
    {
        "id": "gpu_tensordock",
        "pillar": "infrastructure",
        "kind": "gpu_rental",
        "url": "https://dashboard.tensordock.com/api/v2/hostnodes",
        "enabled": False,
    },
    {
        "id": "gpu_runpod",
        "pillar": "infrastructure",
        "kind": "gpu_rental",
        "url": "https://api.runpod.io/graphql",
        "enabled": True,
    },
    {
        "id": "gpu_computeprices",
        "pillar": "infrastructure",
        "kind": "gpu_rental",
        "url": "https://computeprices.com/api/v1/gpu-prices",
        "enabled": True,
    },
    {
        "id": "gpu_azure",
        "pillar": "infrastructure",
        "kind": "gpu_rental",
        "url": "https://prices.azure.com/api/retail/prices",
        "enabled": True,
    },
    # Vast.ai is ToS-gated on the official API + free key. Ships DISABLED — set VAST_API_KEY in
    # .env, then enable via POST /api/sources/gpu_vast/toggle. (No anonymous polling.)
    {
        "id": "gpu_vast",
        "pillar": "infrastructure",
        "kind": "gpu_rental",
        "url": "https://console.vast.ai/api/v0/bundles/",
        "enabled": False,
    },
]


async def ensure_default_sources() -> None:
    """Upsert defaults: refresh url/pillar/kind on each boot; preserve enabled flag if user toggled it."""
    async with async_session() as db:
        for src in DEFAULT_SOURCES:
            stmt = pg_insert(Source).values(**src)
            stmt = stmt.on_conflict_do_update(
                index_elements=["id"],
                set_={"pillar": stmt.excluded.pillar, "kind": stmt.excluded.kind, "url": stmt.excluded.url},
            )
            await db.execute(stmt)
        # Cleanup: remove sources (and their raw_items + signals) that are no longer in DEFAULT_SOURCES.
        # Off-mission ingesters (arXiv, HN, CISA, NVD) get pruned this way during the surgical reset.
        from sqlalchemy import delete
        from app.models import RawItem, Signal, Source as S
        keep_ids = [s["id"] for s in DEFAULT_SOURCES]
        existing = (await db.execute(select(S.id).where(~S.id.in_(keep_ids)))).all()
        if existing:
            removing = [r[0] for r in existing]
            # Cascade by hand: signals → raw_items → sources
            await db.execute(
                delete(Signal).where(
                    Signal.raw_item_id.in_(select(RawItem.id).where(RawItem.source_id.in_(removing)))
                )
            )
            await db.execute(delete(RawItem).where(RawItem.source_id.in_(removing)))
            await db.execute(delete(S).where(S.id.in_(removing)))
            logger.info("Pruned %d obsolete sources and their data", len(removing))
        await db.commit()
        logger.info("Ensured %d default sources", len(DEFAULT_SOURCES))
