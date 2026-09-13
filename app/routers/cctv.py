import os
import time
from datetime import date
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Depends, Query, Body
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.cctv_recognition import Settings, cctv_recognition_service, hrms_configured
from app.services.rabbitmq import RabbitMQPublishError
from app.utils.response import success_response
from app.utils.database import get_db
from app.daos.cctv import AttendanceCCTVDAO, CCTVCameraDAO, CCTVCompanySettingDAO
from app.models.cctv import AttendanceCCTV
from app.schemas.cctv import (
    CCTVCameraSourceUpdate,
    CCTVCompanySettingUpdate,
    CCTVRuntimeSettingUpdate,
    CCTVStartRequest,
    WorkinAttendanceResponse,
)
from app.utils.timezone import DEFAULT_TIMEZONE, validate_timezone


router = APIRouter()


@router.get("/config")
async def cctv_config():
    return success_response(
        message="CCTV runtime configuration.",
        result={
            "attendance_policy_enabled": bool(Settings.attendance_policy_url),
        },
    )


@router.get("/runtime/settings")
async def get_runtime_settings(company_id: UUID | None = Query(default=None), db: Session = Depends(get_db)):
    if company_id:
        setting = CCTVCompanySettingDAO.get_by_company_id(db, company_id)
        if setting and setting.monitor_mode in {"", "visitor", "attendance", "both"}:
            Settings.monitor_mode = setting.monitor_mode
    return success_response(
        message="CCTV runtime settings.",
        result=_serialize_runtime_settings(),
    )


@router.post("/runtime/settings")
async def update_runtime_settings(
    settings_data: CCTVRuntimeSettingUpdate,
    company_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
):
    updates = settings_data.model_dump(exclude_unset=True)
    monitor_mode = updates.get("monitor_mode")
    if monitor_mode is not None and company_id:
        CCTVCompanySettingDAO.update_settings(db, company_id, {"monitor_mode": monitor_mode})
        # Apply the company-scoped mode immediately. Otherwise the shared
        # runtime can keep the previous attendance/both mode until the next
        # settings read and incorrectly start HRMS synchronization.
        Settings.monitor_mode = monitor_mode
    for key, value in updates.items():
        if key == "monitor_mode":
            continue
        if value is not None and hasattr(Settings, key):
            setattr(Settings, key, value)

    if monitor_mode is not None and company_id:
        if monitor_mode == "visitor":
            cctv_recognition_service.stop(company_id=company_id)
        elif monitor_mode == "attendance":
            from app.services.event_visitor import event_visitor_manager
            event_visitor_manager.stop(str(company_id))
        elif monitor_mode == "":
            from app.services.event_visitor import event_visitor_manager
            cctv_recognition_service.stop(company_id=company_id)
            event_visitor_manager.stop(str(company_id))

    return success_response(
        message="CCTV runtime settings updated.",
        result=_serialize_runtime_settings(),
    )


@router.post("/runtime/settings/reset")
async def reset_runtime_settings(company_id: UUID | None = Query(default=None), db: Session = Depends(get_db)):
    defaults = _runtime_setting_defaults()
    for key, value in defaults.items():
        setattr(Settings, key, value)
    if company_id:
        CCTVCompanySettingDAO.update_settings(db, company_id, {"monitor_mode": defaults["monitor_mode"]})
    return success_response(
        message="CCTV runtime settings reset to defaults.",
        result=_serialize_runtime_settings(),
    )


@router.get("/health")
async def cctv_health(db: Session = Depends(get_db)):
    checks = _health_checks(db)
    return success_response(
        message="CCTV health check.",
        result={
            "ok": all(check["ok"] for check in checks.values() if check["required"]),
            "checks": checks,
        },
    )


