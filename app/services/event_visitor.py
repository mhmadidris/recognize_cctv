import hashlib
import logging
import threading
import queue
import math
import time
import uuid
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import cv2

import app.services.cctv_recognition as cctv_ml
from app.daos.event_visitor import EventVisitorStore
from app.utils.timezone import DEFAULT_TIMEZONE, resolve_timezone


def _env(name, default=None):
    return cctv_ml._env(name, default)


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


def _env_bool(name, default=False):
    value = _env(name, "")
    if value in (None, ""):
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _is_video_file_source(source):
    if not isinstance(source, str):
        return False
    if source.startswith("rtsp://"):
        return False
    return Path(source).suffix.lower() in {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm"}


def _resize_to_full_hd(frame):
    """Keep CPU processing bounded while preserving the source aspect ratio."""
    max_width, max_height = 1920, 1080
    height, width = frame.shape[:2]
    scale = min(max_width / width, max_height / height, 1.0)
    if scale >= 1.0:
        return frame
    return cv2.resize(frame, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)


def _resize_to_stream(frame):
    """Keep MJPEG preview encoding affordable on CPU-only deployments."""
    max_width, max_height = 1280, 720
    height, width = frame.shape[:2]
    scale = min(max_width / width, max_height / height, 1.0)
    if scale >= 1.0:
        return frame
    return cv2.resize(frame, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)


class SharedCameraPipeline:
    """Reads one camera source and fans out the latest frame to event sessions."""

    def __init__(self, source):
        self.source = source
        self.subscribers = set()
        self._lock = threading.RLock()
        self._running = False
        self._thread = None

    def subscribe(self):
        subscriber = queue.Queue(maxsize=1)
        with self._lock:
            self.subscribers.add(subscriber)
            if not self._running:
                self._running = True
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
        return subscriber

    def unsubscribe(self, subscriber):
        with self._lock:
            self.subscribers.discard(subscriber)
            if not self.subscribers:
                self._running = False

    def _run(self):
        cap = cv2.VideoCapture(self.source)
        if not _is_video_file_source(self.source):
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        if not cap.isOpened():
            with self._lock:
                self._running = False
            return
        try:
            while self._running:
                ret, frame = cap.read()
                if not ret:
                    time.sleep(0.05)
                    continue
                frame = _resize_to_full_hd(frame)
                with self._lock:
                    subscribers = tuple(self.subscribers)
                for subscriber in subscribers:
                    try:
                        subscriber.put_nowait(frame)
                    except queue.Full:
                        try:
                            subscriber.get_nowait()
                        except queue.Empty:
                            pass
                        try:
                            subscriber.put_nowait(frame)
                        except queue.Full:
                            pass
        finally:
            cap.release()


class EventVisitorGenderClassifier:
    labels = ("male", "female")
    mean_values = (78.4263377603, 87.7689143744, 114.895847746)

    def __init__(self, model_path, config_path, confidence_threshold):
        self.enabled = bool(model_path and config_path)
        self.confidence_threshold = confidence_threshold
        self.net = None
        if self.enabled:
            if not Path(model_path).exists() or not Path(config_path).exists():
                print("Event Visitor gender classifier disabled: model/config file not found.")
                self.enabled = False
                return
            self.net = cv2.dnn.readNet(model_path, config_path)

    def classify(self, face_image):
        if not self.enabled or face_image is None or face_image.size == 0:
            return "unknown", 0.0

        blob = cv2.dnn.blobFromImage(
            face_image,
            1.0,
            (227, 227),
            self.mean_values,
            swapRB=False,
        )
        self.net.setInput(blob)
        predictions = self.net.forward()[0]
        class_id = int(predictions.argmax())
        confidence = float(predictions[class_id])
        if confidence < self.confidence_threshold:
            return "unknown", confidence
        return self.labels[class_id], confidence


class EventVisitorPersonDetector:
    person_class_ids = {0}

    def __init__(self, model_path, detect_width, confidence_threshold):
        self.enabled = bool(model_path)
        self.model = None
        self.detect_width = detect_width
        self.confidence_threshold = confidence_threshold
        self.device = cctv_ml.DEVICE
        if self.enabled:
            if not Path(model_path).exists():
                print("Event Visitor person detector disabled: model file not found.")
                self.enabled = False
                return
            self.model = cctv_ml.YOLO(model_path)

    def detect(self, frame):
        if not self.enabled or self.model is None or frame is None or frame.size == 0:
            return []

        frame_height, frame_width = frame.shape[:2]
        scale = min(1.0, self.detect_width / frame_width)
        detect_frame = cv2.resize(frame, (int(frame_width * scale), int(frame_height * scale)))
        results = self.model(detect_frame, device=self.device, verbose=False)
        boxes = []

        for result in results:
            for box in result.boxes:
                class_id = int(box.cls[0]) if hasattr(box, "cls") else 0
                confidence = float(box.conf[0])
                if class_id not in self.person_class_ids or confidence < self.confidence_threshold:
                    continue
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                if scale != 1.0:
                    x1 = int(x1 / scale)
                    y1 = int(y1 / scale)
                    x2 = int(x2 / scale)
                    y2 = int(y2 / scale)
                boxes.append((max(0, x1), max(0, y1), min(frame_width, x2), min(frame_height, y2), confidence))

        return boxes


class EventVisitorVectorStore:
    def __init__(self, db_path, collection_name):
        self.client = cctv_ml.chromadb.PersistentClient(path=db_path)
        self.collection_name = collection_name
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def reset(self):
        try:
            self.client.delete_collection(name=self.collection_name)
        except Exception:
            pass
        self.collection = self.client.get_or_create_collection(
            name=self.collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def delete(self):
        self.client.delete_collection(name=self.collection_name)

    def query_visitor(self, embedding):
        results = self.collection.query(
            query_embeddings=[embedding.tolist()],
            n_results=1,
        )
        if not results["ids"] or not results["ids"][0]:
            return None

        visitor_id = results["ids"][0][0]
        distance = results["distances"][0][0]
        metadata = (results["metadatas"][0][0] or {}) if results.get("metadatas") else {}
        return {
            "id": visitor_id,
            "similarity": 1 - distance,
            "metadata": metadata,
        }

    def upsert_visitor(self, embedding, visitor_id, metadata):
        clean_meta = {k: v for k, v in metadata.items() if v is not None}
        self.collection.upsert(
            ids=[visitor_id],
            embeddings=[embedding.tolist()],
            metadatas=[clean_meta],
        )

    def update_seen(self, visitor_id, metadata):
        seen_count = int(metadata.get("seen_count") or 0) + 1
        metadata = {
            **metadata,
            "seen_count": seen_count,
            "last_seen_at": datetime.now(timezone.utc).isoformat(),
        }
        clean_meta = {k: v for k, v in metadata.items() if v is not None}
        self.collection.update(ids=[visitor_id], metadatas=[clean_meta])
        return metadata


class EventVisitorService:
    def __init__(self, store=None, company_id=None, event_id=None):
        self.company_id = str(company_id) if company_id else None
        self.event_id = str(event_id) if event_id else None
        self.store = store or EventVisitorStore(company_id=company_id, event_id=event_id)
        self._settings_loaded = False
        self._lock = threading.RLock()
        self.running = False
        self.camera_source = None
        self.thread = None
        self.face_detector = None
        self.embedder = None
        self.gender_classifier = None
        self.person_detector = None
        self.vector_store = None
        self.session_id = None
        self.active_tracks = {}
        self.next_track_id = 1
        self.db_queue = queue.Queue()
        self.db_thread = None
        self._frame_queue = None
        self._camera_pipeline = None
        self.stats = {
            "in_count": 0,
            "out_count": 0,
            "total_count": 0,
            "unique_visitor_count": 0,
            "male_count": 0,
            "female_count": 0,
            "unknown_gender_count": 0,
            "body_gender_count": 0,
            "duplicate_face_count": 0,
            "faces_detected": 0,
            "active_track_count": 0,
            "system_started": False,
            "running": False,
            "last_visitor_at": None,
        }
        self.current_frame = None
        self.current_jpg_bytes = None
        self.frame_id = 0

    def timezone_name(self):
        from app.daos.cctv import CCTVCompanySettingDAO
        from app.utils.database import SessionLocal
        if not self.company_id:
            return DEFAULT_TIMEZONE
        with SessionLocal() as db:
            setting = CCTVCompanySettingDAO.get_by_company_id(db, self.company_id)
            return getattr(setting, "timezone", None) or DEFAULT_TIMEZONE

    def _load_settings(self):
        if not self._settings_loaded:
            for key, value in self.store.load_settings().items():
                if key == "camera_source" and str(value).isdigit():
                    value = int(value)
                setattr(self, key, value)
            self._settings_loaded = True

    def _save_setting(self, key, value):
        with self._lock:
            self._load_settings()
            self.store.save_settings(**{key: value})
            if key == "camera_source" and str(value).isdigit():
                value = int(value)
            setattr(self, key, value)

    def set_camera_source(self, source):
        self._save_setting("camera_source", str(source))

    def set_line_position(self, position: float):
        self._save_setting("line_position", max(0.1, min(0.9, position)))

    def set_line_orientation(self, orientation: str):
        if orientation not in ["horizontal", "vertical"]:
            raise ValueError("Invalid line orientation")
        self._save_setting("line_orientation", orientation)

    def set_reverse_direction(self, reverse: bool):
        self._save_setting("reverse_direction", bool(reverse))

    def set_model_tier(self, tier: str):
        if tier not in ["n", "s", "m", "l", "x"]:
            raise ValueError("Invalid model tier")
        self._save_setting("model_tier", tier)


    def _db_worker(self):
        # Continue draining queued records after stop so the final detections
        # are persisted before the session is reported as finished.
        while self.running or not self.db_queue.empty():
            try:
                task = self.db_queue.get(timeout=1.0)
                if task is None:
                    continue
                visitor_id, visitor_label, gender, direction, now = task
                try:
                    self.store.record_event(
                        session_id=self.session_id,
                        visitor_id=visitor_id,
                        visitor_label=visitor_label,
                        camera_source=str(self.camera_source),
                        gender=gender,
                        direction=direction,
                        detected_at=now,
                    )
                except Exception as e:
                    logging.getLogger(__name__).exception("DB worker failed to write event")
            except queue.Empty:
                pass
            except Exception:
                pass

    def _record_event(self, visitor_id, visitor_label, gender, direction, now):
        if self.running:
            self.db_queue.put((visitor_id, visitor_label, gender, direction, now))
            return
        self.store.record_event(
            session_id=self.session_id,
            visitor_id=visitor_id,
            visitor_label=visitor_label,
            camera_source=str(self.camera_source),
            gender=gender,
            direction=direction,
            detected_at=now,
        )

    def _initialize_face_pipeline(self, reset_vector_store=True):
        cctv_ml._load_ml_dependencies()

        model_path = _env("EVENT_VISITOR_FACE_MODEL_PATH", cctv_ml.Settings.model_path)
        if not model_path:
            model_path = "./models/yolo/yolo26n-face.pt"

        vector_db_path = _env("EVENT_VISITOR_VECTOR_DB_PATH", cctv_ml.Settings.vector_db_path)
        if not vector_db_path:
            vector_db_path = "./vector_db"

        collection = _env("EVENT_VISITOR_VECTOR_DB_COLLECTION", "event_visitors")
        if self.event_id:
            event_suffix = re.sub(r"[^a-zA-Z0-9_-]", "_", self.event_id)[:36]
            collection = f"{collection}_{event_suffix}"
        self.similarity_threshold = _env_float(
            "EVENT_VISITOR_SIMILARITY_THRESHOLD",
            cctv_ml.Settings.similarity_threshold or 0.8,
        )
        self.detect_width = _env_int("EVENT_VISITOR_DETECT_WIDTH", cctv_ml.Settings.detect_width or 416)
        self.detect_every_n_frames = max(1, _env_int("EVENT_VISITOR_DETECT_EVERY_N_FRAMES", 8))
        self.track_iou_threshold = _env_float("EVENT_VISITOR_TRACK_IOU_THRESHOLD", 0.25)
        self.track_max_missed_frames = _env_int("EVENT_VISITOR_TRACK_MAX_MISSED_FRAMES", 8)
        gender_model_path = _env("EVENT_VISITOR_GENDER_MODEL_PATH", "")
        gender_config_path = _env("EVENT_VISITOR_GENDER_CONFIG_PATH", "")
        gender_confidence = _env_float("EVENT_VISITOR_GENDER_CONFIDENCE_THRESHOLD", 0.65)
        person_model_path = _env("EVENT_VISITOR_PERSON_MODEL_PATH", "")
        person_confidence = _env_float("EVENT_VISITOR_PERSON_CONFIDENCE_THRESHOLD", 0.45)

        self.face_detector = cctv_ml.FaceDetector(
            model_path=model_path,
            face_size=cctv_ml.Settings.face_size,
            crop_margin=cctv_ml.Settings.face_crop_margin or 0.25,
            registered_min_confidence=cctv_ml.Settings.registered_face_min_confidence or 0.4,
            detection_min_confidence=cctv_ml.Settings.detection_min_confidence or 0.5,
        )
        self.embedder = cctv_ml.FaceNet()
        self.gender_classifier = EventVisitorGenderClassifier(
            gender_model_path,
            gender_config_path,
            gender_confidence,
        )
        self.person_detector = EventVisitorPersonDetector(
            person_model_path,
            self.detect_width,
            person_confidence,
        )
        self.vector_store = EventVisitorVectorStore(vector_db_path, collection)
        if reset_vector_store:
            self.vector_store.reset()

    def start(self, session_id=None, session_name=None, event_start=None, event_end=None, event_id=None, frame_queue=None, camera_pipeline=None):
        with self._lock:
            if self.running:
                return
            self._load_settings()
            if self.camera_source is None:
                raise ValueError("Camera source not set")

            is_resume = False
            if session_id:
                session = self.store.get_session(session_id)
                if session:
                    self.session_id = session.id
                    if session.camera_source:
                        self.camera_source = int(session.camera_source) if str(session.camera_source).isdigit() else session.camera_source
                    self.store.update_session_status(self.session_id, "active")
                    is_resume = True
            
            if not is_resume:
                session_name = session_name or f"Event {datetime.now().strftime('%Y-%m-%d %H:%M')}"
                session = self.store.create_session(session_name, str(self.camera_source), session_timezone=self.timezone_name(), event_start=event_start, event_end=event_end, event_id=event_id)
                self.session_id = session.id

            self._initialize_face_pipeline(reset_vector_store=not is_resume)
            self.active_tracks = {}
            self.next_track_id = 1
            self._frame_queue = frame_queue
            self._camera_pipeline = camera_pipeline
            self.running = True

            self.db_thread = threading.Thread(target=self._db_worker, daemon=True)
            self.db_thread.start()
            
            if not is_resume:
                self.stats = {
                    "in_count": 0,
                    "out_count": 0,
                    "total_count": 0,
                    "unique_visitor_count": 0,
                    "male_count": 0,
                    "female_count": 0,
                    "unknown_gender_count": 0,
                    "body_gender_count": 0,
                    "duplicate_face_count": 0,
                    "faces_detected": 0,
                    "active_track_count": 0,
                    "system_started": True,
                    "running": True,
                    "last_visitor_at": None,
                }
            else:
                loaded_stats = self.store.get_session_stats(self.session_id)
                for k, v in loaded_stats.items():
                    self.stats[k] = v
                self.stats["system_started"] = True
                self.stats["running"] = True

            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def stop(self, complete=False):
        with self._lock:
            self.running = False
            self.stats["running"] = False
            self.stats["system_started"] = False
            if self.session_id:
                status = "completed" if complete else "paused"
                self.store.update_session_status(self.session_id, status)
            db_thread = self.db_thread

        if db_thread and db_thread.is_alive():
            db_thread.join(timeout=5)

    def get_status(self):
        with self._lock:
            self._load_settings()
            status = dict(self.stats)
            status["company_id"] = self.company_id
            status["event_id"] = self.event_id
            status["timezone"] = self.timezone_name()
            status["camera_source"] = self.camera_source
            status["line_position"] = getattr(self, "line_position", 0.5)
            status["line_orientation"] = getattr(self, "line_orientation", "horizontal")
            status["reverse_direction"] = getattr(self, "reverse_direction", False)
            status["model_tier"] = getattr(self, "model_tier", "m")
            status["recognition_mode"] = "face_vector_line_crossing"
            status["session_id"] = self.session_id
            if self.running:
                status["status"] = "running"
            elif self.event_id:
                latest_session = self.store.latest_session_for_event(self.event_id)
                latest_status = latest_session.status if latest_session else None
                if latest_status == "completed":
                    status["status"] = "completed"
                elif latest_status == "paused":
                    status["status"] = "paused"
                elif latest_status:
                    # The persistence model uses ``active`` for a created
                    # session, but the worker is no longer running here.
                    status["status"] = "stopped"
                else:
                    status["status"] = "not_started"
            else:
                status["status"] = "not_started"
            status["similarity_threshold"] = getattr(self, "similarity_threshold", None)
            status["visitor_collection"] = (
                self.vector_store.collection_name if self.vector_store is not None else None
            )
            status["gender_classifier_enabled"] = (
                self.gender_classifier.enabled if self.gender_classifier is not None else False
            )
            status["body_gender_fallback_enabled"] = False
            status["person_detector_enabled"] = (
                self.person_detector.enabled if self.person_detector is not None else False
            )
            status["track_iou_threshold"] = getattr(self, "track_iou_threshold", None)
            status["track_max_missed_frames"] = getattr(self, "track_max_missed_frames", None)
            return status

    def _crop_box(self, frame, box, margin_ratio=0.0):
        x1, y1, x2, y2 = box
        frame_height, frame_width = frame.shape[:2]
        
        if margin_ratio > 0:
            w = x2 - x1
            h = y2 - y1
            x1 = int(x1 - (w * margin_ratio))
            y1 = int(y1 - (h * margin_ratio))
            x2 = int(x2 + (w * margin_ratio))
            y2 = int(y2 + (h * margin_ratio))

        x1 = max(0, x1)
        y1 = max(0, y1)
        x2 = min(frame_width, x2)
        y2 = min(frame_height, y2)
        if x2 <= x1 or y2 <= y1:
            return None
        return frame[y1:y2, x1:x2]

    def _increment_gender_count(self, gender):
        if gender == "male":
            self.stats["male_count"] += 1
        elif gender == "female":
            self.stats["female_count"] += 1
        else:
            self.stats["unknown_gender_count"] += 1

    def _line_position_pixels(self, frame):
        frame_height, frame_width = frame.shape[:2]
        line_position = getattr(self, "line_position", 0.5)
        orientation = getattr(self, "line_orientation", "horizontal")
        if orientation == "horizontal":
            return int(frame_height * line_position)
        return int(frame_width * line_position)

    def _box_center_value(self, box):
        x1, y1, x2, y2 = box
        orientation = getattr(self, "line_orientation", "horizontal")
        if orientation == "horizontal":
            return int((y1 + y2) / 2)
        return int((x1 + x2) / 2)

    def _crossing_direction(self, previous_value, current_value, line_pos):
        if previous_value is None:
            return None

        crossed_forward = previous_value < line_pos <= current_value
        crossed_backward = previous_value > line_pos >= current_value
        is_reversed = getattr(self, "reverse_direction", False)

        if crossed_forward:
            return "out" if is_reversed else "in"
        if crossed_backward:
            return "in" if is_reversed else "out"
        return None

    def _match_track(self, box, used_track_ids):
        best_track_id = None
        best_score = 0.0
        
        cx1 = (box[0] + box[2]) / 2
        cy1 = (box[1] + box[3]) / 2
        box_width = box[2] - box[0]
        max_dist = box_width * 1.5

        for track_id, track in self.active_tracks.items():
            if track_id in used_track_ids:
                continue
            
            t_box = track["box"]
            iou = cctv_ml._box_iou(t_box, box)
            
            cx2 = (t_box[0] + t_box[2]) / 2
            cy2 = (t_box[1] + t_box[3]) / 2
            dist = math.hypot(cx1 - cx2, cy1 - cy2)
            
            if iou > 0:
                score = 1.0 + iou
            elif dist < max_dist:
                score = 1.0 - (dist / max_dist)
            else:
                score = 0.0
                
            if score > best_score:
                best_score = score
                best_track_id = track_id

        if best_track_id is not None and (best_score >= 1.0 + self.track_iou_threshold or best_score > 0.3):
            return best_track_id
        return None

    def _visitor_match(self, embedding):
        match = self.vector_store.query_visitor(embedding)
        if match and match["similarity"] >= self.similarity_threshold:
            return match
        return None

    def _create_visitor(
        self,
        frame,
        box,
        embedding,
        now,
        pre_gender="unknown",
        pre_gender_source="unknown",
        pre_gender_confidence=0.0,
    ):
        visitor_id = hashlib.md5(f"{self.session_id}:{uuid.uuid4()}".encode()).hexdigest()
        
        if pre_gender == "unknown":
            face_crop = self._crop_box(frame, box, margin_ratio=0.30)
            gender, gender_confidence = self.gender_classifier.classify(face_crop)
            gender_source = "face" if gender != "unknown" else "unknown"
        else:
            gender = pre_gender
            gender_confidence = pre_gender_confidence or 1.0
            gender_source = pre_gender_source

        with self._lock:
            visitor_label = f"Visitor {self.stats['unique_visitor_count'] + 1}"
            self._record_event(visitor_id, visitor_label, gender, "in", now)
            self.stats["unique_visitor_count"] += 1
            self.stats["in_count"] += 1
            self.stats["total_count"] += 1
            self.stats["last_visitor_at"] = now.isoformat()
            self._increment_gender_count(gender)
            visitor_number = self.stats["unique_visitor_count"]

        visitor_label = f"Visitor {visitor_number}"
        metadata = {
            "visitor_label": visitor_label,
            "session_id": str(self.session_id) if self.session_id else None,
            "camera_source": str(self.camera_source),
            "first_seen_at": now.isoformat(),
            "last_seen_at": now.isoformat(),
            "seen_count": 1,
            "gender": gender,
            "gender_confidence": gender_confidence,
            "gender_source": gender_source,
        }
        if embedding is not None:
            self.vector_store.upsert_visitor(embedding, visitor_id, metadata)
        return visitor_id, visitor_label, gender

    def _detect_persons(self, frame):
        if self.person_detector is None or not self.person_detector.enabled:
            return []
        return self.person_detector.detect(frame)

    def _recognize_body_visitors(self, frame):
        person_boxes = self._detect_persons(frame)
        if not person_boxes:
            return []

        now = datetime.now(timezone.utc)
        line_pos = self._line_position_pixels(frame)
        detections = []
        used_track_ids = set()

        for person_box in person_boxes:
            box = person_box[:4]
            current_value = self._box_center_value(box)
            track_id = self._match_track(box, used_track_ids)

            if track_id is None:
                track_id = self.next_track_id
                self.next_track_id += 1
                self.active_tracks[track_id] = {
                    "box": box,
                    "previous_value": None,
                    "last_value": current_value,
                    "visitor_id": None,
                    "visitor_label": f"Track {track_id}",
                    "gender": "unknown",
                    "gender_source": "unknown",
                    "gender_confidence": 0.0,
                    "counted_in": False,
                    "counted_out": False,
                    "missed": 0,
                }
            else:
                track = self.active_tracks[track_id]
                track["previous_value"] = track["last_value"]
                track["last_value"] = current_value
                track["box"] = box
                track["missed"] = 0

            used_track_ids.add(track_id)
            track = self.active_tracks[track_id]
            direction = self._crossing_direction(track["previous_value"], track["last_value"], line_pos)
            counted_now = False
            if direction == "in":
                if track["visitor_id"] is None:
                    visitor_id, visitor_label, gender = self._create_visitor(
                        frame,
                        box,
                        None,
                        now,
                        pre_gender=track["gender"],
                        pre_gender_source=track.get("gender_source", "unknown"),
                        pre_gender_confidence=track.get("gender_confidence", 0.0),
                    )
                    track["visitor_id"] = visitor_id
                    track["visitor_label"] = visitor_label
                    track["gender"] = gender
                    track["counted_in"] = True
                    track["counted_out"] = False
                    counted_now = True
                elif not track["counted_in"]:
                    track["counted_in"] = True
                    track["counted_out"] = False
            elif direction == "out" and track["counted_in"] and not track["counted_out"]:
                self._record_event(track["visitor_id"], track["visitor_label"], track["gender"], "out", now)
                track["counted_out"] = True
                with self._lock:
                    self.stats["out_count"] += 1
                    self.stats["total_count"] = max(0, self.stats["total_count"] - 1)
                counted_now = True

            label = f"{track['visitor_label']} {track['gender']} body"
            if direction:
                label = f"{label} {direction.upper()}"
            detections.append(
                {
                    "box": box,
                    "label": label,
                    "color": (255, 180, 0) if not counted_now else (0, 255, 0),
                    "matched": False,
                }
            )

        self._age_tracks(used_track_ids)
        with self._lock:
            self.stats["active_track_count"] = len(self.active_tracks)
        return detections

    def _detect_faces(self, frame):
        prepared_faces, coordinates = self.face_detector.detect_prepared_faces(
            frame,
            self.detect_width,
        )
        if not prepared_faces:
            return []

        embeddings = self.embedder.embeddings(cctv_ml.np.array(prepared_faces))
        return [
            {
                "box": coordinates_item,
                "embedding": embedding,
            }
            for embedding, coordinates_item in zip(embeddings, coordinates)
        ]

    def _recognize_visitors(self, frame):
        raw_detections = self._detect_faces(frame)
        if not raw_detections:
            body_detections = self._recognize_body_visitors(frame)
            if body_detections:
                return body_detections
            self._age_tracks()
            return []

        now = datetime.now(timezone.utc)
        line_pos = self._line_position_pixels(frame)
        detections = []
        used_track_ids = set()
        for raw_detection in raw_detections:
            box = raw_detection["box"]
            embedding = raw_detection["embedding"]
            current_value = self._box_center_value(box)
            
            # Quality filter for DB and Gender
            width = box[2] - box[0]
            height = box[3] - box[1]
            is_good_quality = (width >= 40 and height >= 40)
            if is_good_quality:
                face_img = frame[int(box[1]):int(box[3]), int(box[0]):int(box[2])]
                if face_img.size > 0:
                    gray = cv2.cvtColor(face_img, cv2.COLOR_BGR2GRAY)
                    if cv2.Laplacian(gray, cv2.CV_64F).var() < 50.0:
                        is_good_quality = False
                        
            track_id = self._match_track(box, used_track_ids)
            match = self._visitor_match(embedding) if is_good_quality else None

            if track_id is None:
                track_id = self.next_track_id
                self.next_track_id += 1
                self.active_tracks[track_id] = {
                    "box": box,
                    "previous_value": None,
                    "last_value": current_value,
                    "visitor_id": None,
                    "visitor_label": f"Track {track_id}",
                    "gender": "unknown",
                    "gender_source": "unknown",
                    "gender_confidence": 0.0,
                    "counted_in": False,
                    "counted_out": False,
                    "missed": 0,
                }
            else:
                track = self.active_tracks[track_id]
                track["previous_value"] = track["last_value"]
                track["last_value"] = current_value
                track["box"] = box
                track["missed"] = 0

            used_track_ids.add(track_id)
            track = self.active_tracks[track_id]
            
            # Continuous gender detection
            if is_good_quality and track["gender"] == "unknown":
                face_crop = self._crop_box(frame, box, margin_ratio=0.30)
                if face_crop is not None:
                    gen, conf = self.gender_classifier.classify(face_crop)
                    if gen != "unknown":
                        track["gender"] = gen
                        track["gender_source"] = "face"
                        track["gender_confidence"] = conf

            if match:
                metadata = self.vector_store.update_seen(match["id"], match["metadata"])
                track["visitor_id"] = match["id"]
                track["visitor_label"] = metadata.get("visitor_label") or match["id"][:8]
                track["gender"] = metadata.get("gender", "unknown")
                track["gender_source"] = metadata.get("gender_source", "unknown")
                track["gender_confidence"] = metadata.get("gender_confidence", 0.0)
                track["counted_in"] = True
                with self._lock:
                    self.stats["duplicate_face_count"] += 1

            direction = self._crossing_direction(
                track["previous_value"],
                track["last_value"],
                line_pos,
            )
            counted_now = False

            if direction == "in":
                if track["visitor_id"] is None:
                    visitor_id, visitor_label, gender = self._create_visitor(
                        frame,
                        box,
                        embedding,
                        now,
                        pre_gender=track["gender"],
                        pre_gender_source=track.get("gender_source", "unknown"),
                        pre_gender_confidence=track.get("gender_confidence", 0.0),
                    )
                    track["visitor_id"] = visitor_id
                    track["visitor_label"] = visitor_label
                    track["gender"] = gender
                    track["counted_in"] = True
                    track["counted_out"] = False
                    counted_now = True
                elif not track["counted_in"]:
                    track["counted_in"] = True
                    track["counted_out"] = False
            elif direction == "out" and track["counted_in"] and not track["counted_out"]:
                self._record_event(
                    track["visitor_id"], track["visitor_label"], track["gender"], "out", now
                )
                track["counted_out"] = True
                with self._lock:
                    self.stats["out_count"] += 1
                    self.stats["total_count"] = max(0, self.stats["total_count"] - 1)
                counted_now = True

            label = f"{track['visitor_label']} {track['gender']}"
            if direction:
                label = f"{label} {direction.upper()}"
            color = (0, 255, 0) if counted_now else (0, 255, 255)

            with self._lock:
                self.stats["faces_detected"] += 1

            detections.append(
                {
                    "box": box,
                    "label": label,
                    "color": color,
                    "matched": bool(match),
                }
            )

        self._age_tracks(used_track_ids)
        with self._lock:
            self.stats["active_track_count"] = len(self.active_tracks)
        return detections

    def _age_tracks(self, seen_track_ids=None):
        seen_track_ids = seen_track_ids or set()
        stale_track_ids = []
        for track_id, track in self.active_tracks.items():
            if track_id in seen_track_ids:
                continue
            track["missed"] += 1
            if track["missed"] > self.track_max_missed_frames:
                stale_track_ids.append(track_id)

        for track_id in stale_track_ids:
            self.active_tracks.pop(track_id, None)

        with self._lock:
            self.stats["active_track_count"] = len(self.active_tracks)

    def _draw_counting_line(self, frame):
        frame_height, frame_width = frame.shape[:2]
        line_pos = self._line_position_pixels(frame)
        orientation = getattr(self, "line_orientation", "horizontal")
        is_reversed = getattr(self, "reverse_direction", False)
        line_color = (0, 255, 255)
        shadow_color = (0, 0, 0)

        if orientation == "horizontal":
            cv2.line(frame, (0, line_pos), (frame_width, line_pos), shadow_color, 8)
            cv2.line(frame, (0, line_pos), (frame_width, line_pos), line_color, 4)
            cv2.putText(
                frame,
                "COUNTING LINE",
                (12, max(26, line_pos - 12)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.75,
                shadow_color,
                5,
            )
            cv2.putText(
                frame,
                "COUNTING LINE",
                (12, max(26, line_pos - 12)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.75,
                line_color,
                2,
            )

            top_label = "OUT" if is_reversed else "IN"
            bottom_label = "IN" if is_reversed else "OUT"
            cv2.putText(frame, top_label, (frame_width - 76, max(28, line_pos - 16)), cv2.FONT_HERSHEY_SIMPLEX, 0.75, shadow_color, 5)
            cv2.putText(frame, top_label, (frame_width - 76, max(28, line_pos - 16)), cv2.FONT_HERSHEY_SIMPLEX, 0.75, line_color, 2)
            cv2.putText(frame, bottom_label, (frame_width - 76, min(frame_height - 12, line_pos + 34)), cv2.FONT_HERSHEY_SIMPLEX, 0.75, shadow_color, 5)
            cv2.putText(frame, bottom_label, (frame_width - 76, min(frame_height - 12, line_pos + 34)), cv2.FONT_HERSHEY_SIMPLEX, 0.75, line_color, 2)
            return

        cv2.line(frame, (line_pos, 0), (line_pos, frame_height), shadow_color, 8)
        cv2.line(frame, (line_pos, 0), (line_pos, frame_height), line_color, 4)
        label_x = max(12, min(frame_width - 210, line_pos + 12))
        cv2.putText(frame, "COUNTING LINE", (label_x, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.75, shadow_color, 5)
        cv2.putText(frame, "COUNTING LINE", (label_x, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.75, line_color, 2)

        left_label = "OUT" if is_reversed else "IN"
        right_label = "IN" if is_reversed else "OUT"
        cv2.putText(frame, left_label, (max(12, line_pos - 82), frame_height - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.75, shadow_color, 5)
        cv2.putText(frame, left_label, (max(12, line_pos - 82), frame_height - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.75, line_color, 2)
        cv2.putText(frame, right_label, (min(frame_width - 86, line_pos + 16), frame_height - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.75, shadow_color, 5)
        cv2.putText(frame, right_label, (min(frame_width - 86, line_pos + 16), frame_height - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.75, line_color, 2)

    def _draw_overlay(self, frame, detections):
        annotated_frame = frame.copy()
        self._draw_counting_line(annotated_frame)

        for detection in detections:
            x1, y1, x2, y2 = detection["box"]
            color = detection["color"]
            cv2.rectangle(annotated_frame, (x1, y1), (x2, y2), color, 2)
            cv2.putText(
                annotated_frame,
                detection["label"],
                (x1, max(20, y1 - 10)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                color,
                2,
            )

        with self._lock:
            unique_count = self.stats["unique_visitor_count"]
            duplicate_count = self.stats["duplicate_face_count"]
            male_count = self.stats["male_count"]
            female_count = self.stats["female_count"]
            unknown_gender_count = self.stats["unknown_gender_count"]

        cv2.putText(
            annotated_frame,
            f"Unique visitors: {unique_count}",
            (12, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 255, 0),
            2,
        )
        cv2.putText(
            annotated_frame,
            f"Male: {male_count}  Female: {female_count}  Unknown: {unknown_gender_count}",
            (12, 62),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (255, 255, 255),
            2,
        )
        cv2.putText(
            annotated_frame,
            f"Duplicates: {duplicate_count}",
            (12, 94),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (0, 255, 255),
            2,
        )
        return annotated_frame

    def _run(self):
        cap = None if self._frame_queue is not None else cv2.VideoCapture(self.camera_source)
        if cap is not None and not _is_video_file_source(self.camera_source):
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
        if cap is not None and not cap.isOpened():
            self.stop()
            return

        is_video_file = _is_video_file_source(self.camera_source)
        source_fps = cap.get(cv2.CAP_PROP_FPS) if cap is not None and is_video_file else 0
        if not source_fps or source_fps <= 0 or source_fps > 120:
            source_fps = 12
        # Preserve the source video's playback cadence. CPU-bound inference
        # may still reduce the effective rate when it cannot keep up.
        playback_fps = source_fps if source_fps and source_fps > 0 else 12
        frame_delay = 1 / playback_fps
        frame_stride = 1
        next_frame_at = time.monotonic()

        frame_index = 0
        detections = []
        inference_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="event-visitor-ai")
        inference_future = None

        try:
            while self.running:
                loop_started_at = time.monotonic()
                if self._frame_queue is not None:
                    try:
                        frame = self._frame_queue.get(timeout=1.0)
                    except queue.Empty:
                        continue
                    ret = True
                else:
                    ret, frame = cap.read()
                if not ret:
                    if is_video_file and cap is not None:
                        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        frame_index = 0
                        detections = []
                    else:
                        time.sleep(0.05)
                    continue
                frame = _resize_to_full_hd(frame)

                # Keep only one AI job in flight. Preview encoding continues
                # while the CPU-bound detector processes the previous frame.
                if inference_future is not None and inference_future.done():
                    try:
                        detections = inference_future.result()
                    except Exception:
                        logging.getLogger(__name__).exception("Event Visitor processing failed")
                        with self._lock:
                            self.stats["last_error"] = "Visitor processing or database write failed. Check server logs."
                        self.stop()
                        break
                    inference_future = None

                if frame_index % self.detect_every_n_frames == 0 and inference_future is None:
                    inference_future = inference_executor.submit(self._recognize_visitors, frame.copy())

                annotated_frame = self._draw_overlay(frame, detections)
                stream_frame = _resize_to_stream(annotated_frame)

                # Encode JPG once per frame for all stream clients.
                ret_jpg, buffer = cv2.imencode(".jpg", stream_frame, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                jpg_bytes = buffer.tobytes() if ret_jpg else None

                with self._lock:
                    self.current_frame = annotated_frame
                    if jpg_bytes:
                        self.current_jpg_bytes = jpg_bytes
                        self.frame_id += 1

                frame_index += 1

                if is_video_file:
                    next_frame_at += frame_delay
                    sleep_for = next_frame_at - time.monotonic()
                    if sleep_for > 0:
                        time.sleep(sleep_for)
                    elif sleep_for < -frame_delay * 3:
                        # Avoid carrying a growing delay after a slow inference.
                        next_frame_at = time.monotonic()
        finally:
            inference_executor.shutdown(wait=False, cancel_futures=True)
            if cap is not None:
                cap.release()

    def generate_frames(self):
        last_frame_id = -1
        while True:
            with self._lock:
                running = self.running
                frame_id = self.frame_id
                jpg_bytes = self.current_jpg_bytes

            if not running:
                break

            if jpg_bytes is None or frame_id == last_frame_id:
                time.sleep(0.02)
                continue

            last_frame_id = frame_id
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" + jpg_bytes + b"\r\n"
            )


class EventVisitorServiceManager:
    """Owns an isolated Event Visitor worker for each company/event pair."""

    def __init__(self):
        self._lock = threading.RLock()
        self._services = {}
        self._pipelines = {}
        self._subscriptions = {}
        self._scheduler_stop = threading.Event()
        self._scheduler_thread = None

    def for_event(self, company_id, event_id=None):
        if not company_id:
            raise ValueError("company_id is required for Event Visitor monitoring")
        key = (str(company_id), str(event_id) if event_id else "__default__")
        with self._lock:
            service = self._services.get(key)
            if service is None:
                service = EventVisitorService(company_id=key[0], event_id=None if key[1] == "__default__" else key[1])
                self._services[key] = service
            return service

    def for_company(self, company_id):
        return self.for_event(company_id)

    def status(self, company_id, event_id=None):
        return self.for_event(company_id, event_id).get_status()

    def is_running(self, company_id, event_id):
        key = (str(company_id), str(event_id))
        with self._lock:
            service = self._services.get(key)
            return bool(service and service.running)

    def delete_event(self, company_id, event_id):
        """Stop an event worker and remove its in-memory visitor collection."""
        key = (str(company_id), str(event_id))
        with self._lock:
            service = self._services.get(key)
        if service is None:
            return
        self.stop(str(company_id), str(event_id), complete=True)
        if service.vector_store is not None:
            try:
                service.vector_store.delete()
            except Exception:
                logging.getLogger(__name__).exception("Failed to delete visitor collection for event %s", event_id)
        with self._lock:
            self._services.pop(key, None)

    def start(self, company_id, event_id=None, **kwargs):
        service = self.for_event(company_id, event_id)
        kwargs.setdefault("event_id", event_id)
        service._load_settings()
        if event_id and not kwargs.get("session_id"):
            latest = service.store.latest_session_for_event(event_id)
            if latest and latest.status == "paused":
                kwargs["session_id"] = latest.id
        source = service.camera_source
        if source is None:
            service.start(**kwargs)
            return service.get_status()

        source_key = (str(company_id), str(source))
        with self._lock:
            pipeline = self._pipelines.get(source_key)
            if pipeline is None:
                pipeline = SharedCameraPipeline(source)
                self._pipelines[source_key] = pipeline
            subscriber = pipeline.subscribe()
            self._subscriptions[id(service)] = (source_key, pipeline, subscriber)
        try:
            service.start(frame_queue=subscriber, camera_pipeline=pipeline, **kwargs)
        except Exception:
            pipeline.unsubscribe(subscriber)
            with self._lock:
                self._subscriptions.pop(id(service), None)
            raise
        return service.get_status()

    def start_scheduler(self):
        with self._lock:
            if self._scheduler_thread and self._scheduler_thread.is_alive():
                return
            self._scheduler_stop.clear()
            self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
            self._scheduler_thread.start()

    def stop_scheduler(self):
        self._scheduler_stop.set()
        thread = self._scheduler_thread
        if thread and thread.is_alive():
            thread.join(timeout=2)

    def _scheduler_loop(self):
        while not self._scheduler_stop.is_set():
            try:
                self._schedule_events()
            except Exception:
                logging.getLogger(__name__).exception("Event scheduler failed")
            self._scheduler_stop.wait(10)

    def _schedule_events(self):
        from datetime import time as datetime_time, timedelta
        from app.models.event import Event
        from app.utils.database import SessionLocal

        with SessionLocal() as db:
            events = db.query(Event).all()

        for event in events:
            if not event.company_id or not event.event_date or not event.event_start or not event.auto_run:
                continue
            try:
                start_time = datetime_time.fromisoformat(event.event_start)
                end_time = datetime_time.fromisoformat(event.event_end) if event.event_end else None
            except ValueError:
                continue

            service = self.for_event(str(event.company_id), str(event.id))
            local_timezone = resolve_timezone(service.timezone_name())
            now = datetime.now(local_timezone)
            if now.date() != event.event_date:
                continue
            current_time = now.time()
            service._load_settings()
            latest = service.store.latest_session_for_event(event.id)
            latest_date = None
            if latest and latest.started_at:
                started_at = latest.started_at
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=timezone.utc)
                latest_date = started_at.astimezone(local_timezone).date()

            in_window = current_time >= start_time and (end_time is None or current_time < end_time)
            if in_window and not service.running and latest_date != now.date():
                try:
                    self.start(
                        str(event.company_id),
                        event_id=str(event.id),
                        session_name=event.name,
                        event_start=event.event_start,
                        event_end=event.event_end,
                    )
                except ValueError:
                    logging.getLogger(__name__).warning(
                        "Event %s is due but has no usable camera source", event.id
                    )

            if end_time is not None and current_time >= end_time and service.running:
                self.stop(str(event.company_id), str(event.id), complete=True)

    def stop(self, company_id, event_id=None, complete=False):
        service = self.for_event(company_id, event_id)
        service.stop(complete=complete)
        with self._lock:
            subscription = self._subscriptions.pop(id(service), None)
        if subscription:
            source_key, pipeline, subscriber = subscription
            pipeline.unsubscribe(subscriber)
            with self._lock:
                if not pipeline.subscribers:
                    self._pipelines.pop(source_key, None)
        return service.get_status()


event_visitor_manager = EventVisitorServiceManager()
# Kept for internal compatibility; tenant-facing routes must use the manager.
event_visitor_service = EventVisitorService()
