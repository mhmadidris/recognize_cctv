from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone
from html import escape
from collections import Counter
from sqlalchemy import func
from sqlalchemy.orm import Session
from app.models.event import Event
from app.models.company import Company
from app.models.event_visitor import EventVisitorEvent, EventVisitorSession
from app.utils.database import SessionLocal
from app.utils.response import success_response
from app.daos.cctv import CCTVCompanySettingDAO
from app.utils.timezone import DEFAULT_TIMEZONE, resolve_timezone
from app.services.event_visitor import event_visitor_manager
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

router = APIRouter(prefix="/events", tags=["events"])

class EventCreateRequest(BaseModel):
    name: str
    event_start: Optional[str] = None
    event_end: Optional[str] = None
    user_id: Optional[str] = None
    company_id: Optional[str] = None

class EventResponse(BaseModel):
    id: str
    name: str
    event_start: Optional[str]
    event_end: Optional[str]
    user_id: Optional[str]
    company_id: Optional[str]
    created_at: Optional[datetime]

    class Config:
        from_attributes = True

@router.post("", response_model=dict)
def create_event(req: EventCreateRequest):
    with SessionLocal() as db:
        new_event = Event(
            name=req.name,
            event_start=req.event_start,
            event_end=req.event_end,
            user_id=req.user_id,
            company_id=req.company_id
        )
        db.add(new_event)
        db.commit()
        db.refresh(new_event)
        return success_response(result={"id": str(new_event.id), "name": new_event.name}, message="Event created successfully")

@router.get("", response_model=dict)
def list_events(company_id: Optional[str] = None):
    with SessionLocal() as db:
        query = db.query(Event).order_by(Event.created_at.desc())
        if company_id:
            query = query.filter(Event.company_id == company_id)
        events = query.all()

        visitor_counts = {}
        if events:
            persisted_counts = dict(
                db.query(
                    EventVisitorSession.event_id,
                    func.count(func.distinct(EventVisitorEvent.visitor_id)),
                )
                .join(EventVisitorEvent, EventVisitorEvent.session_id == EventVisitorSession.id)
                .filter(
                    EventVisitorSession.event_id.in_([event.id for event in events]),
                    EventVisitorEvent.direction == "in",
                )
                .group_by(EventVisitorSession.event_id)
                .all()
            )
            # Normalize UUID keys so the lookup is stable across PostgreSQL
            # driver/configuration combinations.
            visitor_counts = {str(event_id): int(count) for event_id, count in persisted_counts.items()}
        
        data = []
        for e in events:
            latest_session = db.query(EventVisitorSession).filter(
                EventVisitorSession.event_id == e.id
            ).order_by(EventVisitorSession.started_at.desc()).first()
            if event_visitor_manager.is_running(e.company_id, e.id):
                session_status = "running"
            elif latest_session and latest_session.status == "completed":
                session_status = "completed"
            elif latest_session and latest_session.status == "paused":
                session_status = "paused"
            elif latest_session:
                session_status = "stopped"
            else:
                session_status = "not_started"
            visitor_count = visitor_counts.get(str(e.id), 0)
            if event_visitor_manager.is_running(e.company_id, e.id):
                try:
                    live_status = event_visitor_manager.status(str(e.company_id), str(e.id))
                    visitor_count = max(visitor_count, int(live_status.get("unique_visitor_count", 0)))
                except Exception:
                    # A live worker must not make the event list unavailable.
                    pass

            data.append({
                "id": str(e.id),
                "name": e.name,
                "event_start": e.event_start,
                "event_end": e.event_end,
                "company_id": str(e.company_id) if e.company_id else None,
                "created_at": e.created_at.isoformat() if e.created_at else None,
                "status": session_status,
                "session_id": str(latest_session.id) if latest_session else None,
                "visitor_count": visitor_count,
            })
        return success_response(message="Success", result=data)


