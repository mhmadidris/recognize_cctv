from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from app.models.cctv import CCTVCamera, CCTVCompanySetting, AttendanceCCTV
from app.utils.env import env
from uuid import UUID
from datetime import datetime, timezone
from typing import List, Optional


def _max_cameras_per_company() -> int:
    return max(0, env.CCTV_MAX_CAMERAS_PER_COMPANY or 0)


def _coerce_uuid(value):
    if value is None or isinstance(value, UUID):
        return value
    return UUID(str(value))


def _is_sqlite_session(db: Session) -> bool:
    try:
        return db.get_bind().dialect.name == "sqlite"
    except Exception:
        return False


class CCTVCameraDAO:
    @staticmethod
    def get_by_id(db: Session, camera_id: UUID) -> Optional[CCTVCamera]:
        return db.query(CCTVCamera).filter(CCTVCamera.id == camera_id).first()

    @staticmethod
    def get_by_company_id(db: Session, company_id: UUID) -> Optional[CCTVCamera]:
        return (
            db.query(CCTVCamera)
            .filter(CCTVCamera.company_id == company_id)
            .order_by(CCTVCamera.created_at.asc())
            .first()
        )

    @staticmethod
    def list_by_company_id(db: Session, company_id: UUID) -> List[CCTVCamera]:
        return (
            db.query(CCTVCamera)
            .filter(CCTVCamera.company_id == company_id)
            .order_by(CCTVCamera.created_at.asc())
            .all()
        )

    @staticmethod
    def count_by_company_id(db: Session, company_id: UUID) -> int:
        return db.query(CCTVCamera).filter(CCTVCamera.company_id == company_id).count()

    @staticmethod
    def delete_by_id(db: Session, company_id: UUID, camera_id: UUID) -> Optional[CCTVCamera]:
        camera = (
            db.query(CCTVCamera)
            .filter(
                CCTVCamera.id == camera_id,
                CCTVCamera.company_id == company_id,
            )
            .first()
        )
        if not camera:
            return None

        db.delete(camera)
        db.commit()
        return camera

    @staticmethod
    def upsert_company_source(
        db: Session,
        company_id: UUID,
        rtsp_url: str,
        name: str = "Default CCTV Camera",
    ) -> CCTVCamera:
        camera = CCTVCameraDAO.get_by_company_id(db, company_id)
        if camera:
            camera.rtsp_url = rtsp_url
            camera.updated_at = datetime.now()
        else:
            max_cameras = _max_cameras_per_company()
            current_count = CCTVCameraDAO.count_by_company_id(db, company_id)
            if max_cameras and current_count >= max_cameras:
                raise ValueError(
                    f"Maximum CCTV cameras per company is {max_cameras}."
                )

            camera = CCTVCamera(
                company_id=company_id,
                name=name,
                rtsp_url=rtsp_url,
                zone_type="attendance",
                status="offline",
            )
            db.add(camera)

        db.commit()
        db.refresh(camera)
        return camera

    @staticmethod
    def upsert_camera(
        db: Session,
        company_id: UUID,
        rtsp_url: str,
        camera_id: Optional[UUID] = None,
        name: str = "Default CCTV Camera",
        branch_id: Optional[UUID] = None,
        zone_type: str = "attendance",
        roi_enabled: Optional[bool] = None,
        roi_polygon: Optional[list[dict[str, float]]] = None,
        clahe_enabled: Optional[bool] = None,
        detection_confidence_threshold: Optional[float] = None,
        recognition_similarity_threshold: Optional[float] = None,
        reid_similarity_threshold: Optional[float] = None,
        track_confirm_hits: Optional[int] = None,
        min_face_width: Optional[int] = None,
        min_face_height: Optional[int] = None,
        min_face_brightness: Optional[float] = None,
        min_face_blur: Optional[float] = None,
        adaptive_frame_skip_enabled: Optional[bool] = None,
        target_inference_ms: Optional[int] = None,
        max_frame_skip: Optional[int] = None,
    ) -> CCTVCamera:
        camera = CCTVCameraDAO.get_by_id(db, camera_id) if camera_id else None
        if camera and camera.company_id != company_id:
            camera = None

        if camera:
            camera.rtsp_url = rtsp_url
            camera.name = name or camera.name
            camera.branch_id = branch_id
            camera.zone_type = zone_type or camera.zone_type
            if roi_enabled is not None:
                camera.roi_enabled = roi_enabled
            if roi_polygon is not None:
                camera.roi_polygon = roi_polygon
            if clahe_enabled is not None:
                camera.clahe_enabled = clahe_enabled
            for key, value in {
                "detection_confidence_threshold": detection_confidence_threshold,
                "recognition_similarity_threshold": recognition_similarity_threshold,
                "reid_similarity_threshold": reid_similarity_threshold,
                "track_confirm_hits": track_confirm_hits,
                "min_face_width": min_face_width,
                "min_face_height": min_face_height,
                "min_face_brightness": min_face_brightness,
                "min_face_blur": min_face_blur,
                "adaptive_frame_skip_enabled": adaptive_frame_skip_enabled,
                "target_inference_ms": target_inference_ms,
                "max_frame_skip": max_frame_skip,
            }.items():
                if value is not None:
                    setattr(camera, key, value)
            camera.updated_at = datetime.now()
        else:
            camera = CCTVCamera(
                company_id=company_id,
                branch_id=branch_id,
                name=name,
                rtsp_url=rtsp_url,
                zone_type=zone_type or "attendance",
                roi_enabled=bool(roi_enabled),
                roi_polygon=roi_polygon,
                clahe_enabled=bool(clahe_enabled),
                detection_confidence_threshold=detection_confidence_threshold,
                recognition_similarity_threshold=recognition_similarity_threshold,
                reid_similarity_threshold=reid_similarity_threshold,
                track_confirm_hits=track_confirm_hits,
                min_face_width=min_face_width,
                min_face_height=min_face_height,
                min_face_brightness=min_face_brightness,
                min_face_blur=min_face_blur,
                adaptive_frame_skip_enabled=bool(adaptive_frame_skip_enabled),
                target_inference_ms=target_inference_ms,
                max_frame_skip=max_frame_skip,
                status="offline",
            )
            db.add(camera)

        db.commit()
        db.refresh(camera)
        return camera

    @staticmethod
    def get_all_active(db: Session) -> List[CCTVCamera]:
        return db.query(CCTVCamera).filter(CCTVCamera.status == "online").all()

    @staticmethod
    def update_last_online(db: Session, camera_id: UUID):
        camera = db.query(CCTVCamera).filter(CCTVCamera.id == camera_id).first()
        if camera:
            camera.last_online_at = datetime.now()
            camera.status = "online"
            db.commit()

