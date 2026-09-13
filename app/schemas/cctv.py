from pydantic import BaseModel, Field, ConfigDict
from uuid import UUID
from typing import Any, Optional
from datetime import datetime
from typing import Literal

class CCTVCompanySettingBase(BaseModel):
    enabled: bool = True
    confidence_threshold: float = Field(0.75, ge=0.0, le=1.0)
    cooldown_seconds: int = Field(30, ge=0)
    allow_attendance_after_tolerance: bool = True
    time_tolerance_minutes: int = Field(0, ge=0)
    timezone: str = Field("Asia/Jakarta", min_length=1, max_length=64)

class CCTVCompanySettingCreate(CCTVCompanySettingBase):
    company_id: UUID

class CCTVCompanySettingUpdate(BaseModel):
    enabled: Optional[bool] = None
    cooldown_seconds: Optional[int] = Field(None, ge=0)
    allow_attendance_after_tolerance: Optional[bool] = None
    time_tolerance_minutes: Optional[int] = Field(None, ge=0)
    timezone: Optional[str] = Field(None, min_length=1, max_length=64)


class CCTVRuntimeSettingUpdate(BaseModel):
    monitor_mode: Optional[Literal["", "visitor", "attendance", "both"]] = None
    detection_min_confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    similarity_threshold: Optional[float] = Field(None, ge=0.0, le=1.0)
    reid_similarity_threshold: Optional[float] = Field(None, ge=0.0, le=1.0)
    track_iou_threshold: Optional[float] = Field(None, ge=0.0, le=1.0)
    track_confirm_hits: Optional[int] = Field(None, ge=1)
    track_max_missed_frames: Optional[int] = Field(None, ge=1)
    detect_width: Optional[int] = Field(None, ge=160)
    min_face_width: Optional[int] = Field(None, ge=1)
    min_face_height: Optional[int] = Field(None, ge=1)
    min_face_brightness: Optional[float] = Field(None, ge=0.0, le=255.0)
    min_face_blur: Optional[float] = Field(None, ge=0.0)
    adaptive_frame_skip_enabled: Optional[bool] = None
    target_inference_ms: Optional[int] = Field(None, ge=1)
    max_frame_skip: Optional[int] = Field(None, ge=0, le=10)

class CCTVCameraSourceUpdate(BaseModel):
    camera_source: str = Field(..., min_length=1)
    camera_id: Optional[UUID] = None
    name: Optional[str] = None
    branch_id: Optional[UUID] = None
    zone_type: Optional[str] = Field(None, min_length=1)
    roi_enabled: Optional[bool] = None
    roi_polygon: Optional[list[dict[str, float]]] = None
    clahe_enabled: Optional[bool] = None
    detection_confidence_threshold: Optional[float] = Field(None, ge=0.0, le=1.0)
    recognition_similarity_threshold: Optional[float] = Field(None, ge=0.0, le=1.0)
    reid_similarity_threshold: Optional[float] = Field(None, ge=0.0, le=1.0)
    track_confirm_hits: Optional[int] = Field(None, ge=1)
    min_face_width: Optional[int] = Field(None, ge=1)
    min_face_height: Optional[int] = Field(None, ge=1)
    min_face_brightness: Optional[float] = Field(None, ge=0.0, le=255.0)
    min_face_blur: Optional[float] = Field(None, ge=0.0)
    adaptive_frame_skip_enabled: Optional[bool] = None
    target_inference_ms: Optional[int] = Field(None, ge=1)
    max_frame_skip: Optional[int] = Field(None, ge=0, le=10)


class CCTVStartRequest(BaseModel):
    camera_id: Optional[UUID] = None
    employee_id: Optional[UUID] = None


class WorkinAttendanceResponse(BaseModel):
    camera_id: Optional[UUID] = None
    employee_id: Optional[UUID] = None
    status: Optional[str] = Field(None, min_length=1)
    message: Optional[str] = None
    result: Optional[Any] = None
    errors: Optional[Any] = None
    data: Optional[dict[str, Any]] = None

class CCTVCompanySettingResponse(CCTVCompanySettingBase):
    id: UUID
    company_id: UUID

    model_config = ConfigDict(from_attributes=True)


class CCTVCompanySettingStatusResponse(BaseModel):
    company_id: UUID
    setting_id: Optional[UUID] = None
    setup_status: Literal["not_setup", "configured"]
    company_setting_exists: bool
    enabled: bool = True
    confidence_threshold: float = Field(0.75, ge=0.0, le=1.0)
    cooldown_seconds: int = Field(30, ge=0)
    allow_attendance_after_tolerance: bool = True
    time_tolerance_minutes: int = Field(0, ge=0)
    timezone: str = Field("Asia/Jakarta", min_length=1, max_length=64)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
