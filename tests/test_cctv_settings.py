from datetime import date, datetime, time, timezone
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.daos.cctv import AttendanceCCTVDAO, CCTVCameraDAO, CCTVCompanySettingDAO
from app.models.cctv import AttendanceCCTV
from app.services.cctv_recognition import (
    AttendancePolicyService,
    AttendanceService,
    CCTVRecognitionService,
    Settings,
)
from app.services.rabbitmq import RabbitMQPublishError
from app.utils.database import get_db
from main import app


def _override_db():
    yield object()


app.dependency_overrides[get_db] = _override_db
client = TestClient(app)


class TestCCTVSettings(TestCase):
    def test_cctv_flow_page_is_available(self):
        response = client.get("/cctv-flow")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Usulan perombakan flow absensi CCTV", response.text)

    def test_get_cctv_config_returns_runtime_flags(self):
        response = client.get("/api/v1/cctv/config")

        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertNotIn("company_id", result)
        self.assertIn("attendance_policy_enabled", result)

    def test_runtime_settings_can_be_updated_and_reset(self):
        original_detection = Settings.detection_min_confidence
        original_frame_skip = Settings.adaptive_frame_skip_enabled
        try:
            response = client.post(
                "/api/v1/cctv/runtime/settings",
                json={
                    "detection_min_confidence": 0.67,
                    "adaptive_frame_skip_enabled": True,
                    "max_frame_skip": 4,
                },
            )

            self.assertEqual(response.status_code, 200)
            result = response.json()["result"]
            self.assertEqual(result["detection_min_confidence"], 0.67)
            self.assertTrue(result["adaptive_frame_skip_enabled"])
            self.assertEqual(result["max_frame_skip"], 4)
            self.assertEqual(Settings.detection_min_confidence, 0.67)

            reset_response = client.post("/api/v1/cctv/runtime/settings/reset")

            self.assertEqual(reset_response.status_code, 200)
            reset_result = reset_response.json()["result"]
            self.assertEqual(reset_result["detection_min_confidence"], 0.5)
            self.assertFalse(reset_result["adaptive_frame_skip_enabled"])
        finally:
            Settings.detection_min_confidence = original_detection
            Settings.adaptive_frame_skip_enabled = original_frame_skip

    def test_cctv_health_returns_dependency_checks(self):
        response = client.get("/api/v1/cctv/health")

        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertIn("ok", result)
        self.assertIn("database", result["checks"])
        self.assertIn("runtime", result["checks"])

    def test_get_cctv_settings_returns_not_setup_when_company_setting_missing(self):
        company_id = "c8f745e0-aa6e-458b-bb70-4dda3e2accea"

        with patch.object(
            CCTVCompanySettingDAO,
            "get_by_company_id",
            return_value=None,
        ), patch.object(
            CCTVCompanySettingDAO,
            "get_or_create",
            side_effect=AssertionError("get_or_create must not be called for read-only settings lookup"),
        ):
            response = client.get(f"/api/v1/cctv/settings/{company_id}")

        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertEqual(result["company_id"], company_id)
        self.assertIsNone(result["setting_id"])
        self.assertEqual(result["setup_status"], "not_setup")
        self.assertFalse(result["company_setting_exists"])
        self.assertTrue(result["enabled"])
        self.assertEqual(result["confidence_threshold"], 0.75)
        self.assertEqual(result["cooldown_seconds"], 30)

    def test_get_cctv_settings_returns_configured_when_company_setting_exists(self):
        company_id = "c8f745e0-aa6e-458b-bb70-4dda3e2accea"
        created_at = datetime(2026, 6, 24, 10, 0, tzinfo=timezone.utc)
        updated_at = datetime(2026, 6, 24, 11, 0, tzinfo=timezone.utc)
        setting = SimpleNamespace(
            id="11111111-1111-1111-1111-111111111111",
            company_id=company_id,
            enabled=False,
            confidence_threshold=0.82,
            cooldown_seconds=45,
            created_at=created_at,
            updated_at=updated_at,
        )

        with patch.object(
            CCTVCompanySettingDAO,
            "get_by_company_id",
            return_value=setting,
        ):
            response = client.get(f"/api/v1/cctv/settings/{company_id}")

        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertEqual(result["company_id"], company_id)
        self.assertEqual(result["setting_id"], setting.id)
        self.assertEqual(result["setup_status"], "configured")
        self.assertTrue(result["company_setting_exists"])
        self.assertFalse(result["enabled"])
        self.assertEqual(result["confidence_threshold"], 0.82)
        self.assertEqual(result["cooldown_seconds"], 45)
        self.assertEqual(result["created_at"], created_at.isoformat())
        self.assertEqual(result["updated_at"], updated_at.isoformat())

    def test_post_cctv_settings_passes_payload_through(self):
        company_id = "c8f745e0-aa6e-458b-bb70-4dda3e2accea"
        captured = {}
        created_at = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)
        updated_at = datetime(2026, 6, 24, 12, 30, tzinfo=timezone.utc)
        setting = SimpleNamespace(
            id="22222222-2222-2222-2222-222222222222",
            company_id=company_id,
            enabled=True,
            confidence_threshold=0.9,
            cooldown_seconds=10,
            created_at=created_at,
            updated_at=updated_at,
        )

        def fake_update_settings(db, company_id_value, settings_data):
            captured["company_id"] = company_id_value
            captured["settings_data"] = settings_data
            return setting

        service = Mock()
        service.prepare_system.return_value = {"runtime_ready": False, "runtime_initializing": True}

        with patch.object(
            CCTVCompanySettingDAO,
            "update_settings",
            side_effect=fake_update_settings,
        ), patch("app.routers.cctv.cctv_recognition_service", service):
            response = client.post(
                f"/api/v1/cctv/settings/{company_id}",
                json={
                    "enabled": True,
                    "cooldown_seconds": 10,
                },
            )

        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertEqual(str(captured["company_id"]), company_id)
        self.assertEqual(
            captured["settings_data"],
            {
                "enabled": True,
                "cooldown_seconds": 10,
            },
        )
        self.assertEqual(result["setup_status"], "configured")
        self.assertTrue(result["company_setting_exists"])
        self.assertEqual(result["setting_id"], setting.id)
        self.assertEqual(result["runtime"], {"runtime_ready": False, "runtime_initializing": True})
        service.prepare_system.assert_called_once_with()


