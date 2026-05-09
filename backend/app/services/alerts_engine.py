"""Threshold-cross detection on dimensional scores. Writes Alert rows."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session
from app.models import Alert, TimeseriesPoint
from app.services.score_engine import DIMENSION_LABELS

logger = logging.getLogger(__name__)


# Rules: (dimension, threshold delta WoW, severity, headline template)
RULES = [
    ("score_index", 5, "warn", "Acceleration Index up {delta:+.1f} wk/wk"),
    ("score_recursive_ai", 8, "warn", "Recursive-AI dimension up {delta:+.1f} wk/wk"),
    ("score_capability", 8, "warn", "Capability dimension up {delta:+.1f} wk/wk"),
    ("score_infrastructure", 10, "warn", "Infrastructure dimension up {delta:+.1f} wk/wk"),
    ("score_index", -5, "info", "Acceleration Index down {delta:+.1f} wk/wk"),
]


async def scan_and_fire() -> int:
    """Compare latest score for each tracked series vs ~7 days ago.
    Fires an alert when delta crosses a rule threshold and no identical alert was fired in the last 24h.
    """
    fired = 0
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    day_ago = now - timedelta(days=1)

    async with async_session() as db:
        for series, threshold, severity, template in RULES:
            latest_row = (
                await db.execute(
                    select(TimeseriesPoint.value, TimeseriesPoint.ts)
                    .where(TimeseriesPoint.series == series)
                    .order_by(desc(TimeseriesPoint.ts)).limit(1)
                )
            ).first()
            if not latest_row:
                continue
            week_row = (
                await db.execute(
                    select(TimeseriesPoint.value)
                    .where(TimeseriesPoint.series == series, TimeseriesPoint.ts <= week_ago)
                    .order_by(desc(TimeseriesPoint.ts)).limit(1)
                )
            ).first()
            if not week_row:
                continue
            delta = latest_row.value - week_row.value
            crosses_up = threshold > 0 and delta >= threshold
            crosses_down = threshold < 0 and delta <= threshold
            if not (crosses_up or crosses_down):
                continue

            headline = template.format(delta=delta)
            # Dedup: skip if an identical-headline alert fired in last 24h
            existing = (
                await db.execute(
                    select(Alert.id).where(Alert.headline == headline, Alert.fired_at >= day_ago).limit(1)
                )
            ).first()
            if existing:
                continue

            dim = series.removeprefix("score_")
            db.add(Alert(
                dimension=dim,
                severity=severity,
                headline=headline,
                detail=f"{DIMENSION_LABELS.get(dim, dim)}: {week_row.value:.1f} → {latest_row.value:.1f}",
                data={"series": series, "delta": delta, "threshold": threshold},
            ))
            fired += 1

        if fired:
            await db.commit()
    return fired