@router.post("/start")
async def start_cctv_recognition(
    start_data: CCTVStartRequest | None = Body(default=None),
    camera_id: UUID | None = Query(default=None),
    company_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
):
    resolved_camera_id = start_data.camera_id if start_data and start_data.camera_id else camera_id
    try:
        if not company_id:
            raise ValueError("company_id is required for company-scoped monitoring")
        company_setting = CCTVCompanySettingDAO.get_by_company_id(db, company_id) if company_id else None
        monitor_mode = company_setting.monitor_mode if company_setting else Settings.monitor_mode
        # Keep the shared runtime aligned with the company-scoped mode before
        # any runtime preparation can happen.
        Settings.monitor_mode = monitor_mode
        attendance_result = None
        visitor_result = None
        if monitor_mode in {"attendance", "both"} and resolved_camera_id:
            start_kwargs = {
                "camera_id": resolved_camera_id,
                "employee_id": start_data.employee_id if start_data else None,
            }
            start_kwargs["company_id"] = company_id
            attendance_result = cctv_recognition_service.start(**start_kwargs)
        elif monitor_mode in {"attendance", "both"}:
            cctv_recognition_service.start_system(company_id=company_id)
            attendance_result = cctv_recognition_service.start(company_id=company_id)
        if monitor_mode in {"visitor", "both"}:
            from app.services.event_visitor import event_visitor_manager
            if not company_id:
                raise ValueError("company_id is required for Event Visitor monitoring")
            visitor_result = event_visitor_manager.start(str(company_id))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except RabbitMQPublishError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error

    result = attendance_result if monitor_mode == "attendance" else {
        "monitor_mode": monitor_mode,
        "attendance": attendance_result,
        "visitor": visitor_result,
    }
    return success_response(
        message="Selected computer vision monitoring services are starting.",
        result=result,
    )


@router.post("/prepare")
async def prepare_cctv_recognition():
    return success_response(
        message="CCTV recognition runtime is preparing.",
        result=cctv_recognition_service.prepare_system(),
    )


@router.post("/workin/attendance/in/response")
async def receive_workin_attendance_in_response(response_data: WorkinAttendanceResponse):
    return success_response(
        message="Workin check-in response received.",
        result=cctv_recognition_service.record_workin_response("in", response_data.model_dump(mode="json")),
    )


@router.post("/workin/attendance/out/response")
async def receive_workin_attendance_out_response(response_data: WorkinAttendanceResponse):
    return success_response(
        message="Workin check-out response received.",
        result=cctv_recognition_service.record_workin_response("out", response_data.model_dump(mode="json")),
    )


