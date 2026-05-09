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
