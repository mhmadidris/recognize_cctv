from sqlalchemy.dialects.postgresql import UUID
import uuid
from sqlalchemy import Boolean, Column, Date, String, DateTime
from app.utils.database import Base
from sqlalchemy.sql import func

class Event(Base):
    __tablename__ = "events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(255), nullable=False)
    event_date = Column(Date, nullable=True)
    event_start = Column(String(10), nullable=True)
    event_end = Column(String(10), nullable=True)
    auto_run = Column(Boolean, nullable=False, default=True, server_default="true")
    user_id = Column(UUID(as_uuid=True), nullable=True)
    company_id = Column(UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=func.now())
