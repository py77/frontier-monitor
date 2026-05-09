from sqlalchemy import BigInteger, Column, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB

from app.models import Base


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    fired_at = Column(DateTime(timezone=True), server_default=func.now())
    dimension = Column(String, nullable=False)
    severity = Column(String, nullable=False)  # info | warn | critical
    headline = Column(String, nullable=False)
    detail = Column(String)
    data = Column(JSONB)
