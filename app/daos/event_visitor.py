from datetime import datetime, time, timedelta, timezone
from uuid import UUID

from sqlalchemy import case, func

from app.models.event_visitor import EventVisitorEvent, EventVisitorSetting, EventVisitorSession
from app.utils.database import SessionLocal


class EventVisitorStore:
    setting_defaults = {
        "camera_source": None,
        "line_position": 0.5,
        "line_orientation": "horizontal",
        "reverse_direction": False,
        "model_tier": "m",
    }

    def __init__(self, session_factory=None, company_id=None, event_id=None):
        self.session_factory = session_factory or SessionLocal
        self.company_id = str(company_id) if company_id else None
        self.event_id = self._coerce_uuid(event_id) if event_id else None

    @staticmethod
    def _coerce_uuid(value):
        if value is None or isinstance(value, UUID):
            return value
        return UUID(str(value))

    def load_settings(self):
        with self.session_factory() as db:
            query = db.query(EventVisitorSetting)
            if self.company_id:
                query = query.filter(EventVisitorSetting.company_id == self.company_id)
            else:
                query = query.filter(EventVisitorSetting.id == 1)
            record = query.first()
            return {
                key: getattr(record, key) if record is not None else default
                for key, default in self.setting_defaults.items()
            }

    def save_settings(self, **changes):
        if not changes.keys() <= self.setting_defaults.keys():
            raise ValueError("Unknown Event Visitor setting")
        with self.session_factory() as db:
            query = db.query(EventVisitorSetting)
            if self.company_id:
                query = query.filter(EventVisitorSetting.company_id == self.company_id)
            else:
                query = query.filter(EventVisitorSetting.id == 1)
            record = query.first()
            if record is None:
                record = EventVisitorSetting(company_id=self.company_id, **self.setting_defaults)
                db.add(record)
            for key, value in changes.items():
                setattr(record, key, value)
            db.commit()

    def create_session(self, name, camera_source=None, session_timezone="Asia/Jakarta", event_start=None, event_end=None, event_id=None):
        with self.session_factory() as db:
            session = EventVisitorSession(
                name=name,
                company_id=self.company_id,
                camera_source=camera_source,
                timezone=session_timezone,
                event_start=event_start,
                event_end=event_end,
                event_id=event_id,
                started_at=datetime.now(timezone.utc)
            )
            db.add(session)
            db.commit()
            db.refresh(session)
            return session

    def get_session(self, session_id):
        session_id = self._coerce_uuid(session_id)
        with self.session_factory() as db:
            query = db.query(EventVisitorSession).filter(EventVisitorSession.id == session_id)
            if self.company_id:
                query = query.filter(EventVisitorSession.company_id == self.company_id)
            return query.first()

    def latest_session_for_event(self, event_id):
        event_id = self._coerce_uuid(event_id)
        with self.session_factory() as db:
            query = db.query(EventVisitorSession).filter(EventVisitorSession.event_id == event_id)
            if self.company_id:
                query = query.filter(EventVisitorSession.company_id == self.company_id)
            return query.order_by(EventVisitorSession.started_at.desc()).first()
            
    def update_session_status(self, session_id, status):
        session_id = self._coerce_uuid(session_id)
        with self.session_factory() as db:
            query = db.query(EventVisitorSession).filter(EventVisitorSession.id == session_id)
            if self.company_id:
                query = query.filter(EventVisitorSession.company_id == self.company_id)
            session = query.first()
            if session:
                session.status = status
                if status == "completed":
                    session.completed_at = datetime.now(timezone.utc)
                db.commit()
                db.refresh(session)
                return session
            return None

    def get_session_stats(self, session_id):
        session_id = self._coerce_uuid(session_id)
        with self.session_factory() as db:
            query = db.query(EventVisitorEvent).filter(EventVisitorEvent.session_id == session_id)
            if self.company_id:
                query = query.filter(EventVisitorEvent.company_id == self.company_id)
            events = query.all()
            
            unique_visitors = set(e.visitor_id for e in events)
            in_count = sum(1 for e in events if e.direction == "in")
            out_count = sum(1 for e in events if e.direction == "out")
            male_count = sum(1 for e in events if e.gender == "male" and e.direction == "in")
            female_count = sum(1 for e in events if e.gender == "female" and e.direction == "in")
            unknown_count = sum(1 for e in events if e.gender == "unknown" and e.direction == "in")
            last_event = max((e.detected_at for e in events), default=None)
            
            return {
                "in_count": in_count,
                "out_count": out_count,
                "total_count": max(0, in_count - out_count),
                "unique_visitor_count": len(unique_visitors),
                "male_count": male_count,
                "female_count": female_count,
                "unknown_gender_count": unknown_count,
                "duplicate_face_count": 0,
                "last_visitor_at": last_event.isoformat() if last_event else None
            }

    def record_event(self, **values):
        if "session_id" in values:
            values["session_id"] = self._coerce_uuid(values["session_id"])
        if self.company_id:
            values["company_id"] = self.company_id
        with self.session_factory() as db:
            db.add(EventVisitorEvent(**values))
            db.commit()

    def hourly_entries(self, selected_date, event_id=None, timezone_name="Asia/Jakarta"):
        # Compare UTC boundaries so PostgreSQL session timezone cannot shift buckets.
        from app.utils.timezone import resolve_timezone
        local_timezone = resolve_timezone(timezone_name)
        start = datetime.combine(selected_date, time.min, tzinfo=local_timezone).astimezone(timezone.utc)
        boundaries = [start + timedelta(hours=hour) for hour in range(25)]
        with self.session_factory() as db:
            query = db.query(*[
                func.count(case((
                    (EventVisitorEvent.detected_at >= boundaries[hour])
                    & (EventVisitorEvent.detected_at < boundaries[hour + 1]),
                    1,
                )))
                for hour in range(24)
            ]).filter(
                EventVisitorEvent.direction == "in",
                EventVisitorEvent.detected_at >= boundaries[0],
                EventVisitorEvent.detected_at < boundaries[24],
            )
            selected_event_id = self._coerce_uuid(event_id) if event_id else self.event_id
            if selected_event_id:
                query = query.join(EventVisitorSession, EventVisitorSession.id == EventVisitorEvent.session_id).filter(
                    EventVisitorSession.event_id == selected_event_id
                )
            if self.company_id:
                query = query.filter(EventVisitorEvent.company_id == self.company_id)
            counts = query.one()

            gender_query = db.query(
                EventVisitorEvent.gender,
                func.count(EventVisitorEvent.id),
            ).filter(
                EventVisitorEvent.direction == "in",
                EventVisitorEvent.detected_at >= boundaries[0],
                EventVisitorEvent.detected_at < boundaries[24],
            )
            if selected_event_id:
                gender_query = gender_query.join(
                    EventVisitorSession,
                    EventVisitorSession.id == EventVisitorEvent.session_id,
                ).filter(EventVisitorSession.event_id == selected_event_id)
            if self.company_id:
                gender_query = gender_query.filter(EventVisitorEvent.company_id == self.company_id)
            gender_counts = dict(gender_query.group_by(EventVisitorEvent.gender).all())
        hours = [
            {"hour": hour, "label": f"{hour:02d}:00", "in_count": int(count)}
            for hour, count in enumerate(counts)
        ]
        total = sum(item["in_count"] for item in hours)
        peak = max(hours, key=lambda item: item["in_count"]) if total else None
        return {
            "date": selected_date.isoformat(),
            "timezone": timezone_name,
            "total_in": total,
            "peak_hour": peak,
            "male_count": int(gender_counts.get("male", 0)),
            "female_count": int(gender_counts.get("female", 0)),
            "unknown_gender_count": int(gender_counts.get("unknown", 0)),
            "hours": hours,
        }

    def list_events(self, session_id=None, limit=100, offset=0):
        session_id = self._coerce_uuid(session_id)
        with self.session_factory() as db:
            query = db.query(EventVisitorEvent)
            if self.company_id:
                query = query.filter(EventVisitorEvent.company_id == self.company_id)
            if session_id is not None:
                query = query.filter(EventVisitorEvent.session_id == session_id)
            if self.event_id:
                query = query.join(EventVisitorSession, EventVisitorSession.id == EventVisitorEvent.session_id).filter(
                    EventVisitorSession.event_id == self.event_id
                )
            records = query.order_by(
                EventVisitorEvent.detected_at.desc(), EventVisitorEvent.id.desc()
            ).offset(offset).limit(limit).all()
            return [
                {
                    **{column.name: getattr(record, column.name)
                       for column in EventVisitorEvent.__table__.columns},
                    "detected_at": (
                        record.detected_at.replace(tzinfo=timezone.utc)
                        if record.detected_at.tzinfo is None else record.detected_at
                    ).isoformat(),
                }
                for record in records
            ]