@router.post("/stop")
async def stop_cctv_recognition(
    camera_id: UUID | None = Query(default=None),
    company_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if company_id is None:
        raise HTTPException(status_code=422, detail="company_id is required.")
    from app.services.event_visitor import event_visitor_manager
    visitor_result = event_visitor_manager.stop(str(company_id))
    monitor_mode = Settings.monitor_mode
    if company_id:
        setting = CCTVCompanySettingDAO.get_by_company_id(db, company_id)
        monitor_mode = setting.monitor_mode if setting else monitor_mode
    return success_response(
        message="Selected computer vision monitoring services stopped.",
        result={
            "monitor_mode": monitor_mode,
            "attendance": (
            cctv_recognition_service.stop(camera_id=camera_id, company_id=company_id)
            if camera_id or company_id
            else cctv_recognition_service.stop_system()
            ),
            "visitor": visitor_result,
        },
    )


@router.get("/status")
async def cctv_recognition_status(
    camera_id: UUID | None = Query(default=None),
    company_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
):
    if company_id is None:
        raise HTTPException(status_code=422, detail="company_id is required.")
    from app.services.event_visitor import event_visitor_manager
    result = cctv_recognition_service.status(camera_id=camera_id, company_id=company_id)
    setting = CCTVCompanySettingDAO.get_by_company_id(db, company_id)
    result["monitor_mode"] = setting.monitor_mode if setting else Settings.monitor_mode
    result["visitor_monitor"] = event_visitor_manager.status(str(company_id))
    return success_response(
        message="CCTV recognition worker status.",
        result=result,
    )


@router.post("/source")
async def update_cctv_camera_source(source_data: CCTVCameraSourceUpdate):
    try:
        result = cctv_recognition_service.set_camera_source(
            source_data.camera_source,
            camera_id=source_data.camera_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    return success_response(
        message="CCTV camera source updated.",
        result=result,
    )


@router.get("/source/{company_id}")
async def get_company_camera_source(company_id: UUID, db: Session = Depends(get_db)):
    camera = CCTVCameraDAO.get_by_company_id(db, company_id)
    if camera and str(company_id) == str(cctv_recognition_service.status()["company_id"]):
        cctv_recognition_service.set_camera_source(camera.rtsp_url, camera_id=camera.id)

    return success_response(
        message="Company CCTV camera source retrieved.",
        result=_serialize_camera(camera) if camera else None,
    )


@router.post("/source/{company_id}")
async def update_company_camera_source(
    company_id: UUID,
    source_data: CCTVCameraSourceUpdate,
    db: Session = Depends(get_db),
):
    camera_source = source_data.camera_source.strip()
    if not camera_source:
        raise HTTPException(status_code=422, detail="Camera source cannot be empty.")

    try:
        camera = CCTVCameraDAO.upsert_camera(
            db,
            company_id,
            camera_source,
            camera_id=source_data.camera_id,
            name=source_data.name or "Default CCTV Camera",
            branch_id=source_data.branch_id,
            zone_type=source_data.zone_type or "attendance",
            roi_enabled=source_data.roi_enabled,
            roi_polygon=_normalize_roi_polygon(source_data.roi_polygon),
            clahe_enabled=source_data.clahe_enabled,
            detection_confidence_threshold=source_data.detection_confidence_threshold,
            recognition_similarity_threshold=source_data.recognition_similarity_threshold,
            reid_similarity_threshold=source_data.reid_similarity_threshold,
            track_confirm_hits=source_data.track_confirm_hits,
            min_face_width=source_data.min_face_width,
            min_face_height=source_data.min_face_height,
            min_face_brightness=source_data.min_face_brightness,
            min_face_blur=source_data.min_face_blur,
            adaptive_frame_skip_enabled=source_data.adaptive_frame_skip_enabled,
            target_inference_ms=source_data.target_inference_ms,
            max_frame_skip=source_data.max_frame_skip,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error

    runtime_result = None
    if str(company_id) == str(cctv_recognition_service.status()["company_id"]):
        runtime_result = cctv_recognition_service.set_camera_source(camera_source, camera_id=camera.id)

    return success_response(
        message="Company CCTV camera source saved.",
        result={
            "camera": _serialize_camera(camera),
            "runtime": runtime_result,
        },
    )


@router.get("/stream")
async def cctv_recognition_stream(
    camera_id: UUID | None = Query(default=None),
    company_id: UUID | None = Query(default=None),
):
    if company_id is None:
        raise HTTPException(status_code=422, detail="company_id is required.")
    status = cctv_recognition_service.status(camera_id=camera_id, company_id=company_id)
    if not status["running"]:
        raise HTTPException(
            status_code=409,
            detail="CCTV recognition worker is not running. Start it first with POST /api/v1/cctv/start.",
        )

    return StreamingResponse(
        _frame_stream(camera_id=camera_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/attendance")
async def cctv_attendance_records(
    company_id: str | None = Query(default=None),
    camera_id: UUID | None = Query(default=None),
    attendance_date: date | None = Query(default=None, alias="date"),
    source: str = Query(default="db"),
    db: Session = Depends(get_db),
):
    if source == "session":
        result = cctv_recognition_service.attendance_records(camera_id=camera_id)
    elif source == "db":
        if not company_id:
            company_id = cctv_recognition_service.status().get("company_id")
        if not company_id:
            raise HTTPException(status_code=422, detail="company_id is required.")
        result = AttendanceCCTVDAO.list_records(
            db,
            company_id=company_id,
            attendance_date=attendance_date or date.today(),
            camera_id=camera_id,
        )
    else:
        raise HTTPException(status_code=422, detail="source must be 'db' or 'session'.")

    return success_response(
        message="CCTV attendance records.",
        result=result,
    )

@router.delete("/attendance")
async def clear_all_attendance(company_id: str, db: Session = Depends(get_db)):
    if not company_id:
        raise HTTPException(status_code=422, detail="company_id is required.")

    records = db.query(AttendanceCCTV).filter(AttendanceCCTV.company_id == company_id).all()
    import os
    from pathlib import Path
    PROJECT_ROOT = Path(__file__).resolve().parents[2]

    deleted_count = 0
    for record in records:
        for photo_url in [record.photo_in, record.photo_out]:
            if photo_url and photo_url.startswith("/static/"):
                try:
                    rel_path = photo_url.replace("/static/", "")
                    file_path = PROJECT_ROOT / "app" / "static" / rel_path
                    if file_path.exists():
                        os.remove(file_path)
                except Exception:
                    pass
        db.delete(record)
        deleted_count += 1

    db.commit()

    cctv_recognition_service._shared_attendance_service.reset()
    return success_response(
        message=f"Cleared {deleted_count} attendance records and snapshots.",
        result=None
    )


@router.delete("/attendance/{attendance_id}")
async def delete_attendance(attendance_id: str, db: Session = Depends(get_db)):
    record = db.query(AttendanceCCTV).filter(AttendanceCCTV.id == attendance_id).first()
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found.")

    import os
    from pathlib import Path
    
    PROJECT_ROOT = Path(__file__).resolve().parents[2]
    
    for photo_url in [record.photo_in, record.photo_out]:
        if photo_url and photo_url.startswith("/static/"):
            try:
                rel_path = photo_url.replace("/static/", "")
                file_path = PROJECT_ROOT / "app" / "static" / rel_path
                if file_path.exists():
                    os.remove(file_path)
            except Exception:
                pass

    attendance_service = cctv_recognition_service._shared_attendance_service
    with attendance_service._lock:
        emp_id = str(record.employee_id)
        if emp_id in attendance_service.session_attendance:
            del attendance_service.session_attendance[emp_id]

    db.delete(record)
    db.commit()

    return success_response(message="Attendance record and images deleted.", result=None)


@router.get("/cameras/{company_id}")
async def list_company_cameras(company_id: UUID, db: Session = Depends(get_db)):
    cameras = CCTVCameraDAO.list_by_company_id(db, company_id)
    return success_response(
        message="Company CCTV cameras retrieved.",
        result=[_serialize_camera(camera) for camera in cameras],
    )


@router.delete("/cameras/{company_id}/{camera_id}")
async def delete_company_camera(
    company_id: UUID,
    camera_id: UUID,
    db: Session = Depends(get_db),
):
    camera = CCTVCameraDAO.delete_by_id(db, company_id, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Camera not found.")

    cctv_recognition_service.remove_camera(camera_id)

    return success_response(
        message="Company CCTV camera deleted.",
        result={"id": str(camera_id)},
    )


@router.get("/settings/{company_id}")
async def get_cctv_settings(company_id: UUID, db: Session = Depends(get_db)):
    settings = CCTVCompanySettingDAO.get_by_company_id(db, company_id)
    return success_response(
        message="CCTV settings retrieved.",
        result=_serialize_company_settings(
            settings,
            company_id=company_id,
            is_first_setup=settings is None,
        ),
    )


@router.post("/settings/{company_id}")
async def update_cctv_settings(
    company_id: UUID, 
    settings_data: CCTVCompanySettingUpdate, 
    db: Session = Depends(get_db)
):
    data = settings_data.model_dump(exclude_unset=True)
    if "timezone" in data:
        try:
            data["timezone"] = validate_timezone(data["timezone"])
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error))
    settings = CCTVCompanySettingDAO.update_settings(
        db, 
        company_id, 
        data
    )
    runtime_result = None
    if settings.enabled:
        runtime_result = cctv_recognition_service.prepare_system()
    else:
        cctv_recognition_service.stop(company_id=company_id)
        from app.services.event_visitor import event_visitor_manager
        event_visitor_manager.stop(str(company_id))

    result = _serialize_company_settings(settings)
    result["runtime"] = runtime_result
    return success_response(
        message="CCTV settings updated successfully.",
        result=result,
    )


def _serialize_company_settings(settings, company_id=None, is_first_setup=False) -> dict:
    resolved_company_id = settings.company_id if settings else company_id
    return {
        "setting_id": str(settings.id) if settings else None,
        "company_id": str(resolved_company_id) if resolved_company_id else None,
        "setup_status": "not_setup" if is_first_setup else "configured",
        "company_setting_exists": settings is not None,
        "enabled": settings.enabled if settings else True,
        "confidence_threshold": settings.confidence_threshold if settings else 0.75,
        "cooldown_seconds": settings.cooldown_seconds if settings else 30,
        "allow_attendance_after_tolerance": getattr(settings, "allow_attendance_after_tolerance", True) if settings else True,
        "time_tolerance_minutes": getattr(settings, "time_tolerance_minutes", 0) if settings else 0,
        "timezone": getattr(settings, "timezone", DEFAULT_TIMEZONE) if settings else DEFAULT_TIMEZONE,
        "created_at": settings.created_at.isoformat() if settings and settings.created_at else None,
        "updated_at": settings.updated_at.isoformat() if settings and settings.updated_at else None,
    }


def _runtime_setting_defaults():
    return {
        "monitor_mode": "",
        "detection_min_confidence": 0.5,
        "similarity_threshold": 0.80,
        "reid_similarity_threshold": 0.82,
        "track_iou_threshold": 0.35,
        "track_confirm_hits": 5,
        "track_max_missed_frames": 12,
        "detect_width": 480,
        "min_face_width": 32,
        "min_face_height": 32,
        "min_face_brightness": 28.0,
        "min_face_blur": 18.0,
        "adaptive_frame_skip_enabled": False,
        "target_inference_ms": 120,
        "max_frame_skip": 3,
    }


def _serialize_runtime_settings():
    return {
        **{
            key: getattr(Settings, key)
            for key in _runtime_setting_defaults().keys()
        },
        "face_model_path": Settings.model_path,
        "vector_db_path": Settings.vector_db_path,
        "vector_db_collection": Settings.vector_db_collection,
        "attendance_photos_url": Settings.attendance_photos_url,
        "gender_model_path": os.getenv("EVENT_VISITOR_GENDER_MODEL_PATH", ""),
        "gender_model_config_path": os.getenv("EVENT_VISITOR_GENDER_CONFIG_PATH", ""),
        "person_model_path": os.getenv("EVENT_VISITOR_PERSON_MODEL_PATH", ""),
        "rabbitmq_url_configured": bool(os.getenv("RABBITMQ_URL", "")),
    }


def _path_check(path_value, required=True):
    if not path_value:
        return {
            "ok": not required,
            "required": required,
            "message": "Not configured" if required else "Optional",
        }
    path = Path(path_value)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    return {
        "ok": path.exists(),
        "required": required,
        "message": str(path),
    }


def _health_checks(db):
    checks = {}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = {"ok": True, "required": True, "message": "Reachable"}
    except Exception as error:
        checks["database"] = {"ok": False, "required": True, "message": str(error)}

    checks["face_model"] = _path_check(Settings.model_path, required=True)
    checks["gender_model"] = _path_check(os.getenv("EVENT_VISITOR_GENDER_MODEL_PATH", ""), required=False)
    checks["gender_model_config"] = _path_check(os.getenv("EVENT_VISITOR_GENDER_CONFIG_PATH", ""), required=False)
    checks["person_model"] = _path_check(
        os.getenv("EVENT_VISITOR_PERSON_MODEL_PATH", ""),
        required=False,
    )
    checks["vector_db"] = {
        "ok": bool(Settings.vector_db_path),
        "required": bool(Settings.recognition_enabled),
        "message": Settings.vector_db_path or "Not configured",
    }
    hrms_required = hrms_configured()
    checks["hrms_photos"] = {
        "ok": bool(Settings.attendance_photos_url) if hrms_required else True,
        "required": hrms_required,
        "message": (
            "Configured" if Settings.attendance_photos_url
            else "Not configured"
        ) if hrms_required else "Not required for visitor-only monitoring",
    }
    checks["rabbitmq"] = {
        "ok": bool(os.getenv("RABBITMQ_URL", "")),
        "required": False,
        "message": "Configured" if os.getenv("RABBITMQ_URL", "") else "Not configured",
    }
    runtime_status = cctv_recognition_service.status().get("runtime", {})
    checks["runtime"] = {
        "ok": not bool(runtime_status.get("error")),
        "required": False,
        "message": runtime_status.get("error") or "No runtime error",
    }
    return checks


def _serialize_camera(camera):
    return {
        "id": str(camera.id),
        "company_id": str(camera.company_id),
        "branch_id": str(camera.branch_id) if camera.branch_id else None,
        "name": camera.name,
        "camera_source": camera.rtsp_url,
        "rtsp_url": camera.rtsp_url,
        "zone_type": camera.zone_type,
        "roi_enabled": getattr(camera, "roi_enabled", False),
        "roi_polygon": getattr(camera, "roi_polygon", None) or [],
        "clahe_enabled": getattr(camera, "clahe_enabled", False),
        "detection_confidence_threshold": getattr(camera, "detection_confidence_threshold", None),
        "recognition_similarity_threshold": getattr(camera, "recognition_similarity_threshold", None),
        "reid_similarity_threshold": getattr(camera, "reid_similarity_threshold", None),
        "track_confirm_hits": getattr(camera, "track_confirm_hits", None),
        "min_face_width": getattr(camera, "min_face_width", None),
        "min_face_height": getattr(camera, "min_face_height", None),
        "min_face_brightness": getattr(camera, "min_face_brightness", None),
        "min_face_blur": getattr(camera, "min_face_blur", None),
        "adaptive_frame_skip_enabled": getattr(camera, "adaptive_frame_skip_enabled", False),
        "target_inference_ms": getattr(camera, "target_inference_ms", None),
        "max_frame_skip": getattr(camera, "max_frame_skip", None),
        "status": camera.status,
        "last_online_at": camera.last_online_at.isoformat() if camera.last_online_at else None,
        "created_at": camera.created_at.isoformat() if camera.created_at else None,
        "updated_at": camera.updated_at.isoformat() if camera.updated_at else None,
    }


def _normalize_roi_polygon(points):
    if points is None:
        return None
    if not isinstance(points, list):
        raise HTTPException(status_code=422, detail="ROI polygon must be a list of points.")
    if len(points) == 0:
        return []
    if len(points) < 3:
        raise HTTPException(status_code=422, detail="ROI polygon must contain at least 3 points.")

    normalized = []
    for point in points:
        if not isinstance(point, dict):
            raise HTTPException(status_code=422, detail="ROI polygon points must be objects with x and y.")
        try:
            x = float(point["x"])
            y = float(point["y"])
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="ROI polygon points must include numeric x and y.") from error
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise HTTPException(status_code=422, detail="ROI polygon coordinates must be normalized from 0 to 1.")
        normalized.append({"x": x, "y": y})
    return normalized


def _frame_stream(camera_id=None):
    while True:
        frame = cctv_recognition_service.latest_frame_jpeg(camera_id=camera_id)
        if frame is None:
            time.sleep(0.1)
            continue

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n"
            + frame
            + b"\r\n"
        )
        time.sleep(0.03)
