from sqlalchemy import Column, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB

from app.models import Base


class RawItem(Base):
    __tablename__ = "raw_items"

    id = Column(String, primary_key=True)
    source_id = Column(String, ForeignKey("sources.id"), nullable=False, index=True)
    pillar = Column(String, nullable=False, index=True)
    url = Column(String)
    title = Column(String)
    author = Column(String)
    published_at = Column(DateTime(timezone=True), index=True)
    fetched_at = Column(DateTime(timezone=True), nullable=False)
    raw_text = Column(String)
    raw_json = Column(JSONB)

    __table_args__ = (
        Index("ix_raw_items_pillar_pub", "pillar", "published_at"),
    )
