import json
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import Mock
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.daos.event_visitor import EventVisitorStore
from app.models.event_visitor import EventVisitorEvent, EventVisitorSetting
from app.services.event_visitor import EventVisitorService


class TestEventVisitorPersistence(TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        for model in (EventVisitorSetting, EventVisitorEvent):
            model.__table__.create(self.engine)
        self.factory = sessionmaker(bind=self.engine)
        self.store = EventVisitorStore(self.factory)

    def tearDown(self):
        self.engine.dispose()

    def test_settings_survive_new_service_instance(self):
        service = EventVisitorService(self.store)
        service.set_camera_source("0")
        service.set_line_position(0.7)
        service.set_line_orientation("vertical")
        service.set_reverse_direction(True)
        service.set_model_tier("s")

        restarted = EventVisitorService(EventVisitorStore(self.factory))
        status = restarted.get_status()
        self.assertEqual(status["camera_source"], 0)
        self.assertEqual(status["line_position"], 0.7)
        self.assertEqual(status["line_orientation"], "vertical")
        self.assertTrue(status["reverse_direction"])
        self.assertEqual(status["model_tier"], "s")
        self.assertFalse(status["running"])

    def test_failed_setting_save_does_not_change_runtime(self):
        service = EventVisitorService(self.store)
        service.set_line_position(0.6)
        self.store.save_settings = Mock(side_effect=RuntimeError("DB unavailable"))
        with self.assertRaises(RuntimeError):
            service.set_line_position(0.8)
        self.assertEqual(service.line_position, 0.6)

    def test_new_visitor_and_exit_remain_in_history_across_sessions(self):
        service = EventVisitorService(self.store)
        session_one = UUID("11111111-1111-1111-1111-111111111111")
        session_two = UUID("22222222-2222-2222-2222-222222222222")
        service.session_id = session_one
        service.camera_source = "camera.mp4"
        service._crop_box = Mock(return_value=None)
        service.gender_classifier = Mock()
        service.gender_classifier.classify.return_value = ("unknown", 0.3)
        service.vector_store = Mock()
        now = datetime.now(timezone.utc)
        visitor_id, label, gender = service._create_visitor(None, None, None, now)
        service._record_event(visitor_id, label, gender, "out", now)
        restarted = EventVisitorService(EventVisitorStore(self.factory))
        events = restarted.store.list_events(session_one)
        self.assertEqual(len(events), 2)
        self.assertIn("+00:00", events[0]["detected_at"])
        json.dumps(events)
        self.assertEqual({event["direction"] for event in events}, {"in", "out"})
        self.assertEqual({event["visitor_id"] for event in events}, {visitor_id})
        self.assertEqual(restarted.store.list_events(session_two), [])
        self.assertEqual(len(restarted.store.list_events(limit=1, offset=1)), 1)

    def test_failed_visitor_save_does_not_increment_counts(self):
        service = EventVisitorService(self.store)
        service._crop_box = Mock(return_value=None)
        service.gender_classifier = Mock()
        service.gender_classifier.classify.return_value = ("unknown", 0.0)
        service.store.record_event = Mock(side_effect=RuntimeError("DB unavailable"))
        with self.assertRaises(RuntimeError):
            service._create_visitor(None, None, None, datetime.now(timezone.utc))
        self.assertEqual(service.stats["unique_visitor_count"], 0)

    def test_hourly_entries_use_wib_boundaries_and_exclude_exits(self):
        from datetime import date

        samples = [
            (datetime(2026, 9, 7, 16, 59, 59, tzinfo=timezone.utc), "in"),
            (datetime(2026, 9, 7, 17, 0, tzinfo=timezone.utc), "in"),
            (datetime(2026, 9, 8, 1, 0, tzinfo=timezone.utc), "in"),
            (datetime(2026, 9, 8, 1, 59, 59, tzinfo=timezone.utc), "in"),
            (datetime(2026, 9, 8, 2, 0, tzinfo=timezone.utc), "in"),
            (datetime(2026, 9, 8, 2, 15, tzinfo=timezone.utc), "out"),
            (datetime(2026, 9, 8, 16, 59, 59, tzinfo=timezone.utc), "in"),
            (datetime(2026, 9, 8, 17, 0, tzinfo=timezone.utc), "in"),
        ]
        with self.factory() as db:
            db.add_all([
                EventVisitorEvent(session_id=UUID(f"11111111-1111-1111-1111-{index:012d}"), visitor_id=str(index),
                                  visitor_label="Visitor", gender="unknown",
                                  direction=direction, detected_at=detected_at)
                for index, (detected_at, direction) in enumerate(samples)
            ])
            db.commit()
        stats = self.store.hourly_entries(date(2026, 9, 8))
        self.assertEqual(stats["total_in"], 5)
        self.assertEqual(len(stats["hours"]), 24)
        self.assertEqual(stats["hours"][0]["in_count"], 1)
        self.assertEqual(stats["hours"][8]["in_count"], 2)
        self.assertEqual(stats["hours"][9]["in_count"], 1)
        self.assertEqual(stats["hours"][23]["in_count"], 1)
        self.assertEqual(stats["peak_hour"]["label"], "08:00")
        json.dumps(stats)

    def test_empty_day_has_zero_for_every_hour(self):
        from datetime import date

        stats = self.store.hourly_entries(date(2026, 9, 8))
        self.assertEqual(stats["total_in"], 0)
        self.assertIsNone(stats["peak_hour"])
        self.assertEqual(len(stats["hours"]), 24)
        self.assertTrue(all(hour["in_count"] == 0 for hour in stats["hours"]))

    def test_hourly_entries_are_not_limited_to_history_page_size(self):
        from datetime import date

        with self.factory() as db:
            db.add_all([
                EventVisitorEvent(session_id=UUID("11111111-1111-1111-1111-111111111111"), visitor_id=str(index),
                                  visitor_label="Visitor", gender="unknown", direction="in",
                                  detected_at=datetime(2026, 9, 8, 1, 0, tzinfo=timezone.utc))
                for index in range(550)
            ])
            db.commit()
        stats = self.store.hourly_entries(date(2026, 9, 8))
        self.assertEqual(stats["total_in"], 550)
        self.assertEqual(stats["hours"][8]["in_count"], 550)
