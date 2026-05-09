from sqlalchemy import Column, DateTime, Float, String
from sqlalchemy.dialects.postgresql import JSONB

from app.models import Base


class TimeseriesPoint(Base):
    __tablename__ = "timeseries"

    series = Column(String, primary_key=True)
    ts = Column(DateTime(timezone=True), primary_key=True)
    value = Column(Float, nullable=False)
    meta = Column(JSONB)