class AttendanceCCTVDAO:
    @staticmethod
    def list_records(
        db: Session,
        company_id: UUID | str,
        attendance_date,
        camera_id: Optional[UUID | str] = None,
    ) -> dict:
        query = db.query(AttendanceCCTV).filter(
            AttendanceCCTV.company_id == _coerce_uuid(company_id),
            AttendanceCCTV.attendance_date == attendance_date,
        )
        if camera_id:
            query = query.filter(AttendanceCCTV.client_reference_id == str(camera_id))

        records = query.order_by(
            AttendanceCCTV.time_in.asc().nullslast(),
            AttendanceCCTV.time_out.asc().nullslast(),
            AttendanceCCTV.employee_id.asc(),
        ).all()

        return {
            record.employee_id: AttendanceCCTVDAO.serialize_record(record)
            for record in records
        }

    @staticmethod
    def serialize_record(record: AttendanceCCTV) -> dict:
        def _to_local(t):
            if not t: return None
            return t.isoformat()

        return {
            "id": record.id,
            "company_id": record.company_id,
            "employee_id": record.employee_id,
            "employee_name": record.employee_name,
            "log_type": record.log_type,
            "time_in": _to_local(record.time_in),
            "time_out": _to_local(record.time_out),
            "photo_in": record.photo_in,
            "photo_out": record.photo_out,
            "attendance_date": record.attendance_date.isoformat() if record.attendance_date else None,
            "out_date": record.out_date.isoformat() if record.out_date else None,
            "attendance_status": record.attendance_status,
            "checkin_status": record.checkin_status,
            "checkout_status": record.checkout_status,
            "reason": record.reason,
            "explanation": record.explanation,
            "explanation_out": record.explanation_out,
            "shift_assignment_id": record.shift_assignment_id,
            "leave_type_id": record.leave_type_id,
            "leave_id": record.leave_id,
            "attendance_request_id": record.attendance_request_id,
            "status": record.status,
            "client_reference_id": record.client_reference_id,
        }

    @staticmethod
    def record_attendance(
        db: Session,
        company_id: UUID | str,
        employee_id: UUID | str,
        employee_name: Optional[str] = None,
        attendance_type: str = "in",
        detected_at: Optional[datetime] = None,
        client_reference_id: Optional[str] = None,
        photo_url: Optional[str] = None,
        address: Optional[str] = None,
        latlong: Optional[str] = None,
        attendance_policy: Optional[dict] = None,
    ) -> AttendanceCCTV:
        if attendance_type not in {"in", "out"}:
            raise ValueError("attendance_type must be 'in' or 'out'.")

        detected_at = detected_at or datetime.now(timezone.utc)
        if detected_at.tzinfo:
            detected_at = detected_at.astimezone(timezone.utc).replace(tzinfo=None)
        attendance_date = detected_at.date()
        attendance_time = detected_at.time().replace(microsecond=0)

        def find_record():
            query = (
                db.query(AttendanceCCTV)
                .filter(
                    AttendanceCCTV.company_id == _coerce_uuid(company_id),
                    AttendanceCCTV.employee_id == _coerce_uuid(employee_id),
                    AttendanceCCTV.time_out == None
                )
            )

            if attendance_type == "in":
                query = query.filter(AttendanceCCTV.attendance_date == attendance_date)
                return query.order_by(AttendanceCCTV.createdAt.desc()).first()
            
            if attendance_policy and attendance_policy.get("shift_assignment_id"):
                query = query.filter(
                    AttendanceCCTV.shift_assignment_id == _coerce_uuid(attendance_policy["shift_assignment_id"])
                )
            else:
                query = query.filter(AttendanceCCTV.attendance_date == attendance_date)
                
            return query.order_by(AttendanceCCTV.createdAt.desc()).first()

        record = find_record()

        if record is None:
            record = AttendanceCCTV(
                company_id=_coerce_uuid(company_id),
                employee_id=_coerce_uuid(employee_id),
                employee_name=employee_name,
                attendance_date=attendance_date,
                log_type=attendance_type,
                attendance_status="present",
                status="approved",
            )
            db.add(record)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                record = find_record()
                if record is None:
                    raise

        record.updatedAt = detected_at
        if employee_name:
            record.employee_name = employee_name
        if client_reference_id:
            record.client_reference_id = client_reference_id
        attendance_policy = attendance_policy or {}

        if attendance_policy.get("shift_assignment_id"):
            record.shift_assignment_id = _coerce_uuid(attendance_policy["shift_assignment_id"])
        if attendance_policy.get("leave_type_id"):
            record.leave_type_id = _coerce_uuid(attendance_policy["leave_type_id"])
        if attendance_policy.get("leave_id"):
            record.leave_id = _coerce_uuid(attendance_policy["leave_id"])
        if attendance_policy.get("attendance_request_id"):
            record.attendance_request_id = _coerce_uuid(attendance_policy["attendance_request_id"])
        if attendance_policy.get("attendance_status"):
            record.attendance_status = attendance_policy["attendance_status"]
        if attendance_policy.get("status"):
            record.status = attendance_policy["status"]
        if attendance_policy.get("reason"):
            record.reason = attendance_policy["reason"]
        if attendance_policy.get("explanation"):
            record.explanation = attendance_policy["explanation"]
        if attendance_policy.get("explanation_out"):
            record.explanation_out = attendance_policy["explanation_out"]

        if attendance_type == "in":
            is_new_attendance = record.time_in is None
            if is_new_attendance:
                record.log_type = attendance_type
                record.time_in = attendance_time
            if photo_url and (is_new_attendance or not record.photo_in):
                record.photo_in = photo_url
            if address:
                record.address_in = address
                record.address = address
            if latlong:
                record.latlong_in = latlong
            if attendance_policy.get("checkin_status"):
                record.checkin_status = attendance_policy["checkin_status"]
            elif not record.checkin_status:
                record.checkin_status = "ontime"
        else:
            is_new_attendance = record.time_out is None
            if is_new_attendance:
                record.log_type = attendance_type
                record.time_out = attendance_time
            record.out_date = attendance_date
            if record.time_in is None:
                record.attendance_status = "incomplete"
                if not record.reason:
                    record.reason = "missing_checkin"
                if not record.explanation:
                    record.explanation = "Check-out recorded before check-in."
            if photo_url and (is_new_attendance or not record.photo_out):
                record.photo_out = photo_url
            if address:
                record.address_out = address
            if latlong:
                record.latlong_out = latlong
            if attendance_policy.get("checkout_status"):
                record.checkout_status = attendance_policy["checkout_status"]
            elif not record.checkout_status:
                record.checkout_status = "ontime"

        sqlite_session = _is_sqlite_session(db)
        previous_expire_on_commit = getattr(db, "expire_on_commit", None)
        if sqlite_session and previous_expire_on_commit is not None:
            db.expire_on_commit = False
        try:
            db.commit()
            if not sqlite_session:
                db.refresh(record)
        finally:
            if sqlite_session and previous_expire_on_commit is not None:
                db.expire_on_commit = previous_expire_on_commit
        if sqlite_session:
            for uuid_field in (
                "shift_assignment_id",
                "leave_type_id",
                "leave_id",
                "attendance_request_id",
            ):
                value = getattr(record, uuid_field)
                if value is not None:
                    setattr(record, uuid_field, str(value))
        record.is_new_attendance = is_new_attendance
        return record

class CCTVCompanySettingDAO:
    @staticmethod
    def get_by_company_id(db: Session, company_id: UUID) -> Optional[CCTVCompanySetting]:
        return db.query(CCTVCompanySetting).filter(CCTVCompanySetting.company_id == company_id).first()

    @staticmethod
    def get_or_create(db: Session, company_id: UUID) -> CCTVCompanySetting:
        setting = CCTVCompanySettingDAO.get_by_company_id(db, company_id)
        if not setting:
            setting = CCTVCompanySetting(company_id=company_id)
            db.add(setting)
            db.commit()
            db.refresh(setting)
        return setting

    @staticmethod
    def update_settings(db: Session, company_id: UUID, settings_data: dict) -> Optional[CCTVCompanySetting]:
        setting = CCTVCompanySettingDAO.get_or_create(db, company_id)
        for key, value in settings_data.items():
            if value is not None and hasattr(setting, key):
                setattr(setting, key, value)
        
        db.commit()
        db.refresh(setting)
        return setting
