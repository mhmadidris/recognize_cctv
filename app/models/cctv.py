import uuid
from sqlalchemy import Column, String, DateTime, Float, Boolean, Integer, Date, Time, JSON, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
from app.utils.database import Base

class CCTVCamera(Base):
    __tablename__ = "cctv_cameras"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    branch_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    name = Column(String(255), nullable=False)
    rtsp_url = Column(String(500), nullable=False)
    zone_type = Column(String(50), nullable=False, default="attendance")
    roi_enabled = Column(Boolean, default=False, nullable=False)
    roi_polygon = Column(JSON, nullable=True)
    clahe_enabled = Column(Boolean, default=False, nullable=False)
    detection_confidence_threshold = Column(Float, nullable=True)
    recognition_similarity_threshold = Column(Float, nullable=True)
    reid_similarity_threshold = Column(Float, nullable=True)
    track_confirm_hits = Column(Integer, nullable=True)
    min_face_width = Column(Integer, nullable=True)
    min_face_height = Column(Integer, nullable=True)
    min_face_brightness = Column(Float, nullable=True)
    min_face_blur = Column(Float, nullable=True)
    adaptive_frame_skip_enabled = Column(Boolean, default=False, nullable=False)
    target_inference_ms = Column(Integer, nullable=True)
    max_frame_skip = Column(Integer, nullable=True)
    status = Column(String(50), default="offline")
    last_online_at = Column(DateTime(timezone=True), onupdate=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class CCTVCompanySetting(Base):
    __tablename__ = "cctv_company_settings"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), nullable=False, unique=True, index=True)
    enabled = Column(Boolean, default=True)
    monitor_mode = Column(String(20), default="", nullable=False)
    confidence_threshold = Column(Float, default=0.75)
    cooldown_seconds = Column(Integer, default=30)
    allow_attendance_after_tolerance = Column(Boolean, default=True, nullable=False)
    time_tolerance_minutes = Column(Integer, default=0, nullable=False)
    timezone = Column(String(64), default="Asia/Jakarta", nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

class AttendanceCCTV(Base):
    __tablename__ = "attendance_cctv"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    employee_id = Column(UUID(as_uuid=True), nullable=False, index=True)
    employee_name = Column(String(255), nullable=True)
    log_type = Column(String(10), nullable=False)
    time_in = Column(Time, nullable=True)
    time_out = Column(Time, nullable=True)
    latlong_in = Column(String(255), nullable=True)
    address_in = Column(String, nullable=True)
    latlong_out = Column(String(255), nullable=True)
    address_out = Column(String, nullable=True)
    address = Column(String(255), nullable=True)
    checkin_status = Column(String(225), nullable=True)
    checkout_status = Column(String(255), nullable=True)
    attendance_status = Column(String(50), nullable=False, default="present")
    photo_in = Column(String(255), nullable=True)
    photo_out = Column(String(255), nullable=True)
    attendance_date = Column(Date, nullable=False, index=True)
    reason = Column(String(255), nullable=True)
    explanation = Column(String(255), nullable=True)
    shift_assignment_id = Column(UUID(as_uuid=True), nullable=True)
    leave_type_id = Column(UUID(as_uuid=True), nullable=True)
    leave_id = Column(UUID(as_uuid=True), nullable=True)
    attendance_request_id = Column(UUID(as_uuid=True), nullable=True)
    status = Column(String(50), nullable=False, default="approved")
    createdAt = Column(DateTime, nullable=False, server_default=func.now())
    updatedAt = Column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
    out_date = Column(Date, nullable=True)
    explanation_out = Column(String(255), nullable=True)
    client_reference_id = Column(String(255), nullable=True, index=True)
