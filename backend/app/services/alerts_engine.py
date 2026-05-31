"""Threshold-cross detection on dimensional scores. Writes Alert rows."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.database import async_session
from app.models import Alert, TimeseriesPoint
from app.services.baselines import baseline_captured_at
from app.services.score_engine import DIMENSION_LABELS

logger = logging.getLogger(__name__)


# Rules: (series, threshold delta WoW, severity, headline template)
# Score series carry 0-100 deltas; GPU rental series carry $/GPU/hr deltas (2-decimal).
RULES = [
    ("score_index", 5, "warn", "Acceleration Index up {delta:+.1f} wk/wk"),
    ("score_recursive_ai", 8, "warn", "Recursive-AI dimension up {delta:+.1f} wk/wk"),
    ("score_capability", 8, "warn", "Capability dimension up {delta:+.1f} wk/wk"),
    ("score_infrastructure", 10, "warn", "Infrastructure dimension up {delta:+.1f} wk/wk"),
    ("score_index", -5, "info", "Acceleration Index down {delta:+.1f} wk/wk"),
    # GPU rental scarcity — rising rental $/hr = tightening compute demand (Infrastructure).
    ("gpu_h100_sxm_ondemand_median", 0.5, "warn", "H100 SXM rental {delta:+.2f} $/hr wk/wk — GPU demand tightening"),
    ("gpu_h200_ondemand_median", 0.5, "warn", "H200 rental {delta:+.2f} $/hr wk/wk — GPU demand tightening"),
    ("gpu_h100_sxm_ondemand_median", -0.5, "info", "H100 SXM rental {delta:+.2f} $/hr wk/wk — easing"),
]


async def _value_at(db, series: str, ts: datetime) -> float | None:
    """Most recent value of `series` at or before `ts`, or None."""
    row = (
        await db.execute(
            select(TimeseriesPoint.value)
            .where(TimeseriesPoint.series == series, TimeseriesPoint.ts <= ts)
            .order_by(desc(TimeseriesPoint.ts)).limit(1)
        )
    ).first()
    return float(row[0]) if row else None


def _crosses(delta: float, threshold: float) -> bool:
    return (threshold > 0 and delta >= threshold) or (threshold < 0 and delta <= threshold)


async def scan_and_fire() -> int:
    """Fire an alert only when a series' WoW delta *newly* crosses a rule threshold.

    Three guards keep the feed signal, not noise:
      1. **Transition, not persistence.** Fire only if the delta crosses now AND did not a
         day ago — so a sustained move alerts once, not every day it stays elevated.
      2. **No rebase artifacts.** For rebased `score_*` series, skip any window that straddles
         a baseline re-snapshot: every dimension resets to 50 then, so the delta would report
         the goalpost moving, not real change.
      3. **24h same-headline dedup** — guards against the 15-minute scan cadence on the day a
         crossing first appears.
    """
    fired = 0
    now = datetime.now(timezone.utc)
    week_ago = now - timedelta(days=7)
    day_ago = now - timedelta(days=1)
    eight_days_ago = now - timedelta(days=8)
    snap_ts = baseline_captured_at()

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
            v_week = await _value_at(db, series, week_ago)
            if v_week is None:
                continue
            delta = latest_row.value - v_week
            if not _crosses(delta, threshold):
                continue

            # Guard 2 — a rebased-score window spanning a re-snapshot is an artifact.
            if series.startswith("score_") and snap_ts and week_ago <= snap_ts <= now:
                continue

            # Guard 1 — only on the transition. If the delta already crossed a day ago, this
            # is a standing condition (it has aged into the 24h-prior window), so stay quiet.
            v_day = await _value_at(db, series, day_ago)
            v_eight = await _value_at(db, series, eight_days_ago)
            if v_day is not None and v_eight is not None and _crosses(v_day - v_eight, threshold):
                continue

            headline = template.format(delta=delta)
            # Guard 3 — dedup the 15-min scans on the day a crossing first appears. Keyed on
            # the series (not the formatted headline) so an hourly delta drifting across a
            # 0.1 rounding boundary can't slip a second alert through the same day.
            existing = (
                await db.execute(
                    select(Alert.id)
                    .where(Alert.data["series"].astext == series, Alert.fired_at >= day_ago)
                    .limit(1)
                )
            ).first()
            if existing:
                continue

            if series.startswith("score_"):
                dim = series.removeprefix("score_")
                detail = f"{DIMENSION_LABELS.get(dim, dim)}: {v_week:.1f} → {latest_row.value:.1f}"
            else:
                # gpu_* and other raw-value series: 2-decimal $/hr, filed under Infrastructure.
                dim = "infrastructure"
                detail = f"{series.removeprefix('gpu_')}: ${v_week:.2f} → ${latest_row.value:.2f} /hr"
            db.add(Alert(
                dimension=dim,
                severity=severity,
                headline=headline,
                detail=detail,
                data={"series": series, "delta": delta, "threshold": threshold},
            ))
            fired += 1

        if fired:
            await db.commit()
    return fired
