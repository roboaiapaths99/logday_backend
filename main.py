from permissions import require_feature, require_any_feature, require_admin_session
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from collections import deque
from datetime import datetime, timezone
import uvicorn
import os
import re
import time
import uuid
import traceback
import logging
from dotenv import load_dotenv
from auth import get_current_admin
from models import Admin

SERVER_START_TIME = time.time()
RECENT_SLOW_REQUESTS = deque(maxlen=50)
RECENT_SERVER_ERRORS = deque(maxlen=50)

# Configure Logging
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "backend.log"))
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

from database import (
    employees_collection, attendance_logs_collection, settings_collection, admins_collection,
    organizations_collection, visit_plans_collection, visit_logs_collection,
    location_pings_collection, km_reimbursements_collection, expense_claims_collection, otps_collection,
    alerts_collection, leave_requests_collection,
    visit_plan_templates_collection, nudge_logs_collection,
    wfh_sessions_collection,
    wfh_screenshots_collection,
    wfh_activity_collection,
    wfh_app_usage_collection,
    wfh_productivity_collection,
    wfh_alerts_collection,
    wfh_meetings_collection,
    wfh_device_info_collection,
    wfh_signals_collection,
    wfh_commands_collection,
    wfh_face_checks_collection,
    wfh_requests_collection,
    ensure_employee_indexes,
    onboardings_collection,
    payrolls_collection,
    exit_managements_collection,
    salary_structures_collection,
    holidays_collection,
    leave_policies_collection,
    tasks_collection,
    absence_alerts_collection
)
from attendance_service import classify_attendance_day

# Re-export shared helpers and scheduler for 100% backward compatibility with any external imports
from services.common_helpers import (
    GEOCODE_CACHE, MAX_GEOCODE_CACHE_SIZE,
    UPLOAD_DIR, PUBLIC_UPLOAD_FOLDERS,
    S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY,
    S3_BUCKET_NAME, S3_REGION_NAME, S3_PUBLIC_URL_PREFIX,
    build_static_upload_url, sanitize_public_settings,
    send_security_alert_notification, trigger_alert, get_s3_key_from_url,
    get_screenshot_display_url, upload_file_to_storage, delete_file_from_storage,
    get_effective_org_settings, get_today_start, calculate_haversine,
    is_point_in_polygon, attendance_type_str, _is_superadmin,
    get_employee_filter, get_scoped_employee_ids, get_scoped_employee_emails,
    get_scoped_employee_employee_ids, check_feature_access, get_current_any_user,
    normalize_date_str, count_days_between, sync_holiday_days_off,
    calculate_leave_balance, send_expo_push_notifications, verify_wfh_device,
    scan_screenshot_ocr_threats
)
from services.scheduler_jobs import (
    scheduler, check_missed_visits, purge_old_screenshots
)

APP_ENV = os.getenv("APP_ENV", "development")

app = FastAPI(
    title="Log Day AI Attendance API",
    version="1.0.0",
    docs_url="/docs" if APP_ENV != "production" else None,
    redoc_url="/redoc" if APP_ENV != "production" else None,
)

from body_limits import RequestBodyLimitMiddleware
app.add_middleware(RequestBodyLimitMiddleware)

# 1. Existing HRMS & Specialized Routers
from hrms_onboarding import router as onboarding_router, employee_onboarding_router
from hrms_payroll import router as payroll_router
from hrms_exit import router as exit_router, employee_exit_router
from hrms_employee import router as employee_router
from hrms_wfh import router as wfh_router
from flyfield import router as flyfield_router
from absence_alerts import router as absence_alerts_router, check_uninformed_absences_4pm

from invitations import router as invitations_router
app.include_router(invitations_router)
from session_routes import router as session_router
app.include_router(session_router)
from upload_security import router as uploads_router
app.include_router(uploads_router)
app.include_router(onboarding_router)
app.include_router(employee_onboarding_router)
app.include_router(payroll_router)
app.include_router(exit_router)
app.include_router(employee_exit_router)
app.include_router(employee_router)
app.include_router(wfh_router)
app.include_router(flyfield_router)
app.include_router(absence_alerts_router)

# 2. Configure GZip Compression & CORS
app.add_middleware(GZipMiddleware, minimum_size=1000)

_env_origins = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
allowed_origins = set(_env_origins + ([] if APP_ENV == "production" else [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://localhost:5180",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5173",
    "http://127.0.0.1:5180",
    "https://logday-api.duckdns.org",
]))
if "*" in allowed_origins:
    raise RuntimeError("ALLOWED_ORIGINS must contain explicit origins, not a wildcard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(allowed_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*", "X-Request-ID", "X-Process-Time-Ms"],
)

