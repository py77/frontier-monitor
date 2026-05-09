from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB

from app.models import Base


class Signal(Base):
    __tablename__ = "signals"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    raw_item_id = Column(String, ForeignKey("raw_items.id"), nullable=False, index=True)
    signal_type = Column(String, nullable=False)
    analyst_version = Column(String, nullable=False)
    pillar = Column(String, nullable=False, index=True)
    payload = Column(JSONB, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("raw_item_id", "signal_type", "analyst_version", name="uq_signal_idem"),
    )
