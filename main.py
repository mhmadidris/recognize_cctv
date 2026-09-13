from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from pathlib import Path
from app.routers import cctv
from app.middlewares.log import LogMiddleware
from app.middlewares.rate_limit import RateLimitMiddleware
from app.services.event_visitor import event_visitor_manager
from app.exceptions.log import LogError
from starlette.exceptions import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi import APIRouter

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize resources here
    print("Application starting...")
    event_visitor_manager.start_scheduler()
    try:
        yield
    finally:
        event_visitor_manager.stop_scheduler()
        cctv.cctv_recognition_service.stop()
        # Clean up resources here
        print("Application shutting down...")

app = FastAPI(lifespan=lifespan)
log = LogError()

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiting must run before endpoint handlers and is logged by LogMiddleware.
app.add_middleware(RateLimitMiddleware)

# Logging middleware
app.add_middleware(LogMiddleware)

# Exception handlers
app.add_exception_handler(RequestValidationError, log.request_validation_exception_handler)
app.add_exception_handler(HTTPException, log.http_exception_handler)
app.add_exception_handler(Exception, log.unhandled_exception_handler)

from app.routers import cctv, event_visitor, event, auth

# API v1 routes
api_v1 = APIRouter(prefix="/api/v1")
api_v1.include_router(
    cctv.router,
    tags=["CCTV Recognition"],
    prefix="/cctv",
)
api_v1.include_router(
    event_visitor.router,
    tags=["Event Visitor Counter"],
    prefix="/event_visitor",
)
api_v1.include_router(
    event.router,
)
api_v1.include_router(
    auth.router,
)

app.include_router(api_v1)

STATIC_DIR = Path(__file__).resolve().parent / "app" / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/")
def read_root():
    return {
        "message": "It's FINE ✅",
        "success": True,
    }


@app.get("/cctv-flow")
def cctv_flow():
    flow_file = Path(__file__).resolve().parents[1] / "machine-learning-fe" / "public" / "cctv-flow.html"
    if flow_file.exists():
        return FileResponse(flow_file)
    return FileResponse(Path(__file__).resolve().parent / "docs" / "business-flow-proposal.md")