@router.put("/{event_id}", response_model=dict)
def update_event(event_id: str, req: EventCreateRequest, company_id: str | None = Query(default=None)):
    with SessionLocal() as db:
        query = db.query(Event).filter(Event.id == event_id)
        effective_company_id = company_id or req.company_id
        if effective_company_id:
            query = query.filter(Event.company_id == effective_company_id)
        event = query.first()
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")

        event.name = req.name.strip()
        event.event_start = req.event_start
        event.event_end = req.event_end
        db.commit()
        db.refresh(event)
        return success_response(
            message="Event updated successfully",
            result={
                "id": str(event.id),
                "name": event.name,
                "event_start": event.event_start,
                "event_end": event.event_end,
            },
        )

@router.delete("/{event_id}", response_model=dict)
def delete_event(event_id: str, company_id: str | None = Query(default=None)):
    with SessionLocal() as db:
        query = db.query(Event).filter(Event.id == event_id)
        if company_id:
            query = query.filter(Event.company_id == company_id)
        event = query.first()
        if not event:
            raise HTTPException(status_code=404, detail="Event not found")

        event_visitor_manager.delete_event(str(event.company_id), str(event.id))
        sessions = db.query(EventVisitorSession).filter(EventVisitorSession.event_id == event.id).all()
        session_ids = [session.id for session in sessions]
        if session_ids:
            db.query(EventVisitorEvent).filter(EventVisitorEvent.session_id.in_(session_ids)).delete(
                synchronize_session=False
            )
        for session in sessions:
            db.delete(session)
        db.delete(event)
        db.commit()
        return success_response(
            message="Event and all visitor monitoring data deleted successfully",
            result={"event_id": str(event_id), "deleted_sessions": len(sessions)},
        )


