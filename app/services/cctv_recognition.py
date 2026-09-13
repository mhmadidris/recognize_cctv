import hashlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.services.rabbitmq import RabbitMQPublisher, RabbitMQPublishError
from app.utils.timezone import DEFAULT_TIMEZONE, resolve_timezone


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOTENV_PATH = PROJECT_ROOT / ".env"
STATIC_DIR = PROJECT_ROOT / "app" / "static"
ATTENDANCE_SNAPSHOT_DIR = STATIC_DIR / "attendance_snapshots"
cv2 = None
np = None
requests = None
torch = None
FaceNet = None
cosine_similarity = None
YOLO = None
chromadb = None
DEVICE = "cpu"
_DOTENV_CACHE = None


def _load_ml_dependencies():
    global cv2, np, requests, torch, FaceNet, cosine_similarity, YOLO, chromadb, DEVICE

    if cv2 is not None:
        return

    import cv2 as cv2_module
    import numpy as np_module
    import requests as requests_module
    import torch as torch_module
    from keras_facenet import FaceNet as face_net_class
    from sklearn.metrics.pairwise import cosine_similarity as cosine_similarity_function
    from ultralytics import YOLO as yolo_class
    import chromadb as chromadb_module

    cv2 = cv2_module
    np = np_module
    requests = requests_module
    torch = torch_module
    FaceNet = face_net_class
    cosine_similarity = cosine_similarity_function
    YOLO = yolo_class
    chromadb = chromadb_module
    # This service is intentionally CPU-only; keep behavior consistent across hosts.
    DEVICE = "cpu"
    cv2.setNumThreads(1)
    torch.set_num_threads(1)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        # PyTorch rejects changing inter-op threads after work has started.
        pass


def _env_bool(name, default):
    return str(_env(name, default)).lower() in {"1", "true", "yes", "on"}


def _env_int(name, default=0):
    value = _env(name, "")
    if value in (None, ""):
        return default
    return int(value)


def _env_float(name, default=0.0):
    value = _env(name, "")
    if value in (None, ""):
        return default
    return float(value)


def _env(name, default=None):
    import os

    value = os.getenv(name)
    if value not in (None, ""):
        return value

    global _DOTENV_CACHE
    if _DOTENV_CACHE is None:
        _DOTENV_CACHE = {}
        if DOTENV_PATH.exists():
            for raw_line in DOTENV_PATH.read_text().splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, raw_value = line.split("=", 1)
                _DOTENV_CACHE[key.strip()] = raw_value.strip().strip('"').strip("'")

    dotenv_value = _DOTENV_CACHE.get(name)
    if dotenv_value not in (None, ""):
        return dotenv_value

    return default


def _camera_source(source_override=None):
    source = source_override if source_override is not None else _env("CAMERA_SOURCE", "")
    return int(source) if source.isdigit() else source


class Settings:
    monitor_mode = _env("CCTV_MONITOR_MODE", "")
    model_path = _env("FACE_MODEL_PATH", "")
    attendance_photos_url = _env("ATTENDANCE_PHOTOS_URL", "")
    hrms_token = _env("HRMS_TOKEN")
    employee_limit = _env_int("HRMS_EMPLOYEE_LIMIT")
    recognition_enabled = _env_bool("CCTV_RECOGNITION_ENABLED", "")
    similarity_threshold = _env_float("SIMILARITY_THRESHOLD")
    face_size = (160, 160)
    face_crop_margin = _env_float("FACE_CROP_MARGIN")
    registered_face_min_confidence = _env_float("REGISTERED_FACE_MIN_CONFIDENCE")
    detection_min_confidence = _env_float("DETECTION_MIN_CONFIDENCE")
    detect_width = _env_int("DETECT_WIDTH")
    track_iou_threshold = _env_float("TRACK_IOU_THRESHOLD")
    track_confirm_hits = _env_int("TRACK_CONFIRM_HITS")
    track_max_missed_frames = _env_int("TRACK_MAX_MISSED_FRAMES")
    vector_db_path = _env("VECTOR_DB_PATH", "")
    vector_db_collection = _env("VECTOR_DB_COLLECTION", "")
    branch_id = _env("HRMS_BRANCH_ID")
    attendance_policy_url = _env("ATTENDANCE_POLICY_URL", "")
    attendance_snapshot_enabled = _env_bool("ATTENDANCE_SNAPSHOT_ENABLED", "")
    attendance_snapshot_base_url = _env("ATTENDANCE_SNAPSHOT_BASE_URL", "").rstrip("/")
    attendance_snapshot_padding_ratio = _env_float("ATTENDANCE_SNAPSHOT_PADDING_RATIO", 0.75)
    reid_similarity_threshold = _env_float("REID_SIMILARITY_THRESHOLD", 0.82)
    reid_max_age_seconds = _env_float("REID_MAX_AGE_SECONDS", 5.0)
    clahe_brightness_threshold = _env_float("CLAHE_BRIGHTNESS_THRESHOLD", 85.0)
    min_face_width = _env_int("MIN_FACE_WIDTH", 32)
    min_face_height = _env_int("MIN_FACE_HEIGHT", 32)
    min_face_brightness = _env_float("MIN_FACE_BRIGHTNESS", 28.0)
    min_face_blur = _env_float("MIN_FACE_BLUR", 18.0)
    adaptive_frame_skip_enabled = _env_bool("ADAPTIVE_FRAME_SKIP_ENABLED", False)
    target_inference_ms = _env_int("TARGET_INFERENCE_MS", 120)
    max_frame_skip = _env_int("MAX_FRAME_SKIP", 3)


def camera_source_label(source_override=None):
    source = source_override if source_override is not None else _env("CAMERA_SOURCE", "")
    return str(source)


def hrms_configured():
    # Visitor-only monitoring does not need employee photos or HRMS access.
    return bool(
        Settings.monitor_mode in {"attendance", "both"}
        and Settings.recognition_enabled
        and Settings.attendance_photos_url
    )


def configured_hrms_company_ids():
    """Resolve HRMS tenant ids from the CCTV configuration instead of an env id."""
    from app.models.cctv import CCTVCamera
    from app.utils.database import SessionLocal

    db = SessionLocal()
    try:
        company_ids = db.query(CCTVCamera.company_id).distinct().all()
        return [str(company_id[0]) for company_id in company_ids if company_id[0]]
    finally:
        db.close()