@app.middleware("http")
async def add_cors_and_cache_headers(request: Request, call_next):
    req_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:8]
    request.state.request_id = req_id
    start_perf = time.perf_counter()

    response = await call_next(request)

    duration_ms = round((time.perf_counter() - start_perf) * 1000, 2)
    if duration_ms > 500:
        logger.warning(f"[SLOW API] [{req_id}] {request.method} {request.url.path} took {duration_ms}ms (status={response.status_code})")
        RECENT_SLOW_REQUESTS.appendleft({
            "request_id": req_id,
            "method": request.method,
            "path": request.url.path,
            "duration_ms": duration_ms,
            "status_code": response.status_code,
            "timestamp": datetime.now(timezone.utc).isoformat()
        })

    response.headers["X-Request-ID"] = req_id
    response.headers["X-Process-Time-Ms"] = str(duration_ms)

    # Enterprise OWASP HTTP Security Headers
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

# 3. API Prefix Middleware (/api/* auto-routing to legacy root paths)
_KNOWN_ROUTES_CACHE = set()

def _get_app_route_paths():
    global _KNOWN_ROUTES_CACHE
    if not _KNOWN_ROUTES_CACHE and hasattr(app, "routes"):
        _KNOWN_ROUTES_CACHE = {r.path for r in app.routes if hasattr(r, "path")}
    return _KNOWN_ROUTES_CACHE

