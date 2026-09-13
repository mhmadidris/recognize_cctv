import uuid

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from app.utils.database import Base


class EventVisitorSetting(Base):
    __tablename__ = "event_visitor_settings"

    id = Column(Integer, primary_key=True)
    company_id = Column(String(36), nullable=True, index=True)
    camera_source = Column(String, nullable=True)
    line_position = Column(Float, nullable=False, default=0.5)
    line_orientation = Column(String(10), nullable=False, default="horizontal")
    reverse_direction = Column(Boolean, nullable=False, default=False)
    model_tier = Column(String(1), nullable=False, default="m")


class EventVisitorSession(Base):
    __tablename__ = "event_visitor_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(String(36), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    status = Column(String(20), nullable=False, default="active")
    started_at = Column(DateTime(timezone=True), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    timezone = Column(String(50), nullable=False, default="Asia/Jakarta")
    camera_source = Column(String, nullable=True)
    event_start = Column(String(10), nullable=True)
    event_end = Column(String(10), nullable=True)
    event_id = Column(UUID(as_uuid=True), nullable=True)


class EventVisitorEvent(Base):
    __tablename__ = "event_visitor_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(String(36), nullable=True, index=True)
    session_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    visitor_id = Column(String(32), nullable=False, index=True)
    visitor_label = Column(String(100), nullable=False)
    camera_source = Column(String, nullable=True)
    gender = Column(String(10), nullable=False)
    direction = Column(String(3), nullable=False)
    detected_at = Column(DateTime(timezone=True), nullable=False, index=True)