class HRMSClient:
    def __init__(self, photos_url, company_id, employee_limit, token=None):
        self.photos_url = photos_url
        self.company_id = company_id
        self.employee_limit = employee_limit
        self.token = token

    def fetch_employee_photos(self):
        response = requests.get(
            self.photos_url,
            headers=self._headers(),
            params=self._params(),
            timeout=30,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            detail = response.text.strip()
            if len(detail) > 1000:
                detail = detail[:1000] + "..."
            raise RuntimeError(
                f"HRMS employee photos request failed ({response.status_code})"
                f" at {response.url}: {detail or 'no response body'}"
            ) from error
        payload = response.json()
        result = payload.get("result")
        if isinstance(result, dict):
            employees = result.get("employees")
            if isinstance(employees, list):
                print(f"HRMS photos response: using payload.result.employees ({len(employees)} employees)")
                return employees
            if isinstance(result.get("data"), dict):
                nested_employees = result["data"].get("employees")
                if isinstance(nested_employees, list):
                    print(
                        "HRMS photos response: using payload.result.data.employees "
                        f"({len(nested_employees)} employees)"
                    )
                    return nested_employees

        data = payload.get("data")
        if isinstance(data, dict):
            employees = data.get("employees")
            if isinstance(employees, list):
                print(f"HRMS photos response: using payload.data.employees ({len(employees)} employees)")
                return employees

        print(
            "HRMS photos response: no employees found in known keys "
            f"(top-level keys: {list(payload.keys())})"
        )
        return []

    def download_image(self, image_url):
        response = requests.get(image_url, timeout=20)
        response.raise_for_status()
        image_array = np.frombuffer(response.content, np.uint8)
        return cv2.imdecode(image_array, cv2.IMREAD_COLOR)

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _params(self):
        return {
            "company_id": self.company_id,
            "employee_limit": self.employee_limit,
        }


class AttendancePolicyService:
    ALLOWED_KEYS = {
        "shift_assignment_id",
        "shift_type_id",
        "shift_type_name",
        "shift_start_time",
        "shift_end_time",
        "shift_start_date",
        "shift_end_date",
        "shift_ignore_holiday_date",
        "shift_status",
        "leave_type_id",
        "leave_id",
        "attendance_request_id",
        "attendance_status",
        "checkin_status",
        "checkout_status",
        "status",
        "reason",
        "explanation",
        "explanation_out",
    }

    def __init__(self, policy_url=None, token=None):
        self.policy_url = policy_url
        self.token = token

    def resolve(self, company_id, employee_id, attendance_type, detected_at, branch_id=None, camera_id=None):
        timezone_name = self._company_timezone(company_id)
        if not self.policy_url:
            return self._fallback_policy(detected_at, attendance_type, timezone_name)

        try:
            response = requests.post(
                self.policy_url,
                headers=self._headers(),
                json={
                    "company_id": str(company_id),
                    "employee_id": str(employee_id),
                    "attendance_type": attendance_type,
                    "detected_at": detected_at.isoformat(),
                    "attendance_date": detected_at.date().isoformat(),
                    "status": "active",
                    "branch_id": str(branch_id) if branch_id else None,
                    "camera_id": str(camera_id) if camera_id else None,
                },
                timeout=10,
            )
            response.raise_for_status()
            policy = self._extract_policy(
                response.json(),
                employee_id=employee_id,
                company_id=company_id,
                detected_at=detected_at,
            )
            if policy:
                return policy
            return self._fallback_policy(detected_at, attendance_type, timezone_name)
        except Exception as error:
            print(f"Warning: attendance policy lookup failed for employee {employee_id}: {error}")
            return self._fallback_policy(detected_at, attendance_type, timezone_name)

    def _company_timezone(self, company_id):
        from app.daos.cctv import CCTVCompanySettingDAO
        from app.utils.database import SessionLocal
        with SessionLocal() as db:
            setting = CCTVCompanySettingDAO.get_by_company_id(db, company_id)
            return getattr(setting, "timezone", None) or DEFAULT_TIMEZONE

    def _fallback_policy(self, detected_at, attendance_type, timezone_name):
        local_now = detected_at.astimezone(resolve_timezone(timezone_name))
        shift_start = local_now.replace(hour=9, minute=0, second=0, microsecond=0)
        shift_end = local_now.replace(hour=17, minute=0, second=0, microsecond=0)
        
        checkin_status = "ontime"
        checkout_status = "ontime"
        
        if attendance_type == "in" and local_now > shift_start:
            checkin_status = "late"
        if attendance_type == "out" and local_now < shift_end:
            checkout_status = "early_checkout"
            
        return {
            "shift_type_name": "Default Shift (09:00 - 17:00)",
            "shift_start_time": "09:00:00",
            "shift_end_time": "17:00:00",
            "checkin_status": checkin_status,
            "checkout_status": checkout_status,
        }

    def _headers(self):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _extract_policy(self, payload, employee_id=None, company_id=None, detected_at=None):
        assignment = self._select_shift_assignment(
            payload,
            employee_id=employee_id,
            company_id=company_id,
            detected_at=detected_at,
        )
        if assignment:
            shift_type = assignment.get("shift_type_id_rel") or {}
            return {
                "shift_assignment_id": assignment.get("id"),
                "shift_type_id": assignment.get("shift_type_id"),
                "shift_type_name": shift_type.get("shift_type_name"),
                "shift_start_time": shift_type.get("start_time"),
                "shift_end_time": shift_type.get("end_time"),
                "shift_start_date": assignment.get("start_date"),
                "shift_end_date": assignment.get("end_date"),
                "shift_ignore_holiday_date": assignment.get("ignore_holiday_date"),
                "shift_status": assignment.get("status"),
            }

        candidates = [payload]
        if isinstance(payload, dict):
            for key in ("result", "data", "policy"):
                value = payload.get(key)
                if isinstance(value, dict):
                    candidates.append(value)
                    nested_policy = value.get("policy")
                    if isinstance(nested_policy, dict):
                        candidates.append(nested_policy)

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            policy = {
                key: candidate[key]
                for key in self.ALLOWED_KEYS
                if candidate.get(key) not in (None, "")
            }
            if policy:
                return policy
        return {}

    def _select_shift_assignment(self, payload, employee_id=None, company_id=None, detected_at=None):
        """Select the applicable assignment from the HRMS shift-list response."""
        if not isinstance(payload, dict):
            return None

        assignments = payload.get("data")
        if not isinstance(assignments, list):
            return None

        request = payload.get("request") or {}
        employee_id = str(employee_id or request.get("employee_id")) if (employee_id or request.get("employee_id")) else None
        company_id = str(company_id or request.get("company_id")) if (company_id or request.get("company_id")) else None
        response_detected_at = detected_at or request.get("detected_at")
        event_date = response_detected_at.date().isoformat() if hasattr(response_detected_at, "date") else str(response_detected_at)[:10] if response_detected_at else None

        matches = []
        for assignment in assignments:
            if not isinstance(assignment, dict):
                continue
            if employee_id and str(assignment.get("employee_id")) != employee_id:
                continue
            if company_id and str(assignment.get("company_id")) != company_id:
                continue
            if assignment.get("status") != "active":
                continue
            start_date = str(assignment.get("start_date") or "")
            end_date = str(assignment.get("end_date") or "")
            if event_date and (event_date < start_date or event_date > end_date):
                continue
            matches.append(assignment)

        if not matches:
            return None
        return max(matches, key=lambda item: str(item.get("start_date") or ""))


def attendance_allowed_after_tolerance(policy, detected_at, attendance_type, tolerance_minutes, timezone_name=DEFAULT_TIMEZONE):
    """Return whether an event is inside the configured shift tolerance window."""
    start_value = policy.get("shift_start_time")
    end_value = policy.get("shift_end_time")
    if not start_value or not end_value:
        return True

    try:
        start_time = datetime.strptime(str(start_value), "%H:%M:%S").time()
        end_time = datetime.strptime(str(end_value), "%H:%M:%S").time()
        local_now = detected_at.astimezone(resolve_timezone(timezone_name))
        start_at = datetime.combine(local_now.date(), start_time, tzinfo=local_now.tzinfo)
        end_at = datetime.combine(local_now.date(), end_time, tzinfo=local_now.tzinfo)
        if end_at <= start_at:
            end_at += timedelta(days=1)
        if attendance_type == "out" and local_now < start_at:
            start_at -= timedelta(days=1)
            end_at -= timedelta(days=1)
        tolerance = timedelta(minutes=max(0, int(tolerance_minutes or 0)))
        if attendance_type == "in":
            return local_now <= start_at + tolerance
        return local_now >= end_at - tolerance
    except (TypeError, ValueError):
        return True


class VectorStore:
    def __init__(self, db_path, collection_name):
        self.client = chromadb.PersistentClient(path=db_path)
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"}
        )
        self._warned_incomplete_ids = set()

    def upsert_face(self, embedding, name, employee_id, image_url):
        # Use image_url as ID to ensure uniqueness of the specific photo
        face_id = hashlib.md5(image_url.encode()).hexdigest()
        self.collection.upsert(
            ids=[face_id],
            embeddings=[embedding.tolist()],
            metadatas=[{"name": name, "employee_id": str(employee_id), "url": image_url}]
        )

    def update_face_metadata(self, face_id, name, employee_id, image_url):
        self.collection.update(
            ids=[face_id],
            metadatas=[{"name": name, "employee_id": str(employee_id), "url": image_url}]
        )

    def has_complete_metadata(self, existing, name, employee_id):
        metadatas = existing.get("metadatas") or []
        if not metadatas:
            return False

        metadata = metadatas[0] or {}
        return (
            metadata.get("name") == name
            and metadata.get("employee_id") == str(employee_id)
        )

    def query_face(self, embedding):
        results = self.collection.query(
            query_embeddings=[embedding.tolist()],
            n_results=5
        )
        
        if not results["ids"] or not results["ids"][0]:
            return None

        for face_id, distance, metadata in zip(
            results["ids"][0],
            results["distances"][0],
            results["metadatas"][0],
        ):
            # ChromaDB returns distances. For cosine space, distance = 1 - similarity
            similarity = 1 - distance
            metadata = metadata or {}
            name = metadata.get("name")
            employee_id = metadata.get("employee_id")

            if not name or not employee_id:
                if face_id not in self._warned_incomplete_ids:
                    print(
                        "Warning: matched face has incomplete metadata "
                        f"(id={face_id}), skipping this candidate."
                    )
                    self._warned_incomplete_ids.add(face_id)
                continue

            return {
                "name": name,
                "employee_id": employee_id,
                "similarity": similarity,
                "id": face_id
            }

        return None

    def get_all_names(self):
        results = self.collection.get()
        if not results["metadatas"]:
            return []
        return list(set(m["name"] for m in results["metadatas"] if m and m.get("name")))


class FaceDetector:
    def __init__(
        self,
        model_path,
        face_size,
        crop_margin,
        registered_min_confidence,
        detection_min_confidence,
        device=None,
    ):
        self.model = YOLO(model_path, task="detect")
        self.device = "cpu"
        self.face_size = face_size
        self.crop_margin = crop_margin
        self.registered_min_confidence = registered_min_confidence
        self.detection_min_confidence = detection_min_confidence

    def crop_face(self, image, box):
        image_height, image_width = image.shape[:2]
        x1, y1, x2, y2 = box
        box_width = x2 - x1
        box_height = y2 - y1
        margin_x = int(box_width * self.crop_margin)
        margin_y = int(box_height * self.crop_margin)

        x1 = max(0, x1 - margin_x)
        y1 = max(0, y1 - margin_y)
        x2 = min(image_width, x2 + margin_x)
        y2 = min(image_height, y2 + margin_y)
        return image[y1:y2, x1:x2]

    def prepare_face_image(self, face):
        face = cv2.cvtColor(face, cv2.COLOR_BGR2RGB)
        face = cv2.resize(face, self.face_size).astype(np.float32)
        return face

    def prepare_registered_face(self, image, image_name):
        results = self.model(image, device=self.device, verbose=False)
        best_box = None
        best_area = 0

        for result in results:
            for box in result.boxes:
                confidence = float(box.conf[0])
                if confidence < self.registered_min_confidence:
                    continue

                x1, y1, x2, y2 = map(int, box.xyxy[0])
                area = (x2 - x1) * (y2 - y1)
                if area > best_area:
                    best_area = area
                    best_box = (x1, y1, x2, y2)

        if best_box is None:
            print(f"Warning: face not detected in {image_name}, using full image.")
            face = image
        else:
            face = self.crop_face(image, best_box)

        return self.prepare_face_image(face)

    def detect_prepared_faces(self, frame, detect_width, detection_min_confidence=None):
        detection_min_confidence = (
            self.detection_min_confidence
            if detection_min_confidence is None
            else detection_min_confidence
        )
        frame_height, frame_width = frame.shape[:2]
        scale = min(1.0, detect_width / frame_width)
        detect_frame = cv2.resize(frame, (int(frame_width * scale), int(frame_height * scale)))

        results = self.model(detect_frame, device=self.device, verbose=False)
        prepared_faces = []
        coordinates = []

        for result in results:
            for box in result.boxes:
                confidence = float(box.conf[0])
                if confidence <= detection_min_confidence:
                    continue

                x1, y1, x2, y2 = map(int, box.xyxy[0])
                if scale != 1.0:
                    x1 = int(x1 / scale)
                    y1 = int(y1 / scale)
                    x2 = int(x2 / scale)
                    y2 = int(y2 / scale)

                cropped_frame = self.crop_face(frame, (x1, y1, x2, y2))
                if cropped_frame.size == 0:
                    continue

                prepared_faces.append(self.prepare_face_image(cropped_frame))
                coordinates.append((x1, y1, x2, y2))

        return prepared_faces, coordinates


class FaceRecognizer:
    def __init__(self, vector_store, similarity_threshold, embedder=None):
        self.vector_store = vector_store
        self.similarity_threshold = similarity_threshold
        self.embedder = embedder or FaceNet()

    def recognize(self, prepared_faces, similarity_threshold=None):
        if len(prepared_faces) == 0:
            return []

        similarity_threshold = (
            self.similarity_threshold
            if similarity_threshold is None
            else similarity_threshold
        )
        cctv_face_embeddings = self.embedder.embeddings(np.array(prepared_faces))
        recognition_results = []

        for embedding in cctv_face_embeddings:
            match = self.vector_store.query_face(embedding)
            
            if match:
                similarity = match["similarity"]
                recognition_results.append(
                    {
                        "name": match["name"],
                        "employee_id": match["employee_id"],
                        "similarity": similarity,
                        "accuracy": similarity * 100,
                        "matched": similarity > similarity_threshold,
                        "embedding": embedding,
                    }
                )
            else:
                recognition_results.append(
                    {
                        "name": "Unknown",
                        "employee_id": None,
                        "similarity": 0,
                        "accuracy": 0,
                        "matched": False,
                        "embedding": embedding,
                    }
                )

        return recognition_results


