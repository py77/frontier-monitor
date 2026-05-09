"""Enterprise AI-agent ROI surveys from a curated config file.

Source-of-truth lives in `config/enterprise_roi.json`. Each entry is a survey
datapoint: date, source, sample_size, roi_pct (% reporting measurable ROI),
plus a methodology note. Add new surveys as they publish (Anthropic, BCG,
McKinsey, IBM, Gartner, Stanford AI Index, etc.) and re-run the ingest.

Writes two parallel timeseries:
  * `roi_pct`         — % of enterprises reporting measurable ROI (drives the score)
  * `roi_sample_size` — sample size of each survey (for headline weighting + context)
"""
import json
import logging
from datetime import datetime, time, timezone
from pathlib import Path

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session
from app.models import Source, TimeseriesPoint

logger = logging.getLogger(__name__)

CONFIG_PATH = next(
    (p for p in [
        Path("/config/enterprise_roi.json"),
        Path(__file__).resolve().parent.parent.parent.parent / "config" / "enterprise_roi.json",
    ] if p.exists()),
    Path("/config/enterprise_roi.json"),
)


def _date_to_dt(date_str: str) -> datetime:
    """'2026-04-30' → end-of-day UTC datetime for timeseries indexing."""
    d = datetime.fromisoformat(date_str).date()
    return datetime.combine(d, time(23, 59, 59), tzinfo=timezone.utc)


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


async def ingest_source(source_id: str) -> dict:
    cfg = _load_config()
    surveys = cfg.get("surveys", [])
    if not surveys:
        return {"source_id": source_id, "status": "config_empty", "path": str(CONFIG_PATH)}

    async with async_session() as db:
        src = await db.get(Source, source_id)
        if not src:
            return {"source_id": source_id, "status": "missing"}
        if not src.enabled:
            return {"source_id": source_id, "status": "disabled"}

        now = datetime.now(timezone.utc)
        inserted = 0

        for s in surveys:
            ts = _date_to_dt(s["date"])
            meta = {
                "date": s["date"],
                "source": s.get("source"),
                "url": s.get("url"),
                "methodology_note": s.get("methodology_note"),
                "sample_size": s.get("sample_size"),
            }
            for series, value in (
                ("roi_pct", float(s["roi_pct"])),
                ("roi_sample_size", float(s.get("sample_size", 0))),
            ):
                stmt = pg_insert(TimeseriesPoint).values(
                    series=series, ts=ts, value=value, meta=meta,
                ).on_conflict_do_update(
                    index_elements=["series", "ts"],
                    set_={"value": value, "meta": meta},
                )
                result = await db.execute(stmt)
                if result.rowcount:
                    inserted += 1

        latest = max(surveys, key=lambda x: x["date"])
        src.last_fetched_at = now
        await db.commit()
        logger.info("enterprise_roi_ingest: %d points inserted across %d surveys",
                    inserted, len(surveys))
        return {
            "source_id": source_id,
            "status": "ok",
            "n_surveys": len(surveys),
            "latest_date": latest["date"],
            "latest_roi_pct": latest["roi_pct"],
            "config_path": str(CONFIG_PATH),
        }