@app.middleware("http")
async def api_prefix_handler(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        known = _get_app_route_paths()
        if path not in known:
            new_path = path[4:]  # strip /api
            if new_path in known:
                scope = request.scope.copy()
                scope["path"] = new_path
                request = Request(scope)

    return await call_next(request)

# 4. Global Exception Handlers with Request ID & File/Line Diagnostic Context
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Standardize HTTP exception responses for frontend mapping with CORS headers."""
    req_id = getattr(request.state, "request_id", "unknown")
    origin = request.headers.get("origin")
    response = JSONResponse(
        status_code=exc.status_code,
        headers=exc.headers,
        content={
            "status": "error",
            "code": f"HTTP_{exc.status_code}",
            "detail": exc.detail,
            "type": "standard_error",
            "request_id": req_id
        },
    )
    if origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    response.headers["X-Request-ID"] = req_id
    return response

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Catch-all for internal server errors with exact router/service file:line debug tracing."""
    req_id = getattr(request.state, "request_id", "unknown")
    tb_list = traceback.extract_tb(exc.__traceback__)
    loc_info = "unknown"
    if tb_list:
        last_frame = tb_list[-1]
        loc_info = f"{os.path.basename(last_frame.filename)}:{last_frame.lineno} in {last_frame.name}"
    logger.exception(f"[ERROR] [{req_id}] {request.method} {request.url.path} at {loc_info} -> {str(exc)}")
    RECENT_SERVER_ERRORS.appendleft({
        "request_id": req_id,
        "method": request.method,
        "path": request.url.path,
        "location": loc_info,
        "error": str(exc),
        "timestamp": datetime.now(timezone.utc).isoformat()
    })
    origin = request.headers.get("origin")
    response = JSONResponse(
        status_code=500,
        content={
            "status": "error",
            "code": "INTERNAL_SERVER_ERROR",
            "detail": "An unexpected error occurred on the server." if APP_ENV == "production" else f"{str(exc)} ({loc_info})",
            "type": "system_error",
            "request_id": req_id
        },
    )
    if origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    response.headers["X-Request-ID"] = req_id
    return response

# 5. Mount public logos directory for static asset delivery
app.mount("/uploads/logos", StaticFiles(directory=os.path.join(UPLOAD_DIR, "logos")), name="logos")

# 6. Startup & Shutdown Lifecycle Events
@app.on_event("startup")
async def startup_db_client():
    """Create indexes and start background scheduler jobs on startup (non-fatal if DB is temporarily unreachable)."""
    try:
        await ensure_employee_indexes()
    except Exception as e:
        logger.warning(f"ensure_employee_indexes skipped: {e}")

    db_indexes = [
        # Core & GIS Indexes
        (employees_collection, "employee_id", {}),
        (attendance_logs_collection, [("user_id", 1), ("timestamp", -1)], {}),
        (attendance_logs_collection, [("email", 1), ("timestamp", -1)], {}),
        (employees_collection, "organization_id", {}),
        (attendance_logs_collection, "organization_id", {}),
        (visit_plans_collection, [("employee_id", 1), ("status", 1)], {}),
        (visit_logs_collection, [("employee_id", 1), ("timestamp", -1)], {}),
        (visit_logs_collection, [("employee_email", 1), ("date", -1)], {}),
        (visit_logs_collection, [("check_in_location", "2dsphere")], {}),
        (location_pings_collection, [("location", "2dsphere")], {}),
        (location_pings_collection, [("employee_id", 1), ("recorded_at", -1)], {}),
        (km_reimbursements_collection, [("employee_id", 1), ("date", -1)], {}),
        (otps_collection, "expires_at", {"expireAfterSeconds": 0}),
        # High-Traffic HRMS, Leave, Payroll, WFH & Absence Alert Compound Indexes
        (leave_requests_collection, [("organization_id", 1), ("status", 1)], {}),
        (leave_requests_collection, [("employee_id", 1), ("status", 1)], {}),
        (leave_requests_collection, [("email", 1), ("status", 1)], {}),
        (payrolls_collection, [("organization_id", 1), ("payroll_month", 1)], {}),
        (payrolls_collection, [("employee_email", 1), ("payroll_month", -1)], {}),
        (holidays_collection, [("organization_id", 1), ("payroll_month", 1)], {}),
        (onboardings_collection, [("organization_id", 1), ("status", 1)], {}),
        (onboardings_collection, [("employee_email", 1), ("organization_id", 1)], {}),
        (exit_managements_collection, [("organization_id", 1), ("status", 1)], {}),
        (exit_managements_collection, [("employee_email", 1), ("organization_id", 1)], {}),
        (wfh_activity_collection, [("employee_email", 1), ("organization_id", 1), ("timestamp", -1)], {}),
        (wfh_app_usage_collection, [("employee_email", 1), ("organization_id", 1), ("timestamp", -1)], {}),
        (wfh_productivity_collection, [("employee_email", 1), ("organization_id", 1), ("timestamp", -1)], {}),
        (wfh_alerts_collection, [("organization_id", 1), ("timestamp", -1)], {}),
        (absence_alerts_collection, [("organization_id", 1), ("date", -1)], {}),
        (absence_alerts_collection, "token", {}),
        (tasks_collection, [("employee_email", 1), ("organization_id", 1)], {}),
    ]
    for col, keys, kwargs in db_indexes:
        try:
            await col.create_index(keys, **kwargs)
        except Exception as e:
            logger.warning(f"Index creation skipped ({keys}): {type(e).__name__}: {str(e)[:80]}")
    logger.info("MongoDB Enterprise, HRMS, WFH & GIS Indexes checked/created.")

    try:
        scheduler.add_job(check_missed_visits, "interval", hours=1, id="check_missed_visits", replace_existing=True)
        scheduler.add_job(purge_old_screenshots, "cron", hour=2, minute=0, id="purge_old_screenshots", replace_existing=True)
        scheduler.add_job(check_uninformed_absences_4pm, "interval", minutes=5, id="check_uninformed_absences_4pm", replace_existing=True)
        if not scheduler.running:
            scheduler.start()
        logger.info("APScheduler started: check_missed_visits hourly, check_uninformed_absences_4pm every 5 mins, purge_old_screenshots daily at 2 AM.")
    except Exception as e:
        logger.warning(f"Scheduler startup warning: {e}")

    logger.info(f"Application startup complete. ENV={APP_ENV}")
    print("Startup complete.")

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanly shut down scheduler and connections on process exit."""
    try:
        if scheduler.running:
            scheduler.shutdown(wait=False)
            logger.info("Scheduler shut down successfully.")
    except Exception as e:
        logger.warning(f"Error during scheduler shutdown: {e}")

# 7. Modular Domain Routers
from routers import (
    system_uploads_router,
    auth_employee_router,
    attendance_core_router,
    admin_core_router,
    field_ops_router,
    leaves_holidays_router,
    reports_nudges_router,
    wfh_employee_router,
    wfh_admin_router,
)

app.include_router(system_uploads_router)
app.include_router(auth_employee_router)
app.include_router(attendance_core_router)
app.include_router(admin_core_router)
app.include_router(field_ops_router)
app.include_router(leaves_holidays_router)
app.include_router(reports_nudges_router)
app.include_router(wfh_employee_router)
app.include_router(wfh_admin_router)

@app.get("/admin/system/diagnostics", tags=["System"])
async def get_system_diagnostics(current_admin: Admin = Depends(require_feature("settings"))):
    """Real-time admin diagnostics: server uptime, route count, scheduler jobs, recent slow APIs (>500ms), and recent server errors."""
    jobs_info = []
    try:
        if hasattr(scheduler, "get_jobs"):
            for j in scheduler.get_jobs():
                jobs_info.append({
                    "id": getattr(j, "id", ""),
                    "next_run_time": str(getattr(j, "next_run_time", ""))
                })
    except Exception:
        pass

    return {
        "status": "healthy",
        "env": APP_ENV,
        "uptime_seconds": round(time.time() - SERVER_START_TIME, 1),
        "total_registered_routes": len(app.routes),
        "geocode_cache_entries": len(GEOCODE_CACHE),
        "scheduler_running": bool(getattr(scheduler, "running", False)),
        "scheduled_jobs": jobs_info,
        "recent_slow_requests": list(RECENT_SLOW_REQUESTS),
        "recent_server_errors": list(RECENT_SERVER_ERRORS),
    }


@app.get("/debug/routes")
async def debug_routes(current_admin: Admin = Depends(require_feature("settings"))):
    if APP_ENV == "production" and not _is_superadmin(current_admin):
        raise HTTPException(status_code=403, detail="Debug endpoint not available in production")
    return [route.path for route in app.routes]

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=False, reload_excludes=["*.log", "logs/*"])