def _embedding_similarity(embedding_a, embedding_b):
    if embedding_a is None or embedding_b is None:
        return 0.0
    denom = np.linalg.norm(embedding_a) * np.linalg.norm(embedding_b)
    if denom <= 0:
        return 0.0
    return float(np.dot(embedding_a, embedding_b) / denom)


def _normalized_roi_to_pixels(roi_polygon, frame_width, frame_height):
    points = []
    for point in roi_polygon or []:
        if not isinstance(point, dict):
            continue
        try:
            x = float(point.get("x"))
            y = float(point.get("y"))
        except (TypeError, ValueError):
            continue
        if 0 <= x <= 1 and 0 <= y <= 1:
            points.append((int(x * frame_width), int(y * frame_height)))
    return points if len(points) >= 3 else []


def _point_in_polygon(point, polygon):
    if not polygon:
        return True
    return cv2.pointPolygonTest(np.array(polygon, dtype=np.int32), point, False) >= 0


def _apply_roi_mask(frame, roi_polygon):
    frame_height, frame_width = frame.shape[:2]
    points = _normalized_roi_to_pixels(roi_polygon, frame_width, frame_height)
    if not points:
        return frame
    mask = np.zeros(frame.shape[:2], dtype=np.uint8)
    cv2.fillPoly(mask, [np.array(points, dtype=np.int32)], 255)
    return cv2.bitwise_and(frame, frame, mask=mask)


def _roi_crop(frame, roi_polygon):
    frame_height, frame_width = frame.shape[:2]
    points = _normalized_roi_to_pixels(roi_polygon, frame_width, frame_height)
    if not points:
        return frame, (0, 0), []

    polygon = np.array(points, dtype=np.int32)
    x, y, width, height = cv2.boundingRect(polygon)
    if width <= 0 or height <= 0:
        return frame, (0, 0), []

    x2 = min(frame_width, x + width)
    y2 = min(frame_height, y + height)
    cropped = frame[y:y2, x:x2].copy()
    shifted_polygon = [(px - x, py - y) for px, py in points]
    mask = np.zeros(cropped.shape[:2], dtype=np.uint8)
    cv2.fillPoly(mask, [np.array(shifted_polygon, dtype=np.int32)], 255)
    return cv2.bitwise_and(cropped, cropped, mask=mask), (x, y), shifted_polygon


def _should_apply_clahe(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    return float(gray.mean()) < Settings.clahe_brightness_threshold


def _apply_clahe(frame):
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    lightness, channel_a, channel_b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    enhanced_lightness = clahe.apply(lightness)
    enhanced = cv2.merge((enhanced_lightness, channel_a, channel_b))
    return cv2.cvtColor(enhanced, cv2.COLOR_LAB2BGR)


def _box_iou(box_a, box_b):
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b

    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)

    if inter_x2 <= inter_x1 or inter_y2 <= inter_y1:
        return 0.0

    intersection = (inter_x2 - inter_x1) * (inter_y2 - inter_y1)
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - intersection
    if union <= 0:
        return 0.0
    return intersection / union


class AttendanceService:
    def __init__(self):
        self._lock = threading.RLock()
        self.session_attendance = {}
        self.workin_responses = []

    def mark(self, employee_id, name, attendance_type, detected_at=None, attendance_policy=None, timezone_name=DEFAULT_TIMEZONE):
        with self._lock:
            detected_at = detected_at or datetime.now(timezone.utc)
            local_time = detected_at.astimezone(resolve_timezone(timezone_name))
            current_time = local_time.strftime("%H:%M:%S")
            key = str(employee_id)
            record = self.session_attendance.setdefault(
                key,
                {
                    "employee_id": key,
                    "employee_name": name,
                    "time_in": None,
                    "time_out": None,
                    "photo_in": None,
                    "photo_out": None,
                    "last_seen_at": None,
                    "detection_count": 0,
                    "shift_name": "-",
                    "checkin_status": "-",
                    "checkout_status": "-",
                },
            )
            if attendance_type == "in":
                is_new = record["time_in"] is None or record["time_out"] is not None
                if is_new:
                    record["time_in"] = current_time
                    record["time_out"] = None
                    record["photo_in"] = None
                    record["photo_out"] = None
            else:
                is_new = record["time_out"] is None
                if is_new:
                    record["time_out"] = current_time
                    
            if is_new:
                print(f"Attendance display updated: {name} {'OUT' if attendance_type == 'out' else 'IN'}={current_time}")
            
            if attendance_policy:
                if attendance_policy.get("shift_type_name"):
                    record["shift_name"] = attendance_policy["shift_type_name"]
                if attendance_type == "in" and attendance_policy.get("checkin_status"):
                    record["checkin_status"] = attendance_policy["checkin_status"]
                elif attendance_type == "out" and attendance_policy.get("checkout_status"):
                    record["checkout_status"] = attendance_policy["checkout_status"]

            record["employee_name"] = name
            record["last_seen_at"] = current_time
            record["detection_count"] += 1
            return dict(record), is_new

    def is_new(self, employee_id, attendance_type):
        with self._lock:
            record = self.session_attendance.get(str(employee_id))
            if not record:
                return True
            if attendance_type == "in":
                return record.get("time_in") is None or record.get("time_out") is not None
            else:
                return record.get("time_out") is None

    def attach_photo(self, employee_id, attendance_type, photo_url):
        if not photo_url:
            return None

        with self._lock:
            record = self.session_attendance.get(str(employee_id))
            if not record:
                return None

            photo_key = "photo_out" if attendance_type == "out" else "photo_in"
            record[photo_key] = photo_url
            return dict(record)

    def records(self):
        with self._lock:
            return {
                employee_id: dict(record)
                for employee_id, record in self.session_attendance.items()
                if record.get("time_in") or record.get("time_out")
            }

    def record_workin_response(self, attendance_type, payload):
        with self._lock:
            response = {
                "attendance_type": attendance_type,
                "received_at": datetime.now(timezone.utc).isoformat(),
                "payload": payload,
            }
            self.workin_responses.append(response)
            self.workin_responses = self.workin_responses[-100:]
            return response

    def reset(self):
        with self._lock:
            self.session_attendance = {}
            self.workin_responses = []


class LatestFrameCapture:
    def __init__(self, source):
        self.cap = cv2.VideoCapture(source)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self.lock = threading.Lock()
        self.ret = False
        self.frame = None
        self.running = self.cap.isOpened()
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def _reader(self):
        while self.running:
            ret, frame = self.cap.read()
            if not ret:
                time.sleep(0.05)
                continue
            with self.lock:
                self.ret = ret
                self.frame = frame

    def is_opened(self):
        return self.running and self.cap.isOpened()

    def read(self):
        with self.lock:
            if self.frame is None:
                return False, None
            return self.ret, self.frame.copy()

    def release(self):
        self.running = False
        self.thread.join(timeout=1)
        self.cap.release()