class TestCCTVWorkinIntegration(TestCase):
    def test_start_without_camera_starts_system_only(self):
        service = Mock()
        service.start_system.return_value = {
            "system_started": True,
            "running": False,
        }

        with patch("app.routers.cctv.cctv_recognition_service", service):
            response = client.post("/api/v1/cctv/start")

        self.assertEqual(response.status_code, 200)
        service.start_system.assert_called_once_with()
        service.start.assert_not_called()
        self.assertTrue(response.json()["result"]["system_started"])

    def test_start_accepts_body_camera_and_employee_ids(self):
        camera_id = "33333333-3333-3333-3333-333333333333"
        employee_id = "44444444-4444-4444-4444-444444444444"
        service = Mock()
        service.start.return_value = {
            "running": True,
            "camera_start_event": {"message_id": "message-1"},
        }

        with patch("app.routers.cctv.cctv_recognition_service", service):
            response = client.post(
                "/api/v1/cctv/start",
                json={"camera_id": camera_id, "employee_id": employee_id},
            )

        self.assertEqual(response.status_code, 200)
        service.start.assert_called_once_with(
            camera_id=UUID(camera_id),
            employee_id=UUID(employee_id),
        )
        self.assertEqual(
            response.json()["result"]["camera_start_event"],
            {"message_id": "message-1"},
        )

    def test_start_returns_502_when_rabbitmq_publish_fails(self):
        service = Mock()
        service.start.side_effect = RabbitMQPublishError("publish failed")

        with patch("app.routers.cctv.cctv_recognition_service", service):
            response = client.post(
                "/api/v1/cctv/start",
                json={
                    "camera_id": "33333333-3333-3333-3333-333333333333",
                    "employee_id": "44444444-4444-4444-4444-444444444444",
                },
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("publish failed", response.text)

    def test_workin_attendance_response_is_recorded(self):
        service = Mock()
        service.record_workin_response.return_value = {
            "attendance_type": "in",
            "payload": {"status": "processed"},
        }

        with patch("app.routers.cctv.cctv_recognition_service", service):
            response = client.post(
                "/api/v1/cctv/workin/attendance/in/response",
                json={"status": "processed"},
            )

        self.assertEqual(response.status_code, 200)
        service.record_workin_response.assert_called_once_with(
            "in",
            {
                "camera_id": None,
                "employee_id": None,
                "status": "processed",
                "message": None,
                "result": None,
                "errors": None,
                "data": None,
            },
        )

    def test_attendance_endpoint_reads_records_from_database_by_default(self):
        service = Mock()
        service.status.return_value = {"company_id": "c8f745e0-aa6e-458b-bb70-4dda3e2accea"}
        record = SimpleNamespace(
            id="attendance-1",
            company_id="c8f745e0-aa6e-458b-bb70-4dda3e2accea",
            employee_id="44444444-4444-4444-4444-444444444444",
            employee_name="Muhammad Idris",
            log_type="in",
            time_in=time(8, 0),
            time_out=None,
            photo_in="/static/in.png",
            photo_out=None,
            attendance_date=date(2026, 9, 1),
            out_date=None,
            attendance_status="present",
            checkin_status="ontime",
            checkout_status=None,
            reason=None,
            explanation=None,
            explanation_out=None,
            shift_assignment_id="55555555-5555-5555-5555-555555555555",
            leave_type_id=None,
            leave_id=None,
            attendance_request_id=None,
            status="approved",
            client_reference_id="33333333-3333-3333-3333-333333333333",
        )

        with patch("app.routers.cctv.cctv_recognition_service", service), patch.object(
            AttendanceCCTVDAO,
            "list_records",
            return_value={record.employee_id: AttendanceCCTVDAO.serialize_record(record)},
        ) as list_records:
            response = client.get("/api/v1/cctv/attendance?date=2026-09-01")

        self.assertEqual(response.status_code, 200)
        result = response.json()["result"]
        self.assertEqual(result[record.employee_id]["time_in"], "08:00:00")
        self.assertEqual(result[record.employee_id]["employee_name"], "Muhammad Idris")
        list_records.assert_called_once()
        service.attendance_records.assert_not_called()

    def test_attendance_endpoint_can_read_session_when_requested(self):
        service = Mock()
        service.attendance_records.return_value = {
            "44444444-4444-4444-4444-444444444444": {
                "employee_id": "44444444-4444-4444-4444-444444444444",
                "employee_name": "Muhammad Idris",
                "time_in": "08:00:00",
            }
        }

        with patch("app.routers.cctv.cctv_recognition_service", service), patch.object(
            AttendanceCCTVDAO,
            "list_records",
            side_effect=AssertionError("DB should not be queried for session source"),
        ):
            response = client.get("/api/v1/cctv/attendance?source=session")

        self.assertEqual(response.status_code, 200)
        service.attendance_records.assert_called_once_with(camera_id=None)

    def test_start_resolves_camera_before_publish(self):
        service = CCTVRecognitionService()
        publisher = Mock()
        service._rabbitmq_publisher = publisher
        service._system_started = True

        with patch.object(service._runtime, "status", return_value={"loaded": True}), patch.object(
            service,
            "_has_active_workers",
            return_value=True,
        ), patch.object(
            service,
            "_resolve_camera_configs",
            side_effect=ValueError("Camera not found."),
        ):
            with self.assertRaises(ValueError):
                service.start(
                    camera_id=UUID("33333333-3333-3333-3333-333333333333"),
                    employee_id=UUID("44444444-4444-4444-4444-444444444444"),
                )

        publisher.publish_camera_start.assert_not_called()


class TestAttendanceBestPractices(TestCase):
    def test_attendance_policy_service_resolves_employee_shift_policy(self):
        response = Mock()
        response.json.return_value = {
            "result": {
                "shift_assignment_id": "55555555-5555-5555-5555-555555555555",
                "attendance_status": "present",
                "checkin_status": "late",
                "reason": "late_checkin",
                "explanation": "Check-in melewati grace period 10 menit",
            }
        }
        response.raise_for_status.return_value = None
        requests_mock = Mock()
        requests_mock.post.return_value = response

        service = AttendancePolicyService(
            policy_url="https://hrms.test/attendance/policy",
            token="token-1",
        )

        with patch("app.services.cctv_recognition.requests", requests_mock):
            policy = service.resolve(
                company_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                employee_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                attendance_type="in",
                detected_at=datetime(2026, 9, 1, 8, 15, tzinfo=timezone.utc),
                branch_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
                camera_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
            )

        self.assertEqual(policy["shift_assignment_id"], "55555555-5555-5555-5555-555555555555")
        self.assertEqual(policy["checkin_status"], "late")
        requests_mock.post.assert_called_once()

    def test_session_attendance_is_keyed_by_employee_id_not_name(self):
        service = AttendanceService()
        detected_at = datetime(2026, 9, 1, 2, 30, tzinfo=timezone.utc)

        _, first_is_new = service.mark(
            "11111111-1111-1111-1111-111111111111",
            "Budi",
            "in",
            detected_at=detected_at,
        )
        _, second_is_new = service.mark(
            "22222222-2222-2222-2222-222222222222",
            "Budi",
            "in",
            detected_at=detected_at,
        )

        records = service.records()
        employee_ids = [record["employee_id"] for record in records.values()]
        self.assertTrue(first_is_new)
        self.assertTrue(second_is_new)
        self.assertEqual(len(records), 2)
        self.assertIn("11111111-1111-1111-1111-111111111111", employee_ids)
        self.assertIn("22222222-2222-2222-2222-222222222222", employee_ids)

    def test_checkout_without_checkin_is_marked_incomplete(self):
        engine = create_engine("sqlite:///:memory:")
        AttendanceCCTV.__table__.create(engine)
        Session = sessionmaker(bind=engine)
        db = Session()

        try:
            record = AttendanceCCTVDAO.record_attendance(
                db=db,
                company_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                employee_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                attendance_type="out",
                detected_at=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc),
            )
        finally:
            db.close()

        self.assertIsNone(record.time_in)
        self.assertIsNotNone(record.time_out)
        self.assertEqual(record.attendance_status, "incomplete")
        self.assertEqual(record.reason, "missing_checkin")

    def test_repeated_checkin_is_not_marked_as_new_attendance(self):
        engine = create_engine("sqlite:///:memory:")
        AttendanceCCTV.__table__.create(engine)
        Session = sessionmaker(bind=engine)
        db = Session()

        try:
            first_record = AttendanceCCTVDAO.record_attendance(
                db=db,
                company_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                employee_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                employee_name="Muhammad Idris",
                attendance_type="in",
                detected_at=datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc),
            )
            first_is_new_attendance = first_record.is_new_attendance
            second_record = AttendanceCCTVDAO.record_attendance(
                db=db,
                company_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                employee_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                employee_name="Muhammad Idris",
                attendance_type="in",
                detected_at=datetime(2026, 9, 1, 8, 5, tzinfo=timezone.utc),
            )
            second_is_new_attendance = second_record.is_new_attendance
        finally:
            db.close()

        self.assertTrue(first_is_new_attendance)
        self.assertFalse(second_is_new_attendance)
        self.assertEqual(first_record.id, second_record.id)
        self.assertEqual(str(second_record.time_in), "08:00:00")

    def test_attendance_record_saves_shift_policy_fields(self):
        engine = create_engine("sqlite:///:memory:")
        AttendanceCCTV.__table__.create(engine)
        Session = sessionmaker(bind=engine)
        db = Session()

        try:
            record = AttendanceCCTVDAO.record_attendance(
                db=db,
                company_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                employee_id="bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                employee_name="Muhammad Idris",
                attendance_type="in",
                detected_at=datetime(2026, 9, 1, 8, 15, tzinfo=timezone.utc),
                attendance_policy={
                    "shift_assignment_id": "55555555-5555-5555-5555-555555555555",
                    "attendance_status": "present",
                    "checkin_status": "late",
                    "reason": "late_checkin",
                    "explanation": "Check-in melewati grace period 10 menit",
                },
            )
        finally:
            db.close()

        self.assertEqual(record.shift_assignment_id, "55555555-5555-5555-5555-555555555555")
        self.assertEqual(record.employee_name, "Muhammad Idris")
        self.assertEqual(record.attendance_status, "present")
        self.assertEqual(record.checkin_status, "late")
        self.assertEqual(record.reason, "late_checkin")


