from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, String

from app.models import Base


class Source(Base):
    __tablename__ = "sources"

    id = Column(String, primary_key=True)
    pillar = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False)
    url = Column(String, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    last_fetched_at = Column(DateTime(timezone=True))

    # Staleness thresholds aligned to each kind's ingest cadence (see tasks/scheduler.py).
    # A flat 48h cutoff falsely flagged the weekly curated sources (capex, merchant_ai,
    # enterprise_roi) as "stale" ~5 of every 7 days even when perfectly healthy, since
    # their job only runs weekly (Sun). Per-kind grace fixes that without hiding a source
    # that has genuinely stopped refreshing.
    _STALE_THRESHOLD_HOURS: dict[str, int] = {
        "anthropic_html": 48,       # every 2h
        "openrouter": 48,           # daily 03:00 UTC
        "gpu_rental": 48,           # every 6h
        "capex": 8 * 24,            # weekly Sun 04:00 UTC + grace
        "merchant_ai": 8 * 24,      # weekly Sun 04:15 UTC + grace
        "enterprise_roi": 8 * 24,   # weekly Sun 04:30 UTC + grace
    }
    _DEFAULT_STALE_HOURS = 48

    @property
    def stale_threshold_hours(self) -> int:
        return self._STALE_THRESHOLD_HOURS.get(self.kind, self._DEFAULT_STALE_HOURS)

    def is_stale(self, now: datetime) -> bool:
        """True once last_fetched_at is older than this source's cadence-aware threshold.
        A never-fetched source (last_fetched_at is None) is not stale — it's un-started."""
        if not self.last_fetched_at:
            return False
        return (now - self.last_fetched_at).total_seconds() > self.stale_threshold_hours * 3600