class DetectionWorker:
    def __init__(
        self,
        face_detector,
        face_recognizer,
        attendance_service,
        detect_width,
        zone_type="entrance",
        roi_enabled=False,
        roi_polygon=None,
        clahe_enabled=False,
        detection_confidence_threshold=None,
        recognition_similarity_threshold=None,
        min_face_width=None,
        min_face_height=None,
        min_face_brightness=None,
        min_face_blur=None,
        adaptive_frame_skip_enabled=False,
        target_inference_ms=None,
        max_frame_skip=None,
    ):
        self.face_detector = face_detector
        self.face_recognizer = face_recognizer
        self.attendance_service = attendance_service
        self.detect_width = detect_width
        self.zone_type = zone_type
        self.roi_enabled = roi_enabled
        self.roi_polygon = roi_polygon or []
        self.clahe_enabled = clahe_enabled
        self.detection_confidence_threshold = detection_confidence_threshold
        self.recognition_similarity_threshold = recognition_similarity_threshold
        self.min_face_width = min_face_width if min_face_width is not None else Settings.min_face_width
        self.min_face_height = min_face_height if min_face_height is not None else Settings.min_face_height
        self.min_face_brightness = (
            min_face_brightness if min_face_brightness is not None else Settings.min_face_brightness
        )
        self.min_face_blur = min_face_blur if min_face_blur is not None else Settings.min_face_blur
        self.adaptive_frame_skip_enabled = (
            adaptive_frame_skip_enabled
            if adaptive_frame_skip_enabled is not None
            else Settings.adaptive_frame_skip_enabled
        )
        self.target_inference_ms = target_inference_ms if target_inference_ms is not None else Settings.target_inference_ms
        self.max_frame_skip = max(0, max_frame_skip if max_frame_skip is not None else Settings.max_frame_skip)
        self._current_frame_skip = 0
        self._skip_remaining = 0
        self._sequence = 0
        self.frame_lock = threading.Lock()
        self.result_lock = threading.Lock()
        self.frame = None
        self.detections = []
        self.metrics = {
            "fps": 0.0,
            "last_face_count": 0,
            "last_total_ms": 0.0,
            "last_detection_ms": 0.0,
            "last_recognition_ms": 0.0,
            "last_age_ms": 0.0,
            "roi_crop_applied": False,
            "clahe_applied": False,
            "quality_rejected_count": 0,
            "frame_skip_current": 0,
            "frame_skipped": False,
            "sequence": 0,
        }
        self._last_metric_at = None
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def update_frame(self, frame):
        with self.frame_lock:
            self.frame = frame.copy()

    def get_detections(self):
        with self.result_lock:
            return list(self.detections)

    def get_metrics(self):
        with self.result_lock:
            return dict(self.metrics)

    def _run(self):
        while self.running:
            with self.frame_lock:
                frame = None if self.frame is None else self.frame.copy()

            if frame is None:
                time.sleep(0.01)
                continue

            if self._skip_remaining > 0:
                self._skip_remaining -= 1
                with self.result_lock:
                    self.metrics = {
                        **self.metrics,
                        "frame_skip_current": self._current_frame_skip,
                        "frame_skipped": True,
                    }
                time.sleep(0.01)
                continue

            started_at = time.perf_counter()
            detections, metrics = self._detect_faces(frame)
            total_ms = (time.perf_counter() - started_at) * 1000
            now = time.perf_counter()
            previous_metric_at = self._last_metric_at
            self._last_metric_at = now
            fps = 0.0 if previous_metric_at is None else 1 / max(0.001, now - previous_metric_at)
            metrics["fps"] = fps
            metrics["last_total_ms"] = total_ms
            metrics["last_face_count"] = len(detections)
            self._update_adaptive_frame_skip(total_ms)
            metrics["frame_skip_current"] = self._current_frame_skip
            metrics["frame_skipped"] = False
            self._sequence += 1
            metrics["sequence"] = self._sequence
            with self.result_lock:
                self.detections = detections
                self.metrics = metrics
            self._skip_remaining = self._current_frame_skip

    def _detect_faces(self, frame):
        analysis_frame = frame
        offset_x = 0
        offset_y = 0
        roi_pixels = []
        metrics = {
            "fps": 0.0,
            "last_face_count": 0,
            "last_total_ms": 0.0,
            "last_detection_ms": 0.0,
            "last_recognition_ms": 0.0,
            "last_age_ms": 0.0,
            "roi_crop_applied": False,
            "clahe_applied": False,
            "quality_rejected_count": 0,
            "frame_skip_current": self._current_frame_skip,
            "frame_skipped": False,
            "sequence": self._sequence,
        }
        if self.roi_enabled and self.roi_polygon:
            analysis_frame, (offset_x, offset_y), roi_pixels = _roi_crop(analysis_frame, self.roi_polygon)
            metrics["roi_crop_applied"] = bool(roi_pixels)
        if self.clahe_enabled and _should_apply_clahe(analysis_frame):
            analysis_frame = _apply_clahe(analysis_frame)
            metrics["clahe_applied"] = True

        detection_started_at = time.perf_counter()
        prepared_faces, coordinates = self.face_detector.detect_prepared_faces(
            analysis_frame,
            self.detect_width,
            detection_min_confidence=self.detection_confidence_threshold,
        )
        metrics["last_detection_ms"] = (time.perf_counter() - detection_started_at) * 1000
        if offset_x or offset_y:
            coordinates = [
                (x1 + offset_x, y1 + offset_y, x2 + offset_x, y2 + offset_y)
                for x1, y1, x2, y2 in coordinates
            ]
        if self.roi_enabled and self.roi_polygon:
            if not roi_pixels:
                frame_height, frame_width = frame.shape[:2]
                roi_pixels = _normalized_roi_to_pixels(self.roi_polygon, frame_width, frame_height)
            else:
                roi_pixels = [(x + offset_x, y + offset_y) for x, y in roi_pixels]
            filtered_faces = []
            filtered_coordinates = []
            for face, box in zip(prepared_faces, coordinates):
                x1, y1, x2, y2 = box
                center = (int((x1 + x2) / 2), int((y1 + y2) / 2))
                if _point_in_polygon(center, roi_pixels):
                    filtered_faces.append(face)
                    filtered_coordinates.append(box)
            prepared_faces = filtered_faces
            coordinates = filtered_coordinates

        prepared_faces, coordinates, quality_rejected_count = self._filter_low_quality_faces(
            frame,
            prepared_faces,
            coordinates,
        )
        metrics["quality_rejected_count"] = quality_rejected_count

        if self.face_recognizer is None:
            return [
                {
                    "box": (x1, y1, x2, y2),
                    "label": "Face detected",
                    "color": (0, 255, 255),
                    "employee_id": None,
                    "accuracy": 0,
                    "similarity": 0,
                    "matched": False,
                    "age": None,
                }
                for index, (x1, y1, x2, y2) in enumerate(coordinates)
            ], metrics

        recognition_started_at = time.perf_counter()
        recognition_results = self.face_recognizer.recognize(
            prepared_faces,
            similarity_threshold=self.recognition_similarity_threshold,
        )
        metrics["last_recognition_ms"] = (time.perf_counter() - recognition_started_at) * 1000

        detections = []
        for index, (coordinates_item, recognition) in enumerate(zip(coordinates, recognition_results)):
            x1, y1, x2, y2 = coordinates_item
            accuracy = recognition["accuracy"]
            if recognition["matched"]:
                name = recognition["name"]
                display_text = f"{name} {accuracy:.1f}%"
                box_color = (0, 255, 0)
            else:
                display_text = f"Data not found {accuracy:.1f}%"
                box_color = (0, 255, 255)

            detections.append({
                "box": (x1, y1, x2, y2),
                "label": display_text,
                "color": box_color,
                "employee_id": recognition["employee_id"],
                "name": recognition["name"],
                "accuracy": accuracy,
                "similarity": recognition["similarity"],
                "matched": recognition["matched"],
                "embedding": recognition.get("embedding"),
                "age": None,
            })

        return detections, metrics

    def _filter_low_quality_faces(self, frame, prepared_faces, coordinates):
        if not prepared_faces:
            return prepared_faces, coordinates, 0

        filtered_faces = []
        filtered_coordinates = []
        rejected_count = 0
        for face, box in zip(prepared_faces, coordinates):
            if self._face_quality_passes(frame, box):
                filtered_faces.append(face)
                filtered_coordinates.append(box)
            else:
                rejected_count += 1
        return filtered_faces, filtered_coordinates, rejected_count

    def _face_quality_passes(self, frame, box):
        x1, y1, x2, y2 = [int(value) for value in box]
        width = x2 - x1
        height = y2 - y1
        if width < self.min_face_width or height < self.min_face_height:
            return False

        crop = frame[max(0, y1):max(0, y2), max(0, x1):max(0, x2)]
        if crop.size == 0:
            return False

        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        if float(gray.mean()) < self.min_face_brightness:
            return False
        if float(cv2.Laplacian(gray, cv2.CV_64F).var()) < self.min_face_blur:
            return False
        return True

    def _update_adaptive_frame_skip(self, total_ms):
        if not self.adaptive_frame_skip_enabled:
            self._current_frame_skip = 0
            return
        target = max(1, self.target_inference_ms)
        if total_ms > target and self._current_frame_skip < self.max_frame_skip:
            self._current_frame_skip += 1
        elif total_ms < target * 0.6 and self._current_frame_skip > 0:
            self._current_frame_skip -= 1

    def stop(self):
        self.running = False
        self.thread.join(timeout=1)


def load_registered_faces(
    hrms_client,
    face_detector,
    embedder,
    vector_store,
    progress_callback=None,
    progress_start=0,
    progress_end=100,
):
    employees = hrms_client.fetch_employee_photos()
    if not employees:
        if progress_callback:
            progress_callback(progress_end, "No employee photos found")
        return []

    print(f"Loading and processing photos for {len(employees)} employees...")

    def process_photo(employee_name, employee_id, image_url, image_index):
        if not image_url:
            return None

        # Check if already in vector store (caching logic)
        face_id = hashlib.md5(image_url.encode()).hexdigest()
        existing = vector_store.collection.get(ids=[face_id])
        if existing["ids"]:
            if not vector_store.has_complete_metadata(existing, employee_name, employee_id):
                vector_store.update_face_metadata(face_id, employee_name, employee_id, image_url)
            return employee_name

        image_name = f"{employee_name}_{image_index}"
        try:
            person_image = hrms_client.download_image(image_url)
            if person_image is None:
                print(f"Warning: cannot decode {image_name}, skipped.")
                return None

            person_face = face_detector.prepare_registered_face(person_image, image_name)
            # Generate embedding
            embedding = embedder.embeddings(np.array([person_face]))[0]
            
            # Save to vector store
            vector_store.upsert_face(embedding, employee_name, employee_id, image_url)
            
            return employee_name
        except Exception as error:
            print(f"Warning: error processing {image_name}: {error}")
            return None

    tasks = []
    for employee in employees:
        employee_name = employee.get("name")
        employee_id = employee.get("employee_id") or employee.get("id")
        if not employee_name or not employee_id:
            continue
        
        for i, url in enumerate(employee.get("images", []), start=1):
            tasks.append((employee_name, employee_id, url, i))

    with ThreadPoolExecutor(max_workers=10) as executor:
        if tasks:
            futures = [executor.submit(process_photo, *task) for task in tasks]
            total_tasks = len(futures)
            for completed_count, future in enumerate(as_completed(futures), start=1):
                future.result()
                if progress_callback:
                    progress = progress_start + int(
                        ((progress_end - progress_start) * completed_count) / total_tasks
                    )
                    progress_callback(
                        progress,
                        f"Synchronizing employee faces ({completed_count}/{total_tasks})",
                    )
        elif progress_callback:
            progress_callback(progress_end, "No employee photos to synchronize")

    registered_names = vector_store.get_all_names()
    print(f"Successfully synchronized {len(registered_names)} employee names in Vector DB.")
    return registered_names