@router.get("/{event_id}/report", response_class=Response)
def download_event_report(event_id: str, company_id: str | None = Query(default=None)):
    """Return a print-ready report for one event."""
    try:
        with SessionLocal() as db:
            query = db.query(Event).filter(Event.id == event_id)
            if company_id:
                query = query.filter(Event.company_id == company_id)
            event = query.first()
            if not event:
                raise HTTPException(status_code=404, detail="Event not found")
            company = db.query(Company).filter(Company.id == event.company_id).first() if event.company_id else None

            sessions = db.query(EventVisitorSession).filter(
                EventVisitorSession.event_id == event.id
            ).order_by(EventVisitorSession.started_at.asc()).all()
            session_ids = [session.id for session in sessions]
            records = []
            if session_ids:
                records = db.query(EventVisitorEvent).filter(
                    EventVisitorEvent.session_id.in_(session_ids)
                ).order_by(EventVisitorEvent.detected_at.asc()).all()
    except HTTPException:
        raise
    except (ValueError, TypeError):
        raise HTTPException(status_code=404, detail="Event not found")

    with SessionLocal() as db:
        setting = CCTVCompanySettingDAO.get_by_company_id(db, event.company_id) if event.company_id else None
    timezone_name = getattr(setting, "timezone", None) or DEFAULT_TIMEZONE
    wib = resolve_timezone(timezone_name)
    in_records = [record for record in records if record.direction == "in"]
    out_records = [record for record in records if record.direction == "out"]
    gender_counts = Counter(record.gender for record in in_records)
    unique_visitors = len({record.visitor_id for record in in_records})
    hourly = Counter(
        record.detected_at.replace(tzinfo=timezone.utc).astimezone(wib).hour
        if record.detected_at.tzinfo is None
        else record.detected_at.astimezone(wib).hour
        for record in in_records
    )
    peak_hour = max(hourly, key=hourly.get) if hourly else None
    generated_at = datetime.now(wib).strftime("%d %B %Y, %H:%M")
    event_name = escape(event.name)
    company_name = escape(company.name if company else "-")
    filename = "".join(character if character.isalnum() else "-" for character in event.name).strip("-") or "event"

    session_rows = "".join(
        f"<tr><td>{index}</td><td>{escape(session.name or '-')}</td>"
        f"<td>{session.started_at.astimezone(wib).strftime('%d %b %Y %H:%M') if session.started_at else '-'}</td>"
        f"<td>{session.completed_at.astimezone(wib).strftime('%d %b %Y %H:%M') if session.completed_at else '-'}</td>"
        f"<td><span class='badge'>{escape(session.status or '-')}</span></td></tr>"
        for index, session in enumerate(sessions, start=1)
    ) or "<tr><td colspan='5' class='empty'>Belum ada sesi monitoring.</td></tr>"
    activity_rows = "".join(
        f"<tr><td>{record.detected_at.astimezone(wib).strftime('%d %b %Y %H:%M:%S') if record.detected_at.tzinfo else record.detected_at.replace(tzinfo=timezone.utc).astimezone(wib).strftime('%d %b %Y %H:%M:%S')}</td>"
        f"<td>{escape(record.visitor_label)}</td><td>{escape(record.gender.title())}</td>"
        f"<td><span class='direction {record.direction}'>{'MASUK' if record.direction == 'in' else 'KELUAR'}</span></td></tr>"
        for record in records
    ) or "<tr><td colspan='4' class='empty'>Belum ada aktivitas tercatat.</td></tr>"
    hourly_rows = "".join(
        f"<tr><td>{hour:02d}:00 - {hour:02d}:59</td><td>{hourly.get(hour, 0):,}</td></tr>".replace(",", ".")
        for hour in range(24)
    )

    activity_table = [["Waktu", "Visitor", "Gender", "Arah"]]
    activity_table.extend(
        (
            record.detected_at.astimezone(wib).strftime("%d %b %Y %H:%M:%S")
            if record.detected_at.tzinfo
            else record.detected_at.replace(tzinfo=timezone.utc).astimezone(wib).strftime("%d %b %Y %H:%M:%S"),
            record.visitor_label or "-",
            (record.gender or "unknown").title(),
            "MASUK" if record.direction == "in" else "KELUAR",
        )
        for record in records
    )
    if len(activity_table) == 1:
        activity_table.append(["-", "Belum ada aktivitas tercatat", "-", "-"])

    # Generate a native PDF so downloads do not depend on the browser's HTML handling.
    pdf_buffer = BytesIO()
    document = SimpleDocTemplate(
        pdf_buffer,
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=f"Laporan Event - {event.name}",
    )
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ReportTitle", parent=styles["Title"], fontSize=22, textColor=colors.HexColor("#111827"), spaceAfter=5))
    styles.add(ParagraphStyle(name="Muted", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#64748b")))
    styles.add(ParagraphStyle(name="Section", parent=styles["Heading2"], fontSize=13, textColor=colors.HexColor("#111827"), spaceBefore=12, spaceAfter=7))
    styles.add(ParagraphStyle(name="Small", parent=styles["Normal"], fontSize=8, leading=10))
    story = [
        Paragraph("VISION ADMIN", styles["Muted"]),
        Paragraph(event_name, styles["ReportTitle"]),
        Paragraph("Laporan hasil monitoring pengunjung per event", styles["Muted"]),
        Spacer(1, 7 * mm),
        Table([
            [Paragraph("Nama Event", styles["Small"]), Paragraph(event_name, styles["Small"]), Paragraph("Nama Perusahaan", styles["Small"]), Paragraph(company_name, styles["Small"])],
            [Paragraph("Jadwal", styles["Small"]), Paragraph(f"{escape(event.event_start or '-')} - {escape(event.event_end or '-')}", styles["Small"]), Paragraph("Zona Waktu", styles["Small"]), Paragraph(escape(timezone_name), styles["Small"])],
        ], colWidths=[25 * mm, 65 * mm, 32 * mm, 55 * mm], style=[("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")), ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")), ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")), ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]),
        Paragraph("Ringkasan Pengunjung", styles["Section"]),
        Table([["Total masuk", "Total keluar", "Visitor unik", "Total sesi"], [f"{len(in_records):,}", f"{len(out_records):,}", f"{unique_visitors:,}", f"{len(sessions):,}"]], colWidths=[43 * mm] * 4, style=[("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4f46e5")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#f8fafc")), ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")), ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")), ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 9), ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]),
        Paragraph("Distribusi Gender", styles["Section"]),
        Table(
            [
                ["Laki-laki", "Perempuan", "Belum teridentifikasi", "Jam tersibuk"],
                [
                    str(gender_counts.get("male", 0)),
                    str(gender_counts.get("female", 0)),
                    str(gender_counts.get("unknown", 0)),
                    f"{peak_hour:02d}:00" if peak_hour is not None else "-",
                ],
            ],
            colWidths=[43 * mm] * 4,
            style=[("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e0e7ff")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#312e81")), ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#f8fafc")), ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")), ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")), ("ALIGN", (0, 0), (-1, -1), "CENTER"), ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"), ("FONTSIZE", (0, 0), (-1, -1), 8), ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)],
        ),
        Paragraph("Distribusi Pengunjung per Jam", styles["Section"]),
        Table([["Jam", f"Masuk ({timezone_name})"]] + [[f"{hour:02d}:00 - {hour:02d}:59", str(hourly.get(hour, 0))] for hour in range(24)], colWidths=[70 * mm, 100 * mm], repeatRows=1, style=[("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e0e7ff")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#312e81")), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")), ("FONTSIZE", (0, 0), (-1, -1), 8), ("ALIGN", (1, 1), (1, -1), "RIGHT")]),
        Paragraph("Sesi Monitoring", styles["Section"]),
        Table([["No", "Nama sesi", "Mulai", "Selesai", "Status"]] + [[str(index), session.name or "-", session.started_at.astimezone(wib).strftime("%d %b %Y %H:%M") if session.started_at else "-", session.completed_at.astimezone(wib).strftime("%d %b %Y %H:%M") if session.completed_at else "-", session.status or "-"] for index, session in enumerate(sessions, start=1)] or [["-", "Belum ada sesi monitoring", "-", "-", "-"]], colWidths=[10 * mm, 55 * mm, 35 * mm, 35 * mm, 25 * mm], repeatRows=1, style=[("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e0e7ff")), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")), ("FONTSIZE", (0, 0), (-1, -1), 7)]),
        Paragraph("Detail Aktivitas", styles["Section"]),
        Table(activity_table, colWidths=[42 * mm, 53 * mm, 30 * mm, 30 * mm], repeatRows=1, style=[("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e0e7ff")), ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#312e81")), ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#e2e8f0")), ("FONTSIZE", (0, 0), (-1, -1), 7), ("VALIGN", (0, 0), (-1, -1), "TOP")]),
    ]
    document.build(story)
    return Response(content=pdf_buffer.getvalue(), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="event-report-{filename}.pdf"'})

    html = f"""<!doctype html>
<html lang="id"><head><meta charset="utf-8"><title>Laporan Event - {event_name}</title>
<style>
@page {{ size: A4; margin: 18mm 16mm; }}
* {{ box-sizing: border-box; }} body {{ margin: 0; color: #172033; background: #eef2f7; font: 13px Arial, sans-serif; }}
.page {{ max-width: 920px; margin: 28px auto; padding: 42px 46px; background: #fff; }}
.brand {{ color: #4f46e5; font-size: 12px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; }}
h1 {{ margin: 10px 0 6px; color: #111827; font-size: 30px; }} h2 {{ margin: 30px 0 12px; color: #111827; font-size: 16px; }}
.muted {{ color: #64748b; }} .header {{ display: flex; justify-content: space-between; gap: 24px; border-bottom: 2px solid #4f46e5; padding-bottom: 24px; }}
.meta {{ min-width: 190px; color: #475569; line-height: 1.8; text-align: right; }}
.cards {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; margin-top: 24px; }}
.card {{ padding: 16px; border: 1px solid #e2e8f0; border-radius: 8px; background: #f8fafc; }} .card strong {{ display: block; color: #111827; font-size: 24px; }} .card span {{ color: #64748b; font-size: 11px; }}
.grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }} table {{ width: 100%; border-collapse: collapse; }} th {{ color: #475569; background: #f1f5f9; font-size: 11px; text-align: left; text-transform: uppercase; }} th, td {{ border-bottom: 1px solid #e2e8f0; padding: 9px 10px; }}
.badge, .direction {{ display: inline-block; border-radius: 999px; padding: 3px 8px; font-size: 10px; font-weight: 700; text-transform: uppercase; }} .badge {{ color: #475569; background: #e2e8f0; }} .direction.in {{ color: #047857; background: #d1fae5; }} .direction.out {{ color: #b45309; background: #fef3c7; }} .empty {{ color: #94a3b8; text-align: center; }}
.footer {{ margin-top: 34px; border-top: 1px solid #e2e8f0; padding-top: 14px; color: #94a3b8; font-size: 11px; }}
@media print {{ body {{ background: #fff; }} .page {{ max-width: none; margin: 0; padding: 0; }} }} @media (max-width: 700px) {{ .page {{ margin: 0; padding: 24px; }} .header {{ display: block; }} .meta {{ margin-top: 16px; text-align: left; }} .cards {{ grid-template-columns: repeat(2, 1fr); }} .grid {{ grid-template-columns: 1fr; }} }}
</style></head><body><main class="page">
<header class="header"><div><div class="brand">Vision Admin · Event Report</div><h1>{event_name}</h1><div class="muted">Laporan hasil monitoring pengunjung per event</div></div>
<div class="meta"><b>Nama Event</b><br>{event_name}<br><b>Dibuat</b><br>{generated_at}</div></header>
<section class="cards"><div class="card"><strong>{len(in_records):,}</strong><span>Total masuk</span></div><div class="card"><strong>{len(out_records):,}</strong><span>Total keluar</span></div><div class="card"><strong>{unique_visitors:,}</strong><span>Visitor unik</span></div><div class="card"><strong>{len(sessions):,}</strong><span>Total sesi</span></div></section>
<h2>Ringkasan Pengunjung</h2><div class="grid"><table><tr><th>Kategori</th><th>Jumlah</th></tr><tr><td>Laki-laki</td><td>{gender_counts.get('male', 0):,}</td></tr><tr><td>Perempuan</td><td>{gender_counts.get('female', 0):,}</td></tr><tr><td>Belum teridentifikasi</td><td>{gender_counts.get('unknown', 0):,}</td></tr><tr><td><b>Jam tersibuk</b></td><td><b>{f'{peak_hour:02d}:00' if peak_hour is not None else '-'}</b></td></tr></table><table><tr><th>Informasi</th><th>Nilai</th></tr><tr><td>Jadwal event</td><td>{escape(event.event_start or '-')} - {escape(event.event_end or '-')}</td></tr><tr><td>Nama perusahaan</td><td>{company_name}</td></tr><tr><td>Periode sesi</td><td>{len(sessions)} sesi</td></tr><tr><td>Zona waktu</td><td>{escape(timezone_name)}</td></tr></table></div>
<h2>Distribusi Pengunjung per Jam</h2><table><tr><th>Waktu {escape(timezone_name)}</th><th>Pengunjung masuk</th></tr>{hourly_rows}</table>
<h2>Sesi Monitoring</h2><table><tr><th>No</th><th>Nama sesi</th><th>Mulai</th><th>Selesai</th><th>Status</th></tr>{session_rows}</table>
<h2>Detail Aktivitas</h2><table><tr><th>Waktu {escape(timezone_name)}</th><th>Visitor</th><th>Gender</th><th>Arah</th></tr>{activity_rows}</table>
<footer class="footer">Dokumen ini dibuat otomatis oleh Vision Admin. Data ditampilkan berdasarkan aktivitas yang tersimpan pada event ini.</footer>
</main></body></html>"""
    return HTMLResponse(
        content=html,
        headers={"Content-Disposition": f'attachment; filename="event-report-{filename}.html"'},
    )
