from sqlalchemy import BigInteger, Column, DateTime, String, func

from app.models import Base


class Digest(Base):
    __tablename__ = "digests"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    period = Column(String, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    markdown = Column(String, nullable=False)