class CCTVRecognitionService:
    @dataclass
    class CameraConfig:
        camera_id: str | None
        company_id: str
        name: str
        camera_source: str
        branch_id: str | None = None
        zone_type: str = "entrance"
        roi_enabled: bool = False
        roi_polygon: list | None = None
        clahe_enabled: bool = False
        detection_confidence_threshold: float | None = None
        recognition_similarity_threshold: float | None = None
        reid_similarity_threshold: float | None = None
        track_confirm_hits: int | None = None
        min_face_width: int | None = None
        min_face_height: int | None = None
        min_face_brightness: float | None = None
        min_face_blur: float | None = None
        adaptive_frame_skip_enabled: bool = False
        target_inference_ms: int | None = None
        max_frame_skip: int | None = None

        @property
        def worker_key(self):
            return str(self.camera_id) if self.camera_id else "__default__"

    class SharedRecognitionRuntime:
        def __init__(self):
            self._condition = threading.Condition(threading.RLock())
            self._loading = False
            self._loaded = False
            self._error = None
            self._face_detector = None
            self._face_recognizer = None
            self._registered_names = []

        def get_runtime(self, progress_callback=None):
            with self._condition:
                if self._loaded:
                    return self._snapshot_unlocked()

                if self._loading:
                    if progress_callback:
                        progress_callback(10, "Waiting for shared recognizer")
                    while self._loading:
                        self._condition.wait(timeout=0.5)
                    if self._loaded:
                        return self._snapshot_unlocked()
                    raise RuntimeError(self._error or "Shared recognizer initialization failed.")

                self._loading = True
                self._error = None

            try:
                if progress_callback:
                    progress_callback(5, "Loading ML dependencies")
                print("CCTV Runtime: Loading ML dependencies...")
                _load_ml_dependencies()

                if progress_callback:
                    progress_callback(20, "Initializing face detector")
                print("CCTV Runtime: Initializing face detector...")
                face_detector = FaceDetector(
                    model_path=Settings.model_path,
                    face_size=Settings.face_size,
                    crop_margin=Settings.face_crop_margin,
                    registered_min_confidence=Settings.registered_face_min_confidence,
                    detection_min_confidence=Settings.detection_min_confidence,
                )

                registered_names = []
                face_recognizer = None
                if hrms_configured():
                    if progress_callback:
                        progress_callback(35, "Preparing HRMS sync")
                    print("CCTV Runtime: Initializing Vector Store and HRMS Sync...")
                    company_ids = configured_hrms_company_ids()
                    if not company_ids:
                        raise RuntimeError(
                            "Cannot synchronize HRMS photos: no CCTV camera is configured with a company_id."
                        )
                    vector_store = VectorStore(Settings.vector_db_path, Settings.vector_db_collection)
                    embedder = FaceNet()

                    if progress_callback:
                        progress_callback(50, "Synchronizing employee faces")
                    for company_id in company_ids:
                        hrms_client = HRMSClient(
                            photos_url=Settings.attendance_photos_url,
                            company_id=company_id,
                            employee_limit=Settings.employee_limit,
                            token=Settings.hrms_token,
                        )
                        registered_names.extend(
                            load_registered_faces(
                                hrms_client,
                                face_detector,
                                embedder,
                                vector_store,
                                progress_callback=progress_callback,
                                progress_start=50,
                                progress_end=82,
                            )
                        )
                    registered_names = sorted(set(registered_names))

                    if registered_names:
                        if progress_callback:
                            progress_callback(86, "Initializing face recognizer")
                        face_recognizer = FaceRecognizer(
                            vector_store,
                            Settings.similarity_threshold,
                            embedder=embedder,
                        )
                else:
                    if progress_callback:
                        progress_callback(86, "Recognition disabled")

            except Exception as error:
                with self._condition:
                    self._loading = False
                    self._loaded = False
                    self._error = str(error)
                    self._condition.notify_all()
                raise

            with self._condition:
                self._face_detector = face_detector
                self._face_recognizer = face_recognizer
                self._registered_names = sorted(set(registered_names))
                self._loading = False
                self._loaded = True
                self._condition.notify_all()
                return self._snapshot_unlocked()

        def status(self):
            with self._condition:
                return {
                    "loaded": self._loaded,
                    "loading": self._loading,
                    "error": self._error,
                    "registered_names": list(self._registered_names),
                    "registered_count": len(self._registered_names),
                }

        def _snapshot_unlocked(self):
            return {
                "face_detector": self._face_detector,
                "face_recognizer": self._face_recognizer,
                "registered_names": list(self._registered_names),
            }

    class CameraWorker:
        def __init__(
            self,
            config,
            runtime,
            shared_attendance_service,
            rabbitmq_publisher,
            attendance_policy_service,
        ):
            self._lock = threading.RLock()
            self._config = config
            self._runtime = runtime
            self._shared_attendance_service = shared_attendance_service
            self._rabbitmq_publisher = rabbitmq_publisher
            self._attendance_policy_service = attendance_policy_service
            self._stop_event = threading.Event()
            self._thread = None
            self._running = False
            self._initializing = False
            self._error = None
            self._started_at = None
            self._stopped_at = None
            self._registered_names = []
            self._last_detections = []
            self._last_frame_jpeg = None
            self._performance = {}
            self._attendance_service = None
            self._initialization_progress_percent = 0
            self._initialization_stage = "Idle"

        def update_config(self, config):
            with self._lock:
                self._config = config

        def set_shared_attendance_service(self, attendance_service):
            with self._lock:
                self._shared_attendance_service = attendance_service

        def start(self):
            with self._lock:
                if self._running or self._initializing:
                    return self.status()

                self._stop_event.clear()
                self._error = None
                self._stopped_at = None
                self._last_detections = []
                self._last_frame_jpeg = None
                self._initialization_progress_percent = 1
                self._initialization_stage = "Starting worker"
                self._initializing = True
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
                return self.status()

        def stop(self):
            thread = None
            with self._lock:
                self._stop_event.set()
                thread = self._thread

            if thread and thread.is_alive():
                thread.join(timeout=5)

            return self.status()

        def status(self):
            with self._lock:
                return {
                    "camera_id": self._config.camera_id,
                    "company_id": self._config.company_id,
                    "camera_name": self._config.name,
                    "camera_source": self._config.camera_source,
                    "branch_id": self._config.branch_id,
                    "zone_type": self._config.zone_type,
                    "roi_enabled": self._config.roi_enabled,
                    "roi_polygon": self._config.roi_polygon or [],
                    "clahe_enabled": self._config.clahe_enabled,
                    "detection_confidence_threshold": self._config.detection_confidence_threshold,
                    "recognition_similarity_threshold": self._config.recognition_similarity_threshold,
                    "reid_similarity_threshold": self._config.reid_similarity_threshold,
                    "track_confirm_hits": self._config.track_confirm_hits,
                    "min_face_width": self._config.min_face_width,
                    "min_face_height": self._config.min_face_height,
                    "min_face_brightness": self._config.min_face_brightness,
                    "min_face_blur": self._config.min_face_blur,
                    "adaptive_frame_skip_enabled": self._config.adaptive_frame_skip_enabled,
                    "target_inference_ms": self._config.target_inference_ms,
                    "max_frame_skip": self._config.max_frame_skip,
                    "running": self._running,
                    "initializing": self._initializing,
                    "initialization_progress_percent": self._initialization_progress_percent,
                    "initialization_stage": self._initialization_stage,
                    "error": self._error,
                    "started_at": self._started_at,
                    "stopped_at": self._stopped_at,
                    "recognition_enabled": bool(self._registered_names),
                    "recognition_configured": Settings.recognition_enabled,
                    "registered_names": list(self._registered_names),
                    "registered_count": len(self._registered_names),
                    "last_detections": list(self._last_detections),
                    "attendance_count": len(self._attendance_records_unlocked()),
                    "performance": dict(self._performance),
                }

        def latest_frame_jpeg(self):
            with self._lock:
                return self._last_frame_jpeg

        def attendance_records(self):
            with self._lock:
                return self._attendance_records_unlocked()

        def _set_initialization_progress(self, percent, stage):
            with self._lock:
                self._initialization_progress_percent = max(0, min(100, int(percent)))
                self._initialization_stage = stage

        def _track_detections(self, tracks, detections, next_track_id):
            tracked_detections = []
            confirmed_events = []
            available_track_ids = set(tracks.keys())
            matched_track_ids = set()

            for det in detections:
                det_box = det["box"]
                best_track_id = None
                best_iou = 0.0

                for track_id in available_track_ids:
                    track = tracks[track_id]
                    iou = _box_iou(det_box, track["box"])
                    if iou > best_iou:
                        best_iou = iou
                        best_track_id = track_id

                if best_track_id is None or best_iou < Settings.track_iou_threshold:
                    best_track_id = self._match_reid_track(
                        tracks,
                        available_track_ids,
                        det.get("embedding"),
                    )

                if best_track_id is None:
                    best_track_id = next_track_id
                    next_track_id += 1
                    tracks[best_track_id] = {
                        "track_id": best_track_id,
                        "box": det_box,
                        "age": 0,
                        "observations": 0,
                        "missed": 0,
                        "candidate_identity": None,
                        "identity": None,
                        "candidate_hits": 0,
                        "best_similarity": 0.0,
                        "ema_similarity": 0.0,
                        "confirmed": False,
                        "last_seen": time.time(),
                        "last_embedding": None,
                        "reid_hits": 0,
                    }

                track = tracks[best_track_id]
                matched_track_ids.add(best_track_id)
                available_track_ids.discard(best_track_id)
                track["box"] = det_box
                track["age"] += 1
                track["observations"] += 1
                track["missed"] = 0
                track["last_seen"] = time.time()
                if det.get("embedding") is not None:
                    track["last_embedding"] = det["embedding"]

                if det["matched"] and det["employee_id"]:
                    similarity = float(det.get("similarity", 0.0))
                    if track["confirmed"]:
                        pass
                    elif track["candidate_identity"] is None:
                        track["candidate_identity"] = {
                            "name": det["name"],
                            "employee_id": det["employee_id"],
                            "similarity": similarity,
                            "accuracy": det.get("accuracy", 0.0),
                        }
                        track["candidate_hits"] = 1
                        track["ema_similarity"] = similarity
                        track["best_similarity"] = max(track["best_similarity"], similarity)
                    elif track["candidate_identity"]["employee_id"] == det["employee_id"]:
                        track["candidate_hits"] += 1
                        track["candidate_identity"] = {
                            "name": det["name"],
                            "employee_id": det["employee_id"],
                            "similarity": similarity,
                            "accuracy": det.get("accuracy", 0.0),
                        }
                        track["ema_similarity"] = (track["ema_similarity"] * 0.7) + (similarity * 0.3)
                        track["best_similarity"] = max(track["best_similarity"], similarity)
                    else:
                        track["candidate_identity"] = {
                            "name": det["name"],
                            "employee_id": det["employee_id"],
                            "similarity": similarity,
                            "accuracy": det.get("accuracy", 0.0),
                        }
                        track["candidate_hits"] = 1
                        track["ema_similarity"] = similarity
                        track["best_similarity"] = similarity

                if (
                    not track["confirmed"]
                    and track["candidate_identity"]
                    and track["candidate_hits"] >= self._track_confirm_hits()
                    and track["ema_similarity"] >= self._recognition_similarity_threshold()
                ):
                    track["identity"] = track["candidate_identity"]
                    track["confirmed"] = True
                    cached_identity = track["identity"]
                    confirmed_events.append(
                        {
                            "track_id": best_track_id,
                            "name": cached_identity["name"],
                            "employee_id": cached_identity["employee_id"],
                            "similarity": cached_identity["similarity"],
                            "accuracy": cached_identity["accuracy"],
                        }
                    )
                else:
                    cached_identity = track["identity"] or track["candidate_identity"]

                label = det["label"]
                matched = det["matched"]
                employee_id = det["employee_id"]
                accuracy = det["accuracy"]
                if cached_identity:
                    label = f"{cached_identity['name']} {track['ema_similarity'] * 100:.1f}%"
                    matched = True
                    employee_id = cached_identity["employee_id"]
                    accuracy = track["ema_similarity"] * 100

                tracked_detections.append(
                    {
                        **det,
                        "box": det_box,
                        "label": label,
                        "matched": matched,
                        "employee_id": employee_id,
                        "accuracy": accuracy,
                        "track_id": best_track_id,
                        "track_age": track["age"],
                        "track_observations": track["observations"],
                        "candidate_hits": track["candidate_hits"],
                        "identity_cached": bool(track["identity"]),
                        "candidate_identity": bool(track["candidate_identity"]),
                        "identity_confirmed": track["confirmed"],
                        "reid_hits": track.get("reid_hits", 0),
                    }
                )

            for track_id, track in list(tracks.items()):
                if track_id not in matched_track_ids:
                    track["missed"] += 1
                if track["missed"] > Settings.track_max_missed_frames:
                    tracks.pop(track_id, None)

            return tracked_detections, confirmed_events, next_track_id

        def _match_reid_track(self, tracks, available_track_ids, embedding):
            if embedding is None:
                return None

            best_track_id = None
            best_similarity = 0.0
            now = time.time()
            for track_id in available_track_ids:
                track = tracks[track_id]
                if now - track.get("last_seen", 0) > Settings.reid_max_age_seconds:
                    continue
                similarity = _embedding_similarity(embedding, track.get("last_embedding"))
                if similarity > best_similarity:
                    best_similarity = similarity
                    best_track_id = track_id

            if best_track_id is None or best_similarity < self._reid_similarity_threshold():
                return None

            tracks[best_track_id]["reid_hits"] = tracks[best_track_id].get("reid_hits", 0) + 1
            return best_track_id

        def _recognition_similarity_threshold(self):
            return (
                self._config.recognition_similarity_threshold
                if self._config.recognition_similarity_threshold is not None
                else Settings.similarity_threshold
            )

        def _reid_similarity_threshold(self):
            return (
                self._config.reid_similarity_threshold
                if self._config.reid_similarity_threshold is not None
                else Settings.reid_similarity_threshold
            )

        def _track_confirm_hits(self):
            configured_hits = (
                self._config.track_confirm_hits
                if self._config.track_confirm_hits is not None
                else Settings.track_confirm_hits
            )
            return max(1, int(configured_hits or 1))

        def _run(self):
            cap = None
            detection_worker = None

            try:
                from app.utils.database import SessionLocal
                from app.daos.cctv import AttendanceCCTVDAO, CCTVCompanySettingDAO

                runtime = self._runtime.get_runtime(progress_callback=self._set_initialization_progress)
                face_detector = runtime["face_detector"]
                face_recognizer = runtime["face_recognizer"]
                registered_names = runtime["registered_names"]

                self._set_initialization_progress(90, f"Opening camera {self._config.name}")
                print(f"CCTV Worker[{self._config.worker_key}]: Initializing camera and detection worker...")
                attendance_service = self._shared_attendance_service
                cap = LatestFrameCapture(_camera_source(self._config.camera_source))

                if not cap.is_opened():
                    raise RuntimeError(f"Could not open camera source: {self._config.camera_source}")

                self._set_initialization_progress(95, "Starting detection worker")
                detection_worker = DetectionWorker(
                    face_detector=face_detector,
                    face_recognizer=face_recognizer,
                    attendance_service=attendance_service,
                    detect_width=Settings.detect_width,
                    zone_type=self._config.zone_type,
                    roi_enabled=self._config.roi_enabled,
                    roi_polygon=self._config.roi_polygon,
                    clahe_enabled=self._config.clahe_enabled,
                    detection_confidence_threshold=self._config.detection_confidence_threshold,
                    recognition_similarity_threshold=self._config.recognition_similarity_threshold,
                    min_face_width=self._config.min_face_width,
                    min_face_height=self._config.min_face_height,
                    min_face_brightness=self._config.min_face_brightness,
                    min_face_blur=self._config.min_face_blur,
                    adaptive_frame_skip_enabled=self._config.adaptive_frame_skip_enabled,
                    target_inference_ms=self._config.target_inference_ms,
                    max_frame_skip=self._config.max_frame_skip,
                )

                with self._lock:
                    self._attendance_service = attendance_service
                    self._registered_names = list(registered_names)
                    self._started_at = datetime.now().isoformat()
                    self._running = True
                    self._initializing = False
                    self._initialization_progress_percent = 100
                    self._initialization_stage = "Ready"

                print(f"CCTV Worker[{self._config.worker_key}]: SYSTEM READY AND RUNNING.")

                tracks = {}
                next_track_id = 1
                last_detection_sequence = None
                tracked_detections = []
                identity_cache = {}
                identity_last_logged = {}
                cooldown_seconds = 30
                allow_attendance_after_tolerance = True
                time_tolerance_minutes = 0

                db = SessionLocal()
                try:
                    company_setting = CCTVCompanySettingDAO.get_by_company_id(db, self._config.company_id)
                    if company_setting:
                        cooldown_seconds = company_setting.cooldown_seconds
                        allow_attendance_after_tolerance = company_setting.allow_attendance_after_tolerance
                        time_tolerance_minutes = company_setting.time_tolerance_minutes
                        print(
                            f"CCTV Worker[{self._config.worker_key}]: "
                            f"Using company cooldown settings ({cooldown_seconds}s)"
                        )
                except Exception as error:
                    print(f"Warning: could not fetch company settings for {self._config.worker_key}: {error}")
                finally:
                    db.close()

                while not self._stop_event.is_set() and cap.is_opened():
                    ret, frame = cap.read()
                    if not ret:
                        time.sleep(0.05)
                        continue

                    detection_worker.update_frame(frame)
                    raw_detections = detection_worker.get_detections()
                    performance = detection_worker.get_metrics()
                    detection_sequence = performance.get("sequence")
                    if detection_sequence == last_detection_sequence:
                        frame_jpeg = self._encode_frame_jpeg(frame, tracked_detections)
                        with self._lock:
                            self._last_frame_jpeg = frame_jpeg
                            self._performance = performance
                        time.sleep(0.01)
                        continue
                    last_detection_sequence = detection_sequence

                    tracked_detections, confirmed_events, next_track_id = self._track_detections(
                        tracks,
                        raw_detections,
                        next_track_id,
                    )

                    current_time = time.time()
                    current_employee_ids = set()
                    for det in tracked_detections:
                        if not (det["matched"] and det["employee_id"] and det.get("identity_confirmed")):
                            continue

                        emp_id = det["employee_id"]
                        current_employee_ids.add(emp_id)
                        cache = identity_cache.get(emp_id)
                        similarity = float(det.get("similarity", 0.0))
                        if cache is None or cache["name"] != det["name"]:
                            cache = {
                                "name": det["name"],
                                "employee_id": emp_id,
                                "hits": 0,
                                "ema_similarity": 0.0,
                                "confirmed": False,
                                "last_seen": current_time,
                            }

                        cache["hits"] += 1
                        cache["ema_similarity"] = (
                            similarity
                            if cache["hits"] == 1
                            else (cache["ema_similarity"] * 0.7) + (similarity * 0.3)
                        )
                        cache["last_seen"] = current_time
                        identity_cache[emp_id] = cache

                        if not cache["confirmed"]:
                            if cache["hits"] == 1 and self._track_confirm_hits() > 1:
                                print(
                                    f"CCTV Worker[{self._config.worker_key}]: "
                                    f"Identity candidate {det['name']} detected, waiting for "
                                    f"{self._track_confirm_hits()} stable hits"
                                )
                            if cache["hits"] < self._track_confirm_hits():
                                continue
                            if cache["ema_similarity"] < self._recognition_similarity_threshold():
                                continue
                            cache["confirmed"] = True
                            print(
                                f"CCTV Worker[{self._config.worker_key}]: "
                                f"Identity confirmed for {det['name']}"
                            )

                        last_time = identity_last_logged.get(emp_id, 0)
                        if current_time - last_time <= cooldown_seconds:
                            continue

                        attendance_type = self._attendance_type()
                        detected_at = datetime.now(timezone.utc)
                        if not attendance_service.is_new(emp_id, attendance_type):
                            identity_last_logged[emp_id] = current_time
                            continue

                        identity_last_logged[emp_id] = current_time
                        photo_url = self._save_attendance_snapshot(
                            frame=frame,
                            detection=det,
                            employee_id=emp_id,
                            attendance_type=attendance_type,
                            detected_at=detected_at,
                        )
                        attendance_policy = self._attendance_policy_service.resolve(
                            company_id=self._config.company_id,
                            employee_id=emp_id,
                            attendance_type=attendance_type,
                            detected_at=detected_at,
                            branch_id=self._config.branch_id,
                            camera_id=self._config.camera_id,
                        )
                        company_timezone = self._attendance_policy_service._company_timezone(self._config.company_id)
                        if (
                            not allow_attendance_after_tolerance
                            and not attendance_allowed_after_tolerance(
                                attendance_policy,
                                detected_at,
                                attendance_type,
                                time_tolerance_minutes,
                                company_timezone,
                            )
                        ):
                            print(
                                f"CCTV Worker[{self._config.worker_key}]: "
                                f"Marked {attendance_type} for {det['name']} as exception "
                                "because it is outside the configured time tolerance."
                            )
                            attendance_policy["status"] = "pending_review"
                            attendance_policy["reason"] = "out_of_tolerance"
                            attendance_policy["explanation"] = "Attendance recorded outside allowed time tolerance."

                        db = SessionLocal()
                        try:
                            attendance_record = AttendanceCCTVDAO.record_attendance(
                                db=db,
                                company_id=self._config.company_id,
                                employee_id=emp_id,
                                employee_name=det["name"],
                                attendance_type=attendance_type,
                                detected_at=detected_at,
                                photo_url=photo_url,
                                client_reference_id=(
                                    str(self._config.camera_id) if self._config.camera_id else None
                                ),
                                attendance_policy=attendance_policy,
                            )
                            if not getattr(attendance_record, "is_new_attendance", True):
                                identity_last_logged[emp_id] = current_time
                                continue
                            attendance_service.mark(
                                emp_id,
                                det["name"],
                                attendance_type,
                                detected_at=detected_at,
                                attendance_policy=attendance_policy,
                                timezone_name=company_timezone,
                            )
                            attendance_service.attach_photo(emp_id, attendance_type, photo_url)
                            print(
                                f"CCTV Worker[{self._config.worker_key}]: "
                                f"Recorded attendance_cctv {attendance_type} "
                                f"for {det['name']} (ID: {attendance_record.id})"
                            )

                        except Exception as error:
                            print(f"Error recording CCTV attendance for {self._config.worker_key}: {error}")
                            continue
                        finally:
                            db.close()

                        try:
                            publish_result = self._rabbitmq_publisher.publish_attendance(
                                attendance_type,
                                {
                                    "company_id": str(self._config.company_id),
                                    "branch_id": str(self._config.branch_id) if self._config.branch_id else None,
                                    "camera_id": str(self._config.camera_id) if self._config.camera_id else None,
                                    "employee_id": str(emp_id),
                                    "employee_name": det["name"],
                                    "confidence_score": cache["ema_similarity"],
                                    "detected_at": detected_at.isoformat(),
                                    "attendance_mode": "multi_camera_zone_type",
                                    "attendance_policy": attendance_policy,
                                },
                            )
                            print(
                                f"CCTV Worker[{self._config.worker_key}]: "
                                f"Published attendance {attendance_type} event "
                                f"({publish_result['message_id']})"
                            )
                        except RabbitMQPublishError as error:
                            print(
                                f"Error publishing attendance event for "
                                f"{self._config.worker_key}: {error}"
                            )

                    for emp_id in list(identity_cache.keys()):
                        if emp_id in current_employee_ids:
                            continue
                        identity_cache[emp_id]["hits"] = 0
                        if current_time - identity_cache[emp_id]["last_seen"] > Settings.reid_max_age_seconds:
                            identity_cache.pop(emp_id, None)

                    detections = self._serialize_detections(tracked_detections)
                    frame_jpeg = self._encode_frame_jpeg(frame, tracked_detections)
                    with self._lock:
                        self._last_detections = detections
                        self._last_frame_jpeg = frame_jpeg
                        self._performance = performance

                    time.sleep(0.01)

                if not self._stop_event.is_set():
                    with self._lock:
                        self._error = "Camera source is not opened or stopped sending frames."
            except Exception as error:
                print(f"CRITICAL ERROR in CCTV Worker[{self._config.worker_key}]: {error}")
                import traceback
                traceback.print_exc()
                with self._lock:
                    self._error = str(error)
                    self._initializing = False
            finally:
                if detection_worker:
                    detection_worker.stop()
                if cap:
                    cap.release()

                with self._lock:
                    self._running = False
                    self._initializing = False
                    self._stopped_at = datetime.now().isoformat()

        def _attendance_records_unlocked(self):
            if not self._attendance_service:
                return {}
            return self._attendance_service.records()

        def _attendance_type(self):
            zone_type = (self._config.zone_type or "").strip().lower()
            if zone_type in {"out", "exit", "checkout", "check-out", "attendance_out"}:
                return "out"
            return "in"

        def _save_attendance_snapshot(self, frame, detection, employee_id, attendance_type, detected_at=None):
            if not Settings.attendance_snapshot_enabled:
                return None

            try:
                employee_dir = ATTENDANCE_SNAPSHOT_DIR / str(employee_id)
                employee_dir.mkdir(parents=True, exist_ok=True)

                detected_at = detected_at or datetime.now(timezone.utc)
                snapshot_date = detected_at.date().isoformat()
                filename = f"{snapshot_date}_{attendance_type}.png"
                file_path = employee_dir / filename

                frame_height, frame_width = frame.shape[:2]
                x1, y1, x2, y2 = self._snapshot_box_with_padding(
                    detection["box"],
                    frame_width,
                    frame_height,
                )

                snapshot = frame[y1:y2, x1:x2] if x2 > x1 and y2 > y1 else frame
                if snapshot.size == 0:
                    snapshot = frame

                if not cv2.imwrite(str(file_path), snapshot):
                    print(
                        f"Warning: failed to save attendance snapshot "
                        f"for employee {employee_id} ({attendance_type})"
                    )
                    return None

                return (
                    f"{Settings.attendance_snapshot_base_url}/"
                    f"{employee_id}/{filename}"
                )
            except Exception as error:
                print(
                    f"Warning: error saving attendance snapshot for "
                    f"employee {employee_id} ({attendance_type}): {error}"
                )
                return None

        def _snapshot_box_with_padding(self, box, frame_width, frame_height):
            x1, y1, x2, y2 = [int(value) for value in box]
            box_width = max(1, x2 - x1)
            box_height = max(1, y2 - y1)
            padding_ratio = max(0.0, Settings.attendance_snapshot_padding_ratio)
            pad_x = int(box_width * padding_ratio)
            pad_top = int(box_height * padding_ratio)
            pad_bottom = int(box_height * padding_ratio * 1.6)

            return (
                max(0, min(frame_width, x1 - pad_x)),
                max(0, min(frame_height, y1 - pad_top)),
                max(0, min(frame_width, x2 + pad_x)),
                max(0, min(frame_height, y2 + pad_bottom)),
            )

        def _serialize_detections(self, detections):
            serialized = []
            for det in detections:
                x1, y1, x2, y2 = det["box"]
                serialized.append(
                    {
                        "box": {"x1": x1, "y1": y1, "x2": x2, "y2": y2},
                        "label": det["label"],
                        "color_bgr": list(det["color"]),
                        "employee_id": det.get("employee_id"),
                        "matched": det.get("matched", False),
                        "accuracy": det.get("accuracy", 0),
                        "similarity": det.get("similarity", 0),
                        "track_id": det.get("track_id"),
                        "identity_confirmed": det.get("identity_confirmed", False),
                        "reid_hits": det.get("reid_hits", 0),
                        "age": det.get("age"),
                    }
                )
            return serialized

        def _encode_frame_jpeg(self, frame, detections):
            annotated_frame = frame.copy()
            self._draw_roi_overlay(annotated_frame)
            for det in detections:
                x1, y1, x2, y2 = det["box"]
                box_color = det["color"]
                display_text = det["label"]
                cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), box_color, 2)
                cv2.putText(
                    annotated_frame,
                    display_text,
                    (x1, max(20, y1 - 10)),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.6,
                    box_color,
                    2,
                )

            success, buffer = cv2.imencode(".jpg", annotated_frame)
            return buffer.tobytes() if success else None

        def _draw_roi_overlay(self, frame):
            if not (self._config.roi_enabled and self._config.roi_polygon):
                return
            frame_height, frame_width = frame.shape[:2]
            points = _normalized_roi_to_pixels(self._config.roi_polygon, frame_width, frame_height)
            if not points:
                return
            polygon = np.array(points, dtype=np.int32)
            cv2.polylines(frame, [polygon], True, (255, 200, 0), 2)
            overlay = frame.copy()
            cv2.fillPoly(overlay, [polygon], (255, 200, 0))
            cv2.addWeighted(overlay, 0.12, frame, 0.88, 0, frame)

    def __init__(self):
        self._lock = threading.RLock()
        self._runtime = self.SharedRecognitionRuntime()
        self._shared_attendance_service = AttendanceService()
        self._rabbitmq_publisher = RabbitMQPublisher()
        self._attendance_policy_service = AttendancePolicyService(
            policy_url=Settings.attendance_policy_url,
            token=Settings.hrms_token,
        )
        self._workers = {}
        self._camera_source_overrides = {}
        self._system_started = False
        self._system_thread = None
        self._system_initializing = False
        self._system_initialization_progress_percent = 0
        self._system_initialization_stage = "Idle"
        self._system_error = None

    def prepare_system(self):
        with self._lock:
            runtime_status = self._runtime.status()
            if runtime_status["loaded"]:
                self._system_initializing = False
                self._system_error = None
                self._system_initialization_progress_percent = 100
                self._system_initialization_stage = "Ready"
                return self.status()

            if self._system_initializing:
                return self.status()

            self._system_initializing = True
            self._system_error = None
            self._system_initialization_progress_percent = 1
            self._system_initialization_stage = "Preparing runtime"
            self._system_thread = threading.Thread(target=self._preload_runtime, daemon=True)
            self._system_thread.start()
        return self.status()

    def start_system(self, company_id=None):
        with self._lock:
            self._system_started = True
            runtime_status = self._runtime.status()
            if runtime_status["loaded"]:
                self._system_initializing = False
                self._system_error = None
                self._system_initialization_progress_percent = 100
                self._system_initialization_stage = "Ready"
                return self.status()

            if self._system_initializing:
                return self.status()

        return self.prepare_system()

    def stop_system(self):
        with self._lock:
            self._system_started = False
            self._system_initializing = False
            self._system_initialization_stage = "System stopped"
            workers = list(self._workers.values())

        for worker in workers:
            worker.stop()
        return self.status()

    def start(self, camera_id=None, employee_id=None, company_id=None):
        with self._lock:
            system_started = self._system_started
            system_initializing = self._system_initializing
        runtime_status = self._runtime.status()
        if not system_started:
            raise ValueError("System is not started. Start system first.")
        if system_initializing or not runtime_status["loaded"]:
            raise ValueError("System is still initializing. Wait until system is ready.")

        if not self._has_active_workers():
            self._shared_attendance_service = AttendanceService()
            with self._lock:
                for worker in self._workers.values():
                    worker.set_shared_attendance_service(self._shared_attendance_service)

        camera_configs = self._resolve_camera_configs(camera_id, company_id)
        camera_start_event = None
        if camera_id and employee_id:
            camera_start_event = self._rabbitmq_publisher.publish_camera_start(camera_id, employee_id)

        for config in camera_configs:
            self._get_or_create_worker(config).start()
        status = self.status(camera_id, company_id=company_id)
        if camera_start_event:
            status["camera_start_event"] = camera_start_event
        return status

    def _preload_runtime(self):
        try:
            self._runtime.get_runtime(progress_callback=self._set_system_initialization_progress)
            with self._lock:
                self._system_initializing = False
                self._system_error = None
                self._system_initialization_progress_percent = 100
                self._system_initialization_stage = "Ready"
        except Exception as error:
            with self._lock:
                self._system_initializing = False
                self._system_error = str(error)
                self._system_initialization_stage = "Error"

    def _set_system_initialization_progress(self, percent, stage):
        with self._lock:
            self._system_initialization_progress_percent = max(0, min(100, int(percent)))
            self._system_initialization_stage = stage

    def stop(self, camera_id=None, company_id=None):
        if camera_id is None:
            if company_id is None:
                return self.stop_system()
            workers = self._selected_workers(company_id=company_id)
            for worker in workers:
                worker.stop()
            return self.status(company_id=company_id)

        workers = self._selected_workers(camera_id)
        for worker in workers:
            worker.stop()
        return self.status(camera_id, company_id=company_id)

    def remove_camera(self, camera_id):
        worker = self._worker_for_camera_id(camera_id)
        if worker:
            worker.stop()

        worker_key = str(camera_id)
        with self._lock:
            self._workers.pop(worker_key, None)
            self._camera_source_overrides.pop(worker_key, None)

    def status(self, camera_id=None, company_id=None):
        if camera_id:
            worker = self._worker_for_camera_id(camera_id)
            status = worker.status() if worker else self._inactive_camera_status(camera_id)
            if company_id and status.get("company_id") and str(status["company_id"]) != str(company_id):
                return self._inactive_camera_status(camera_id)
            return status

        with self._lock:
            workers = list(self._workers.values())

        worker_statuses = [
            status for status in (worker.status() for worker in workers)
            if not company_id or str(status.get("company_id")) == str(company_id)
        ]
        primary_status = self._primary_worker_status(worker_statuses)
        runtime_status = self._runtime.status()
        aggregate_attendance = self.attendance_records()
        with self._lock:
            system_started = self._system_started
            runtime_initializing = self._system_initializing or runtime_status["loading"]
            system_initializing = system_started and runtime_initializing
            system_progress = self._system_initialization_progress_percent
            system_stage = self._system_initialization_stage
            system_error = self._system_error

        return {
            "system_started": system_started,
            "system_initializing": system_initializing,
            "system_ready": bool(system_started and runtime_status["loaded"] and not system_initializing),
            "runtime_initializing": runtime_initializing,
            "runtime_ready": runtime_status["loaded"],
            "runtime_initialization_progress_percent": system_progress,
            "runtime_initialization_stage": system_stage,
            "running": any(status["running"] for status in worker_statuses),
            "initializing": system_initializing or any(status["initializing"] for status in worker_statuses),
            "initialization_progress_percent": (
                system_progress if system_initializing else primary_status.get("initialization_progress_percent", 0)
            ),
            "initialization_stage": (
                system_stage if system_initializing else primary_status.get("initialization_stage", "Idle")
            ),
            "error": self._combine_errors(worker_statuses, runtime_status, system_error),
            "started_at": primary_status.get("started_at"),
            "stopped_at": primary_status.get("stopped_at"),
            "company_id": str(company_id) if company_id else None,
            "camera_source": primary_status.get("camera_source", camera_source_label()),
            "recognition_enabled": runtime_status["registered_count"] > 0,
            "recognition_configured": Settings.recognition_enabled,
            "registered_names": runtime_status["registered_names"],
            "registered_count": runtime_status["registered_count"],
            "last_detections": primary_status.get("last_detections", []),
            "performance": primary_status.get("performance", {}),
            "attendance_count": len(aggregate_attendance),
            "worker_count": len(worker_statuses),
            "running_count": sum(1 for status in worker_statuses if status["running"]),
            "workers": worker_statuses,
            "runtime": runtime_status,
        }

    def set_camera_source(self, camera_source, camera_id=None):
        camera_source = str(camera_source).strip()
        if not camera_source:
            raise ValueError("Camera source cannot be empty.")

        worker_key = str(camera_id) if camera_id else "__default__"
        with self._lock:
            self._camera_source_overrides[worker_key] = camera_source
            worker = self._workers.get(worker_key)

        if worker:
            worker_status = worker.status()
            config = self.CameraConfig(
                camera_id=worker_status["camera_id"],
                company_id=worker_status.get("company_id"),
                name=worker_status["camera_name"],
                camera_source=camera_source,
                branch_id=worker_status["branch_id"],
                zone_type=worker_status["zone_type"],
                roi_enabled=worker_status.get("roi_enabled", False),
                roi_polygon=worker_status.get("roi_polygon", []),
                clahe_enabled=worker_status.get("clahe_enabled", False),
                detection_confidence_threshold=worker_status.get("detection_confidence_threshold"),
                recognition_similarity_threshold=worker_status.get("recognition_similarity_threshold"),
                reid_similarity_threshold=worker_status.get("reid_similarity_threshold"),
                track_confirm_hits=worker_status.get("track_confirm_hits"),
                min_face_width=worker_status.get("min_face_width"),
                min_face_height=worker_status.get("min_face_height"),
                min_face_brightness=worker_status.get("min_face_brightness"),
                min_face_blur=worker_status.get("min_face_blur"),
                adaptive_frame_skip_enabled=worker_status.get("adaptive_frame_skip_enabled", False),
                target_inference_ms=worker_status.get("target_inference_ms"),
                max_frame_skip=worker_status.get("max_frame_skip"),
            )
            worker.update_config(config)
            applies_immediately = not worker_status["running"] and not worker_status["initializing"]
        else:
            applies_immediately = True
            worker_status = None

        return {
            "camera_id": None if worker_key == "__default__" else worker_key,
            "camera_source": camera_source,
            "running": worker_status["running"] if worker_status else False,
            "applies_immediately": applies_immediately,
        }

    def latest_frame_jpeg(self, camera_id=None):
        worker = self._worker_for_camera_id(camera_id) if camera_id else self._primary_worker()
        return worker.latest_frame_jpeg() if worker else None

    def attendance_records(self, camera_id=None):
        if camera_id:
            worker = self._worker_for_camera_id(camera_id)
            return worker.attendance_records() if worker else {}
        return self._shared_attendance_service.records()

    def record_workin_response(self, attendance_type, payload):
        return self._shared_attendance_service.record_workin_response(attendance_type, payload)

    def _resolve_camera_configs(self, camera_id=None, company_id=None):
        from app.utils.database import SessionLocal
        from app.daos.cctv import CCTVCameraDAO

        db = SessionLocal()
        try:
            if camera_id:
                camera = CCTVCameraDAO.get_by_id(db, camera_id)
                if camera:
                    return [self._config_from_camera(camera)]
                if str(camera_id) == "__default__":
                    return [self._default_camera_config()]
                raise ValueError(f"Camera {camera_id} not found.")

            # Start cameras for ALL active companies
            from app.models.cctv import CCTVCamera, CCTVCompanySetting
            
            cameras_query = db.query(CCTVCamera).join(
                CCTVCompanySetting, 
                CCTVCamera.company_id == CCTVCompanySetting.company_id
            ).filter(CCTVCompanySetting.enabled == True)
            if company_id:
                cameras_query = cameras_query.filter(CCTVCamera.company_id == company_id)
            cameras = cameras_query.all()
            
            if cameras:
                return [self._config_from_camera(camera) for camera in cameras]
            return []
        finally:
            db.close()

    def _config_from_camera(self, camera):
        worker_key = str(camera.id)
        return self.CameraConfig(
            camera_id=str(camera.id),
            company_id=str(camera.company_id),
            name=camera.name,
            camera_source=self._camera_source_overrides.get(worker_key, camera.rtsp_url),
            branch_id=str(camera.branch_id) if camera.branch_id else None,
            zone_type=camera.zone_type,
            roi_enabled=getattr(camera, "roi_enabled", False),
            roi_polygon=getattr(camera, "roi_polygon", None) or [],
            clahe_enabled=getattr(camera, "clahe_enabled", False),
            detection_confidence_threshold=getattr(camera, "detection_confidence_threshold", None),
            recognition_similarity_threshold=getattr(camera, "recognition_similarity_threshold", None),
            reid_similarity_threshold=getattr(camera, "reid_similarity_threshold", None),
            track_confirm_hits=getattr(camera, "track_confirm_hits", None),
            min_face_width=getattr(camera, "min_face_width", None),
            min_face_height=getattr(camera, "min_face_height", None),
            min_face_brightness=getattr(camera, "min_face_brightness", None),
            min_face_blur=getattr(camera, "min_face_blur", None),
            adaptive_frame_skip_enabled=getattr(camera, "adaptive_frame_skip_enabled", False),
            target_inference_ms=getattr(camera, "target_inference_ms", None),
            max_frame_skip=getattr(camera, "max_frame_skip", None),
        )

    def _default_camera_config(self):
        return self.CameraConfig(
            camera_id=None,
            company_id=None,
            name="Default CCTV Camera",
            camera_source=self._camera_source_overrides.get("__default__", camera_source_label()),
            branch_id=str(Settings.branch_id) if Settings.branch_id else None,
            zone_type="attendance",
            roi_enabled=False,
            roi_polygon=[],
            clahe_enabled=False,
            detection_confidence_threshold=None,
            recognition_similarity_threshold=None,
            reid_similarity_threshold=None,
            track_confirm_hits=None,
            min_face_width=None,
            min_face_height=None,
            min_face_brightness=None,
            min_face_blur=None,
            adaptive_frame_skip_enabled=False,
            target_inference_ms=None,
            max_frame_skip=None,
        )

    def _get_or_create_worker(self, config):
        with self._lock:
            worker = self._workers.get(config.worker_key)
            if worker is None:
                worker = self.CameraWorker(
                    config,
                    self._runtime,
                    self._shared_attendance_service,
                    self._rabbitmq_publisher,
                    self._attendance_policy_service,
                )
                self._workers[config.worker_key] = worker
            else:
                worker.update_config(config)
                worker.set_shared_attendance_service(self._shared_attendance_service)
            return worker

    def _selected_workers(self, camera_id=None, company_id=None):
        if camera_id:
            worker = self._worker_for_camera_id(camera_id)
            if worker and company_id and str(worker.status().get("company_id")) != str(company_id):
                return []
            return [worker] if worker else []

        if company_id:
            with self._lock:
                workers = list(self._workers.values())
            return [worker for worker in workers if str(worker.status().get("company_id")) == str(company_id)]

        with self._lock:
            return list(self._workers.values())

    def _worker_for_camera_id(self, camera_id):
        camera_key = str(camera_id) if camera_id else "__default__"
        with self._lock:
            return self._workers.get(camera_key)

    def _primary_worker(self):
        with self._lock:
            workers = list(self._workers.values())

        for worker in workers:
            status = worker.status()
            if status["running"] or status["initializing"]:
                return worker
        return workers[0] if workers else None

    def _primary_worker_status(self, worker_statuses):
        for status in worker_statuses:
            if status["running"] or status["initializing"]:
                return status
        return worker_statuses[0] if worker_statuses else {}

    def _inactive_camera_status(self, camera_id):
        return {
            "camera_id": str(camera_id),
            "company_id": None,
            "camera_name": None,
            "camera_source": None,
            "branch_id": None,
            "zone_type": None,
            "roi_enabled": False,
            "roi_polygon": [],
            "clahe_enabled": False,
            "detection_confidence_threshold": None,
            "recognition_similarity_threshold": None,
            "reid_similarity_threshold": None,
            "track_confirm_hits": None,
            "min_face_width": None,
            "min_face_height": None,
            "min_face_brightness": None,
            "min_face_blur": None,
            "adaptive_frame_skip_enabled": False,
            "target_inference_ms": None,
            "max_frame_skip": None,
            "running": False,
            "initializing": False,
            "initialization_progress_percent": 0,
            "initialization_stage": "Idle",
            "error": None,
            "started_at": None,
            "stopped_at": None,
            "recognition_enabled": False,
            "recognition_configured": Settings.recognition_enabled,
            "registered_names": [],
            "registered_count": 0,
            "last_detections": [],
            "performance": {},
            "attendance_count": 0,
        }

    def _combine_errors(self, worker_statuses, runtime_status, system_error=None):
        errors = [status["error"] for status in worker_statuses if status["error"]]
        if system_error:
            errors.append(system_error)
        if runtime_status["error"]:
            errors.append(runtime_status["error"])
        return "; ".join(errors) if errors else None

    def _has_active_workers(self):
        with self._lock:
            workers = list(self._workers.values())
        return any(worker.status()["running"] or worker.status()["initializing"] for worker in workers)


cctv_recognition_service = CCTVRecognitionService()