class TestCCTVCameraSettings(TestCase):
    def test_create_camera_passes_ai_optimization_payload(self):
        company_id = "c8f745e0-aa6e-458b-bb70-4dda3e2accea"
        camera = SimpleNamespace(
            id="55555555-5555-5555-5555-555555555555",
            company_id=company_id,
            branch_id=None,
            name="Entrance Camera",
            rtsp_url="rtsp://camera/stream",
            zone_type="in",
            roi_enabled=True,
            roi_polygon=[
                {"x": 0.25, "y": 0.2},
                {"x": 0.75, "y": 0.2},
                {"x": 0.75, "y": 0.9},
            ],
            clahe_enabled=True,
            status="offline",
            last_online_at=None,
            created_at=None,
            updated_at=None,
        )
        captured = {}
        service = Mock()
        service.status.return_value = {"company_id": "other-company"}

        def fake_upsert_camera(db, company_id_value, rtsp_url, **kwargs):
            captured["company_id"] = company_id_value
            captured["rtsp_url"] = rtsp_url
            captured.update(kwargs)
            return camera

        with patch.object(CCTVCameraDAO, "upsert_camera", side_effect=fake_upsert_camera), patch(
            "app.routers.cctv.cctv_recognition_service",
            service,
        ):
            response = client.post(
                f"/api/v1/cctv/source/{company_id}",
                json={
                    "name": "Entrance Camera",
                    "camera_source": "rtsp://camera/stream",
                    "zone_type": "in",
                    "roi_enabled": True,
                    "roi_polygon": [
                        {"x": 0.25, "y": 0.2},
                        {"x": 0.75, "y": 0.2},
                        {"x": 0.75, "y": 0.9},
                    ],
                    "clahe_enabled": True,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(captured["company_id"]), company_id)
        self.assertEqual(captured["rtsp_url"], "rtsp://camera/stream")
        self.assertTrue(captured["roi_enabled"])
        self.assertEqual(len(captured["roi_polygon"]), 3)
        self.assertTrue(captured["clahe_enabled"])
        result_camera = response.json()["result"]["camera"]
        self.assertTrue(result_camera["roi_enabled"])
        self.assertEqual(result_camera["roi_polygon"][0], {"x": 0.25, "y": 0.2})

    def test_create_camera_rejects_short_roi_polygon(self):
        company_id = "c8f745e0-aa6e-458b-bb70-4dda3e2accea"

        response = client.post(
            f"/api/v1/cctv/source/{company_id}",
            json={
                "name": "Entrance Camera",
                "camera_source": "rtsp://camera/stream",
                "zone_type": "in",
                "roi_enabled": True,
                "roi_polygon": [{"x": 0.25, "y": 0.2}],
            },
        )

        self.assertEqual(response.status_code, 422)
        self.assertIn("ROI polygon must contain at least 3 points.", response.text)

    def test_create_camera_returns_422_when_company_camera_limit_is_reached(self):
        company_id = "c8f745e0-aa6e-458b-bb70-4dda3e2accea"

        with patch.object(
            CCTVCameraDAO,
            "upsert_camera",
            side_effect=ValueError("Maximum CCTV cameras per company is 2."),
        ):
            response = client.post(
                f"/api/v1/cctv/source/{company_id}",
                json={
                    "name": "Third Camera",
                    "camera_source": "rtsp://camera/stream",
                    "zone_type": "in",
                },
            )

        self.assertEqual(response.status_code, 422)
        self.assertIn("Maximum CCTV cameras per company is 2.", response.text)
