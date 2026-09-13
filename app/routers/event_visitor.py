from datetime import date
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from app.services.event_visitor import event_visitor_manager
from app.utils.response import success_response

router = APIRouter()


@router.get("/statistics/hourly")
def get_hourly_visitor_statistics(selected_date: date = Query(..., alias="date"), company_id: str = Query(..., min_length=1), event_id: str | None = Query(default=None, min_length=1)):
    if not company_id.strip():
        raise HTTPException(status_code=422, detail="company_id is required.")
    return success_response(
        message="Hourly Event Visitor entries.",
        result=event_visitor_manager.for_event(company_id, event_id).store.hourly_entries(
            selected_date,
            timezone_name=event_visitor_manager.for_event(company_id, event_id).timezone_name(),
        ),
    )

class CameraSourceRequest(BaseModel):
    camera_source: str

class LinePositionRequest(BaseModel):
    position: float

from app.models.event import Event
from app.utils.database import SessionLocal

class StartEventVisitorRequest(BaseModel):
    event_id: str | None = None
    event_name: str | None = None
    event_start: str | None = None
    event_end: str | None = None
    user_id: str | None = None
    company_id: str | None = None

@router.post("/start")
async def start_event_visitor(
    req: StartEventVisitorRequest = None,
    company_id: str | None = Query(default=None, min_length=1),
):
    try:
        event_id = req.event_id if req else None
        name = req.event_name if req else None
        start_time = req.event_start if req else None
        end_time = req.event_end if req else None
        user_id = req.user_id if req else None
        company_id = company_id or (req.company_id if req else None)
        if not company_id:
            raise ValueError("company_id is required for Event Visitor monitoring")
        
        if event_id:
            with SessionLocal() as db:
                event = db.query(Event).filter(Event.id == event_id).first()
                if not event:
                    raise ValueError("Event not found.")
                if str(event.company_id) != str(company_id):
                    raise ValueError("Event does not belong to the selected company.")
                name = event.name
                start_time = event.event_start
                end_time = event.event_end
        elif name:
            with SessionLocal() as db:
                new_event = Event(
                    name=name,
                    event_start=start_time,
                    event_end=end_time,
                    user_id=user_id,
                    company_id=company_id
                )
                db.add(new_event)
                db.commit()
                db.refresh(new_event)
                event_id = new_event.id

        return success_response(
            message="Event Visitor Counter started.",
            result=event_visitor_manager.start(company_id, event_id=event_id, session_name=name, event_start=start_time, event_end=end_time),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/stop")
async def stop_event_visitor(company_id: str = Query(..., min_length=1), event_id: str | None = Query(default=None, min_length=1)):
    return success_response(message="Event Visitor Counter stopped.", result=event_visitor_manager.stop(company_id, event_id, complete=True))

@router.post("/pause")
async def pause_event_visitor(company_id: str = Query(..., min_length=1), event_id: str | None = Query(default=None, min_length=1)):
    return success_response(message="Event Visitor Counter paused.", result=event_visitor_manager.stop(company_id, event_id, complete=False))

@router.get("/status")
async def get_event_visitor_status(company_id: str = Query(..., min_length=1), event_id: str | None = Query(default=None, min_length=1)):
    status = event_visitor_manager.status(company_id, event_id)
    return success_response(message="Event Visitor Counter status.", result=status)

@router.post("/source")
async def set_event_visitor_source(req: CameraSourceRequest, company_id: str = Query(..., min_length=1), event_id: str | None = Query(default=None, min_length=1)):
    event_visitor_manager.for_event(company_id, event_id).set_camera_source(req.camera_source)
    return success_response(message="Camera source set successfully.")

class LineOrientationRequest(BaseModel):
    orientation: Literal["horizontal", "vertical"]

class LineReverseRequest(BaseModel):
    reverse: bool

class ModelTierRequest(BaseModel):
    tier: Literal["n", "s", "m", "l", "x"]

@router.post("/line")
async def set_event_visitor_line(req: LinePositionRequest, company_id: str = Query(..., min_length=1), event_id: str | None = Query(default=None, min_length=1)):
    event_visitor_manager.for_event(company_id, event_id).set_line_position(req.position)
    return success_response(message="Line position updated.")

@router.post("/orientation")
async def set_event_visitor_orientation(req: LineOrientationRequest, company_id: str = Query(..., min_length=1), event_id: str | None = Query(default=None, min_length=1)):
    event_visitor_manager.for_event(company_id, event_id).set_line_orientation(req.orientation)
    return success_response(message="Line orientation updated.")

@router.post("/reverse")
async def set_event_visitor_reverse(req: LineReverseRequest, company_id: str = Query(..., min_length=1), event_id: str | None = Query(default=None, min_length=1)):
    event_visitor_manager.for_event(company_id, event_id).set_reverse_direction(req.reverse)
    return success_response(message="Direction reversed.")

@router.post("/tier")
async def set_event_visitor_tier(req: ModelTierRequest, company_id: str = Query(..., min_length=1), event_id: str | None = Query(default=None, min_length=1)):
    event_visitor_manager.for_event(company_id, event_id).set_model_tier(req.tier)
    return success_response(message=f"Model tier updated to {req.tier}.")

@router.get("/stream")
async def stream_event_visitor(company_id: str = Query(..., min_length=1), event_id: str | None = Query(default=None, min_length=1)):
    service = event_visitor_manager.for_event(company_id, event_id)
    if not service.running:
        raise HTTPException(status_code=409, detail="Service is not running.")
    return StreamingResponse(
        service.generate_frames(),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@router.get("/events")
def list_event_visitor_events(
    session_id: str | None = None,
    company_id: str = Query(..., min_length=1),
    event_id: str | None = Query(default=None, min_length=1),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return success_response(
        message="Event Visitor history.",
        result=event_visitor_manager.for_event(company_id, event_id).store.list_events(session_id, limit, offset),
    )
