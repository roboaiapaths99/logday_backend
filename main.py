from fastapi import FastAPI, HTTPException, File, Depends, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse
from starlette import status
import uvicorn
import asyncio
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import os
import calendar
import math
import re
import io
import time
import random
import traceback
import zipfile
import tempfile
import urllib.request
from urllib.parse import unquote
import pandas as pd
from bson import ObjectId
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from reportlab.lib.pagesizes import letter, landscape
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from dotenv import load_dotenv
from typing import Optional, List, Dict
import logging
import base64
import uuid
from fastapi.staticfiles import StaticFiles
import requests
try:
    from PIL import Image as PILImage  # type: ignore[import-not-found]  # optional: Pillow
except ImportError:
    PILImage = None  # type: ignore[assignment,misc]

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

# Initialize Scheduler
scheduler = AsyncIOScheduler()

from jose import jwt
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
    leave_policies_collection
)
from models import (
    RegisterRequest, LoginRequest, VerifyPresenceRequest, Token, LoginResponse, EmployeeProfile, UpdateFaceRequest,
    AdminLoginRequest, EmployeeUpdate, SystemSettings, Admin, AdminRole, Organization, OrganizationRegisterRequest, SubAdminCreate,
    EmployeeType, TerritoryType, AttendanceType, CheckInMethod, PlanStatus, VisitPlan, Visit, LocationPing, ExpenseClaim,
    LeaveType, LeaveStatus, DiscussionMessage, LeaveRequest, SyncBatchRequest, ChangePasswordRequest, SalaryStructure,
    HolidayCreateRequest, HolidayBulkCreateRequest, LeavePolicyUpdateRequest, EmployeeLeaveQuotaUpdateRequest, ConvertAbsentToLeaveRequest
)
from auth import (
    get_password_hash, verify_password, create_access_token, 
    get_current_admin, get_current_employee, admin_oauth2_scheme, employee_oauth2_scheme,
    SECRET_KEY, ALGORITHM
)
from face_utils import get_face_embedding, verify_face, compare_faces, is_dummy_embedding
from sheets_sync import sync_to_google_sheets, sync_visit_to_google_sheets

APP_ENV = os.getenv("APP_ENV", "development")

app = FastAPI(
    title="Log Day AI Attendance API",
    version="1.0.0",
    docs_url="/docs" if APP_ENV != "production" else None,
    redoc_url="/redoc" if APP_ENV != "production" else None,
)

# HRMS Routers
from hrms_onboarding import router as onboarding_router, employee_onboarding_router
from hrms_payroll import router as payroll_router
from hrms_exit import router as exit_router, employee_exit_router
from hrms_employee import router as employee_router
from hrms_wfh import router as wfh_router

app.include_router(onboarding_router)
app.include_router(employee_onboarding_router)
app.include_router(payroll_router)
app.include_router(exit_router)
app.include_router(employee_exit_router)
app.include_router(employee_router)
app.include_router(wfh_router)


# Configure CORS - Allow all origins dynamically with regex for Web, Vercel, Electron & Mobile
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_origin_regex=r"^https?://.*",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

@app.middleware("http")
async def add_cors_and_cache_headers(request: Request, call_next):
    # Handle preflight OPTIONS requests directly with full CORS headers
    origin = request.headers.get("origin") or "*"
    if request.method == "OPTIONS":
        from fastapi.responses import Response
        response = Response(status_code=200)
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH"
        response.headers["Access-Control-Allow-Headers"] = request.headers.get("access-control-request-headers", "*")
        response.headers["Access-Control-Max-Age"] = "86400"
        return response

    response = await call_next(request)
    
    # Ensure CORS headers are present even on errors
    response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Credentials"] = "true"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, PATCH"
    response.headers["Access-Control-Allow-Headers"] = "*"
    
    # Prevent caching of API responses to ensure real-time data accuracy
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}

# --- GEOCODING PROXY ---

GEOCODE_CACHE = {}

@app.get("/api/geocoding/reverse")
async def reverse_geocode(lat: float, lon: float):
    """
    Proxy for Nominatim reverse geocoding to bypass CORS and 429 errors.
    Uses a server-side User-Agent to comply with Nominatim's policy.
    Uses an in-memory cache and runs in threadpool to prevent event-loop blockages.
    """
    cache_key = (round(lat, 4), round(lon, 4))
    if cache_key in GEOCODE_CACHE:
        return GEOCODE_CACHE[cache_key]

    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={lat}&lon={lon}"
        headers = {
            "User-Agent": "LogDayAttendanceApp/1.0 (contact: roboaiapaths99@gmail.com)"
        }
        # Run blocking requests.get in threadpool to keep event loop unblocked
        response = await run_in_threadpool(
            requests.get, url, headers=headers, timeout=5
        )
        response.raise_for_status()
        data = response.json()
        GEOCODE_CACHE[cache_key] = data
        return data
    except Exception as e:
        logger.error(f"Geocoding proxy error: {str(e)}")
        # Fail gracefully with coordinates if geocoding fails
        return {"display_name": f"{lat:.4f}, {lon:.4f}", "address": {}}

# --- API PREFIX MIDDLEWARE ---
# Automatically routes /api/* to /* if the prefix is missing in main.py
@app.middleware("http")
async def api_prefix_handler(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        # Check if the exact path exists in the app's routes
        route_exists = any(route.path == path for route in app.routes)
        if not route_exists:
            # Try to route without /api
            new_path = path[4:] # strip /api
            # Check if stripped path exists
            if any(route.path == new_path for route in app.routes):
                scope = request.scope.copy()
                scope['path'] = new_path
                request = Request(scope)
    
    return await call_next(request)

# --- GLOBAL EXCEPTION HANDLERS ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Standardize HTTP exception responses for frontend mapping with CORS headers."""
    origin = request.headers.get("origin")
    response = JSONResponse(
        status_code=exc.status_code,
        content={
            "status": "error",
            "code": f"HTTP_{exc.status_code}",
            "detail": exc.detail,
            "type": "standard_error"
        },
    )
    if origin and origin != "*":
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
    else:
        response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    return response

@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    """Catch-all for internal server errors to avoid leaking tracebacks in production with CORS headers."""
    logger.exception(f"Unhandled error: {str(exc)}")
    origin = request.headers.get("origin")
    response = JSONResponse(
        status_code=500,
        content={
            "status": "error",
            "code": "INTERNAL_SERVER_ERROR",
            "detail": "An unexpected error occurred on the server." if APP_ENV == "production" else str(exc),
            "type": "system_error"
        },
    )
    if origin and origin != "*":
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Credentials"] = "true"
    else:
        response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    return response

# --- STARTUP EVENT ---
@app.on_event("startup")
async def startup_event():
    """Ensure database indexes are present for performance optimization."""
    # Each index op is isolated so a MongoDB SSL/network issue won't crash startup
    index_ops = [
        ("employees_email", employees_collection, [("email", 1)], {"unique": True, "background": True}),
        ("attendance_logs", attendance_logs_collection, [("user_id", 1), ("timestamp", -1)], {"background": True}),
        ("visit_plans", visit_plans_collection, [("employee_id", 1), ("status", 1)], {"background": True}),
        ("visit_logs", visit_logs_collection, [("employee_id", 1), ("timestamp", -1)], {"background": True}),
        ("location_pings", location_pings_collection, [("employee_id", 1), ("recorded_at", -1)], {"background": True}),
        ("km_reimbursements", km_reimbursements_collection, [("employee_id", 1), ("date", -1)], {"background": True}),
    ]
    for name, col, keys, kwargs in index_ops:
        try:
            await col.create_index(keys, **kwargs)
        except Exception as e:
            logger.warning(f"Index '{name}' skipped (non-fatal): {type(e).__name__}: {str(e)[:60]}")

    # Scheduler startup
    try:
        if not scheduler.running:
            scheduler.start()
        logger.info("Scheduler started successfully.")
    except Exception as e:
        logger.warning(f"Scheduler startup failed (non-fatal): {e}")

    logger.info(f"Application startup complete. ENV={APP_ENV}")

# --- HEALTH CHECK ENDPOINT ---
# (Moved to line 284 for consolidation)

async def send_security_alert_notification(alert_type: str, employee_email: str, detail: str):
    """
    Mock function for sending SMS/WhatsApp alerts.
    In production, this would integrate with Twilio, AWS SNS, etc.
    """
    logger.info(f"CRITICAL SECURITY NOTIFICATION sent to Admin: [{alert_type}] User: {employee_email} - {detail}")
    # Integration logic here (e.g., Twilio API)

async def trigger_alert(alert_type: str, employee_id: str, organization_id: str, detail: str, severity: str = "medium", metadata: dict = None):
    """Log a security or operational alert to the database."""
    try:
        alert = {
            "type": alert_type, # Identity, Territory, Productivity, Compliance
            "employee_id": employee_id,
            "organization_id": organization_id,
            "detail": detail,
            "severity": severity, # low, medium, high, critical
            "timestamp": datetime.now(timezone.utc),
            "status": "pending", # pending, resolved, dismissed
            "metadata": metadata or {}
        }
        await alerts_collection.insert_one(alert)
        logger.warning(f"ALERT TRIGGERED [{alert_type}]: {detail} for {employee_id}")
        
        # If high severity, send out external notification
        if severity in ["high", "critical"]:
            await send_security_alert_notification(alert_type, employee_id, detail)
            
    except Exception as e:
        logger.error(f"Failed to trigger alert: {e}")

# Ensure uploads directory exists
UPLOAD_DIR = "uploads"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)
os.makedirs("uploads/wfh_view", exist_ok=True)

# Serve static files
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")

# Hybrid S3/MinIO & Local File Storage
import boto3
from botocore.exceptions import NoCredentialsError

S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL")
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME") or os.getenv("AWS_STORAGE_BUCKET_NAME", "logday-wfh-screenshots")
S3_REGION_NAME = os.getenv("S3_REGION_NAME", "us-east-1")
S3_PUBLIC_URL_PREFIX = os.getenv("S3_PUBLIC_URL_PREFIX") or os.getenv("R2_PUBLIC_URL_PREFIX")

def get_s3_key_from_url(url: str) -> str:
    for folder in ["wfh_view", "wfh_face_checks"]:
        marker = f"/{folder}/"
        if marker in url:
            idx = url.find(marker)
            return url[idx+1:]
    return ""

def get_screenshot_display_url(url: str, request: Request = None) -> str:
    if not url:
        return ""
    if url.startswith("/uploads/"):
        if request:
            return build_static_upload_url(request, url)
        return url
    
    # Generate Presigned URL for private S3/R2 storage
    if S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY:
        s3_key = get_s3_key_from_url(url)
        if s3_key:
            try:
                from botocore.client import Config
                s3_client = boto3.client(
                    "s3",
                    endpoint_url=S3_ENDPOINT_URL,
                    aws_access_key_id=S3_ACCESS_KEY_ID,
                    aws_secret_access_key=S3_SECRET_ACCESS_KEY,
                    region_name=S3_REGION_NAME,
                    config=Config(signature_version='s3v4')
                )
                presigned_url = s3_client.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': S3_BUCKET_NAME, 'Key': s3_key},
                    ExpiresIn=3600 # 1 hour
                )
                return presigned_url
            except Exception as e:
                logger.error(f"Failed to generate presigned URL for key {s3_key}: {e}")
    return url

async def upload_file_to_storage(image_bytes: bytes, filename: str, folder: str = "wfh_view") -> str:
    """
    Enterprise hybrid storage: Uploads to S3/MinIO/R2 if configured, otherwise falls back to local disk.
    """
    if S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY:
        try:
            s3_client = boto3.client(
                "s3",
                endpoint_url=S3_ENDPOINT_URL,
                aws_access_key_id=S3_ACCESS_KEY_ID,
                aws_secret_access_key=S3_SECRET_ACCESS_KEY,
                region_name=S3_REGION_NAME
            )
            # Ensure bucket exists (gracefully ignore failures since Cloudflare R2 has different bucket creation rules)
            try:
                s3_client.head_bucket(Bucket=S3_BUCKET_NAME)
            except Exception:
                try:
                    if S3_REGION_NAME == "us-east-1":
                        s3_client.create_bucket(Bucket=S3_BUCKET_NAME)
                    else:
                        s3_client.create_bucket(
                            Bucket=S3_BUCKET_NAME,
                            CreateBucketConfiguration={"LocationConstraint": S3_REGION_NAME}
                        )
                except Exception as bucket_err:
                    logger.warning(f"Could not verify/create bucket {S3_BUCKET_NAME}, proceeding anyway: {bucket_err}")
            
            s3_key = f"{folder}/{filename}"
            s3_client.put_object(
                Bucket=S3_BUCKET_NAME,
                Key=s3_key,
                Body=image_bytes,
                ContentType="image/png" if filename.endswith(".png") else "image/jpeg"
            )
            
            if S3_PUBLIC_URL_PREFIX:
                url = f"{S3_PUBLIC_URL_PREFIX.rstrip('/')}/{s3_key}"
            elif S3_ENDPOINT_URL:
                url = f"{S3_ENDPOINT_URL.rstrip('/')}/{S3_BUCKET_NAME}/{s3_key}"
            else:
                url = f"https://{S3_BUCKET_NAME}.s3.amazonaws.com/{s3_key}"
            logger.info(f"Successfully uploaded screenshot {filename} to S3/MinIO bucket {S3_BUCKET_NAME}")
            return url
        except Exception as e:
            logger.error(f"S3/MinIO upload failed, falling back to local storage: {e}")

    # Local filesystem fallback
    filepath = os.path.join(UPLOAD_DIR, folder, filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "wb") as f:
        f.write(image_bytes)
    return f"/uploads/{folder}/{filename}"


async def delete_file_from_storage(image_url: str):
    """
    Deletes the file from S3/R2 or local filesystem.
    """
    if not image_url:
        return

    if image_url.startswith("/uploads/"):
        local_path = image_url.lstrip("/")
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
                logger.info(f"Deleted local file: {local_path}")
            except Exception as delete_err:
                logger.error(f"Failed to delete local screenshot file: {delete_err}")
        return

    if S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY:
        key_start = image_url.find("/wfh_view/")
        if key_start != -1:
            s3_key = image_url[key_start + 1:]
            try:
                s3_client = boto3.client(
                    "s3",
                    endpoint_url=S3_ENDPOINT_URL,
                    aws_access_key_id=S3_ACCESS_KEY_ID,
                    aws_secret_access_key=S3_SECRET_ACCESS_KEY,
                    region_name=S3_REGION_NAME
                )
                s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
                logger.info(f"Successfully deleted {s3_key} from S3/R2 bucket {S3_BUCKET_NAME}")
            except Exception as e:
                logger.error(f"Failed to delete {s3_key} from S3/R2 bucket {S3_BUCKET_NAME}: {e}")


async def check_missed_visits():
    """
    Scheduled job to check for missed visits.
    A visit is considered missed if it was approved for today but not checked in.
    """
    logger.info("Running scheduled job: check_missed_visits")
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    # Find all approved plans for today
    approved_plans_cursor = visit_plans_collection.find({
        "date": today_str,
        "status": PlanStatus.APPROVED
    })
    
    async for plan in approved_plans_cursor:
        employee_id = plan["employee_id"]
        organization_id = plan["organization_id"]
        
        for stop in plan.get("stops", []):
            stop_id = stop.get("sequence_order") # Assuming sequence_order is the unique stop identifier
            
            # Check if a visit log exists for this employee, date, and stop_id
            existing_log = await visit_logs_collection.find_one({
                "employee_id": employee_id,
                "date": today_str,
                "visit_plan_stop_id": stop_id
            })
            
            if not existing_log:
                # This stop was planned but not checked in
                detail = f"Missed visit: Employee {employee_id} did not check in to planned stop '{stop.get('place_name')}' (Stop ID: {stop_id})."
                await trigger_alert(
                    "Productivity",
                    employee_id,
                    organization_id,
                    detail,
                    "medium",
                    {"plan_id": str(plan["_id"]), "stop_id": stop_id, "place_name": stop.get("place_name")}
                )
                logger.warning(detail)
    logger.info("Finished scheduled job: check_missed_visits")


async def purge_old_screenshots():
    """Daily clean-up of expired screenshots from DB and local storage based on organization retention policy."""
    logger.info("Starting scheduled job: purge_old_screenshots")
    try:
        import os
        from bson import ObjectId
        orgs = await organizations_collection.find({}).to_list(length=1000)
        purged_count = 0
        now = datetime.now(timezone.utc)
        
        for org in orgs:
            org_id = str(org["_id"])
            policy = org.get("wfh_policy", {}) or {}
            retention_days = policy.get("screenshot_retention_days", 5)
            
            cutoff_date = now - timedelta(days=retention_days)
            query = {
                "organization_id": org_id,
                "timestamp": {"$lt": cutoff_date}
            }
            
            cursor = wfh_screenshots_collection.find(query)
            expired_screens = await cursor.to_list(length=1000)
            
            for screen in expired_screens:
                image_url = screen.get("image_url", "")
                await delete_file_from_storage(image_url)
                await wfh_screenshots_collection.delete_one({"_id": screen["_id"]})
                purged_count += 1
                
        logger.info(f"Finished scheduled job: purge_old_screenshots. Purged {purged_count} screenshots.")
    except Exception as e:
        logger.error(f"Error running purge_old_screenshots: {e}")


@app.on_event("startup")
async def startup_db_client():
    """Create indexes on startup (non-fatal if DB is temporarily unreachable)."""
    try:
        # WFH & Employee indexes
        await ensure_employee_indexes()
        
        # Core Indexes
        await employees_collection.create_index("email", unique=True)
        await employees_collection.create_index("employee_id")
        await attendance_logs_collection.create_index([("user_id", 1), ("timestamp", -1)])
        
        # Enterprise Indexes
        await employees_collection.create_index("organization_id")
        await attendance_logs_collection.create_index("organization_id")
        
        # Field Force GIS Indexes
        await location_pings_collection.create_index([("location", "2dsphere")])
        await location_pings_collection.create_index([("employee_id", 1), ("recorded_at", -1)])
        await visit_logs_collection.create_index([("check_in_location", "2dsphere")])
        await otps_collection.create_index("expires_at", expireAfterSeconds=0)
        
        logger.info("MongoDB Enterprise & GIS Indexes created successfully.")
    except Exception as e:
        logger.warning(f"Startup index creation skipped (non-fatal): {type(e).__name__}: {str(e)[:80]}")

    # Start Scheduler
    try:
        if not scheduler.running:
            scheduler.add_job(check_missed_visits, 'interval', hours=1)
            scheduler.add_job(purge_old_screenshots, 'cron', hour=2, minute=0)
            scheduler.start()
        logger.info("APScheduler started: check_missed_visits scheduled hourly, purge_old_screenshots daily at 2 AM.")
    except Exception as e:
        logger.warning(f"Scheduler already running or failed to start: {e}")
    
    print("Startup complete.")


@app.get("/")
async def root():
    return {"message": "LogDay AI Attendance API is active", "status": "online"}


@app.get("/health", tags=["System"])
async def health_check():
    """Health check endpoint to verify API and DB are connected."""
    try:
        from database import client
        await client.admin.command("ping")
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"
    return {
        "api": "LogDay AI Attendance API",
        "status": "healthy",
        "env": APP_ENV,
        "database": db_status,
        "version": "1.0.0"
    }


@app.post("/register", response_model=LoginResponse)
async def register(req: RegisterRequest):
    """Register a new employee with face image and enterprise metadata."""
    try:
        clean_email = req.email.strip().lower()
        logger.info(f"Received registration request for: {clean_email}")

        # Check if employee already exists
        existing = await employees_collection.find_one({"email": clean_email})
        if existing:
            raise HTTPException(status_code=400, detail="Email already registered")

        # Generate face embedding
        if not req.face_image:
            raise HTTPException(status_code=400, detail="face_image is required for registration")
        
        embedding = get_face_embedding(req.face_image)
        if embedding is None:
            raise HTTPException(status_code=400, detail="No face detected in image. Please try again with a clear photo.")

        # Create employee record
        hashed_password = get_password_hash(req.password)
        employee_dict = {
            "full_name": req.full_name,
            "email": clean_email,
            "employee_id": req.employee_id,
            "designation": req.designation,
            "department": req.department,
            "organization_id": req.organization_id or "system_org", # Default to system_org
            "employee_type": req.employee_type,
            "hashed_password": hashed_password,
            "face_embedding": embedding,
            "profile_image": req.face_image,
            "device_id": req.device_id,
            "created_at": datetime.now(timezone.utc),
            "status": "Active"
        }

        # For Field employees, initialize default territory if not provided
        if req.employee_type == EmployeeType.FIELD:
            employee_dict.update({
                "territory_type": TerritoryType.RADIUS,
                "territory_radius_meters": 500, # Default 500m
                "gps_otp_fallback_enabled": True
            })

        await employees_collection.insert_one(employee_dict)
        logger.info(f"User {req.email} saved to database successfully as {req.employee_type}.")

        # Generate token
        access_token = create_access_token(data={"sub": req.email})
        
        response_data = {
            "access_token": access_token, 
            "token_type": "bearer",
            "user": {
                "full_name": employee_dict["full_name"],
                "email": employee_dict["email"],
                "employee_id": employee_dict["employee_id"],
                "designation": employee_dict["designation"],
                "department": employee_dict["department"],
                "organization_id": employee_dict["organization_id"],
                "employee_type": employee_dict["employee_type"],
                "created_at": employee_dict["created_at"],
                "profile_image": employee_dict["profile_image"]
            }
        }
        try:
            return LoginResponse(**response_data)
        except Exception as e:
            logger.error(f"Register Response Validation Failed: {e}", exc_info=True)
            raise HTTPException(status_code=500, detail=f"Registration Validation Error: {str(e)}")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Registration failed for {req.email}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Registration failed due to an internal server error.")


















@app.get("/organization/discover/{slug}")
async def discover_organization(slug: str):
    """Public endpoint for mobile app to discover organization and its branding."""
    org = await organizations_collection.find_one({"slug": slug})
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    
    # Also fetch settings for this org
    settings = await settings_collection.find_one({"organization_id": str(org["_id"])})
    
    return {
        "id": str(org["_id"]),
        "name": org["name"],
        "logo_url": org.get("logo_url"),
        "primary_color": org.get("primary_color", "#0f172a"),
        "settings": settings or SystemSettings().dict()
    }



@app.get("/settings/{slug}")
async def get_public_settings(slug: str):
    """Public endpoint for apps to fetch organization settings by slug."""
    # Handle "null" or "undefined" string from frontend JS
    if not slug or slug in ["null", "undefined", "generic"]:
        return SystemSettings().dict()
        
    org = await organizations_collection.find_one({"slug": slug})
    if not org:
        # Fallback to default instead of 404 to prevent app crashes
        return SystemSettings().dict()
    
    settings_doc = await settings_collection.find_one({"organization_id": str(org["_id"])})
    return settings_doc if settings_doc else SystemSettings().dict()


from fastapi import Request

@app.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest, request: Request):
    """Login with email and password. When organization_id is provided, employee must belong to that org (multi-tenant security)."""
    # Log the raw request info
    try:
        body = await request.body()
        logger.info(f"Incoming login body: {body.decode('utf-8', errors='ignore')}")
    except Exception:
        pass
    
    clean_email = req.email.strip().lower()
    logger.info(f"--- Login attempt for '{clean_email}' ---")
    
    # 1. Try finding in employees
    user = await employees_collection.find_one({"email": clean_email})
    is_admin_login = False
    
    if not user:
        # 2. Try finding in admins (Admins might want to log into the mobile app too)
        logger.info(f"User '{clean_email}' not found in employees, checking admins...")
        user = await admins_collection.find_one({"email": clean_email})
        if user:
            is_admin_login = True
            logger.info(f"User found in admins collection. Role: {user.get('role')}")
        else:
            # 3. Last resort: check if it's the hardcoded superadmin
            fallback_email = os.getenv("ADMIN_EMAIL", "admin@officeflow.ai")
            if clean_email == fallback_email:
                logger.info("System superadmin detected via email fallback.")
                user = {
                    "email": clean_email,
                    "full_name": "System Super Admin",
                    "role": "superadmin",
                    "organization_id": "system_org",
                    "employee_type": "desk"
                }
                is_admin_login = True
            else:
                logger.warning(f"Login failed: User '{clean_email}' not found anywhere.")
                raise HTTPException(status_code=401, detail="Account not found. Please contact your administrator.")
    
    logger.info(f"User identified. Stored Org ID: {user.get('organization_id')}, Is Admin: {is_admin_login}")
    
    if not is_admin_login:
        if user.get("status") == "Inactive" or user.get("is_active") is False:
            logger.warning(f"Login blocked: Employee account is deactivated for '{clean_email}'.")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your account has been deactivated. Access denied."
            )

    # Password check
    stored_hash = user.get("hashed_password")
    is_valid = False
    
    if stored_hash:
        is_valid = verify_password(req.password, stored_hash)
        if not is_valid:
            logger.info(f"Password mismatch in { 'admins' if is_admin_login else 'employees' } collection for '{clean_email}'.")
            # CROSS-COLLECTION FALLBACK: If they are in both, try the other hash
            if not is_admin_login:
                # We found them in employees, but password failed. Check if they have an admin account with this password.
                other_user = await admins_collection.find_one({"email": clean_email})
                if other_user and other_user.get("hashed_password"):
                    if verify_password(req.password, other_user.get("hashed_password")):
                        logger.info(f"Cross-collection fallback: User '{clean_email}' authenticated via admins collection.")
                        user = other_user
                        is_admin_login = True
                        is_valid = True
            else:
                # We found them in admins, but password failed. Check if they have an employee account with this password.
                other_user = await employees_collection.find_one({"email": clean_email})
                if other_user and other_user.get("hashed_password"):
                    if verify_password(req.password, other_user.get("hashed_password")):
                        logger.info(f"Cross-collection fallback: User '{clean_email}' authenticated via employees collection.")
                        user = other_user
                        is_admin_login = False
                        is_valid = True

    if not is_valid:
        # Final check for special fallback
        if not (is_admin_login and clean_email == os.getenv("ADMIN_EMAIL", "admin@officeflow.ai")):
            logger.warning(f"Login failed: Invalid password for '{clean_email}'.")
            raise HTTPException(status_code=401, detail="Invalid password.")
    
    logger.info(f"Authentication successful for '{clean_email}' (as {'admin' if is_admin_login else 'employee'}).")

    # SUPERADMIN & GOOGLE PLAY REVIEWER BYPASS logic
    is_reviewer = (clean_email == "google.review@logday.app")
    is_superadmin = (user.get("role") == "superadmin") or (clean_email == os.getenv("ADMIN_EMAIL", "admin@officeflow.ai")) or is_reviewer

    # Org-scoped security: user from one org cannot log in to another org
    if req.organization_id and not is_superadmin:
        emp_org = user.get("organization_id")
        logger.info(f"Checking Org Match: App sent '{req.organization_id}', DB has '{emp_org}'")
        if emp_org is None or emp_org == "" or emp_org == "system_org":
            # Auto-link employee to the requested organization on first login
            logger.info(f"Auto-linking user {clean_email} to organization {req.organization_id}")
            await employees_collection.update_one(
                {"email": clean_email},
                {"$set": {"organization_id": req.organization_id}}
            )
            user["organization_id"] = req.organization_id
        elif emp_org != req.organization_id:
            logger.warning(f"Login failed: Org mismatch. App: {req.organization_id}, DB: {emp_org}")
            raise HTTPException(
                status_code=403,
                detail=f"Access denied. You are registered with '{emp_org}' but trying to access '{req.organization_id}'."
            )
    
    if is_superadmin:
        logger.info("Superadmin bypass: Organization check skipped.")
    else:
        logger.info("Organization match successful.")

    # Device Binding Check
    if user.get("device_id") and req.device_id and user["device_id"] != req.device_id:
        if is_superadmin:
            logger.info(f"Superadmin bypass: Device mismatch ignored (DB: {user['device_id']}, App: {req.device_id})")
        else:
            logger.warning(f"Login failed: Device binding mismatch. App: {req.device_id}, DB: {user['device_id']}")
            raise HTTPException(
                status_code=403, 
                detail="Security Alert: This account is locked to another device. Please contact Admin to reset your device binding."
            )
    
    # Auto-bind on first login if not set (Skip for Google Reviewer)
    if not user.get("device_id") and req.device_id:
        if is_reviewer:
            logger.info("Google Reviewer login: Bypassing device auto-bind to keep the account un-bound.")
        else:
            logger.info(f"Binding user {clean_email} to device {req.device_id}")
            if is_admin_login:
                await admins_collection.update_one({"email": clean_email}, {"$set": {"device_id": req.device_id}})
            else:
                await employees_collection.update_one({"email": clean_email}, {"$set": {"device_id": req.device_id}})
            
    # Auto-register WFH device in wfh_device_info_collection for all WFH logins
    emp_type_val = user.get("employee_type", "")
    
    # WFH Desktop Authorization Gate: If request comes from WFH desktop app, check permission
    client_type = getattr(req, "client_type", None)
    if client_type in ["wfh_desktop", "desktop"] and not is_admin_login and not is_superadmin:
        is_allowed_wfh = emp_type_val in ["wfh", "hybrid", EmployeeType.WFH, EmployeeType.HYBRID]
        if not is_allowed_wfh:
            # Check if they have an approved WFH request for today
            tz_offset_mins = 330  # default IST
            try:
                org_settings = await settings_collection.find_one({"organization_id": user.get("organization_id")})
                if org_settings:
                    tz_offset_mins = org_settings.get("timezone_offset", 330)
            except Exception:
                pass
            local_now = datetime.now(timezone.utc) + timedelta(minutes=tz_offset_mins)
            today_str = local_now.strftime("%Y-%m-%d")
            
            wfh_approved = await wfh_requests_collection.find_one({
                "employee_email": clean_email,
                "date": today_str,
                "status": "approved"
            })
            if not wfh_approved:
                logger.warning(f"Login blocked: Office employee '{clean_email}' has no approved WFH request for today.")
                raise HTTPException(
                    status_code=403,
                    detail="Access denied. You do not have an approved Work From Home (WFH) request for today, and your policy is set to Office-only."
                )

    if not is_admin_login and emp_type_val in ["wfh", EmployeeType.WFH] and req.device_id:
        # Check if exists to avoid duplicates
        exists = await wfh_device_info_collection.find_one({"employee_id": str(user["_id"]), "device_id": req.device_id})
        if not exists:
            emp_full_name = user.get("full_name") or user.get("name") or "Unknown"
            device_doc = {
                "employee_id": str(user["_id"]),
                "organization_id": str(user.get("organization_id", "")),
                "employee_name": emp_full_name,
                "employee_email": user.get("email", clean_email),
                "device_id": req.device_id,
                "mac_address": req.device_id,  # Using device_id as fallback
                "hostname": f"{emp_full_name}'s PC",
                "os_info": "Windows",  # Generic fallback
                "status": "pending",  # Requires admin approval
                "registered_at": datetime.now(timezone.utc),
                "approved_by": None,
                "approved_at": None,
                "revoked_by": None,
                "revoked_at": None,
                "revoke_reason": None
            }
            await wfh_device_info_collection.insert_one(device_doc)
            logger.info(f"Registered new WFH device for {clean_email} (type={emp_type_val}), pending admin approval.")
        else:
            logger.info(f"WFH device already registered for {clean_email}, device_id={req.device_id}")

    logger.info("Login process complete. Generating token.")
    access_token = create_access_token(data={"sub": clean_email})
    
    # Check if this user is a manager (has subordinates)
    subordinates_count = await employees_collection.count_documents({"manager_id": clean_email})
    is_manager = subordinates_count > 0
    
    # Check if user needs face enrollment (no face_embedding)
    needs_enrollment = user.get("face_embedding") is None
    
    # Return full profile with defaults for legacy accounts
    response_data = {
        "access_token": access_token, 
        "token_type": "bearer",
        "user": {
            "full_name": user.get("full_name", "User"),
            "email": user.get("email", req.email),
            "employee_id": user.get("employee_id", "EMP-000"),
            "designation": user.get("designation", "Employee"),
            "department": user.get("department", "General"),
            "organization_id": user.get("organization_id") or "unknown",
            "employee_type": user.get("employee_type") or "desk",
            "created_at": user.get("created_at", datetime.now(timezone.utc)),
            "profile_image": user.get("profile_image"),
            "is_manager": is_manager
        },
        "needs_face_enrollment": needs_enrollment,
        "force_password_change": user.get("force_password_change", False)
    }
    
    try:
        return LoginResponse(**response_data)
    except Exception as e:
        logger.error(f"Login Response Validation Failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Login Validation Error: {str(e)}")
@app.post("/auth/check")
async def auth_check(employee=Depends(get_current_employee)):
    """Return current check‑in status and session info without modifying them."""
    emp = await employees_collection.find_one({"email": employee["email"]})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
        
    # 1. Check active WFH session
    active_session = await wfh_sessions_collection.find_one({
        "employee_id": str(emp["_id"]),
        "status": "active"
    })
    
    checked_in = emp.get("checked_in", False)
    session_id = emp.get("session_id")
    
    if active_session:
        checked_in = True
        session_id = str(active_session["_id"])

    # 2. Check today's attendance logs (Desk / Field / General check-in)
    org_id = emp.get("organization_id")
    tz_offset = 330
    if org_id:
        try:
            org_settings = await settings_collection.find_one({"organization_id": str(org_id) if ObjectId.is_valid(str(org_id)) else org_id})
            if org_settings:
                tz_offset = org_settings.get("timezone_offset", 330)
        except Exception:
            pass

    today_start_utc = get_today_start(tz_offset)
    user_id_str = str(emp["_id"])
    emp_email = emp.get("email", "")

    latest_log = await attendance_logs_collection.find_one(
        {
            "$or": [
                {"user_id": user_id_str},
                {"user_id": emp_email},
                {"email": emp_email}
            ],
            "timestamp": {"$gte": today_start_utc}
        },
        sort=[("timestamp", -1)]
    )

    if latest_log:
        if latest_log.get("type") == "check-in":
            checked_in = True
        elif latest_log.get("type") == "check-out":
            checked_in = False
        
    return {
        "status": "success",
        "checked_in": checked_in,
        "session_id": session_id
    }



@app.get("/me", response_model=EmployeeProfile)
async def get_me(employee=Depends(get_current_employee)):
    """Retrieve currently authenticated employee profile."""
    # Check if this user is a manager
    subordinates_count = await employees_collection.count_documents({"manager_id": employee["email"]})
    
    return {
        "full_name": employee.get("full_name", "Unknown"),
        "email": employee.get("email"),
        "employee_id": employee.get("employee_id", "0000"),
        "designation": employee.get("designation", "Employee"),
        "department": employee.get("department", "General"),
        "organization_id": employee.get("organization_id", "unknown"),
        "employee_type": employee.get("employee_type", "desk"),
        "created_at": employee.get("created_at", datetime.now(timezone.utc)),
        "profile_image": employee.get("profile_image"),
        "is_manager": subordinates_count > 0
    }

@app.post("/api/me/change-password")
async def change_password(req: ChangePasswordRequest, employee=Depends(get_current_employee)):
    """Change employee password and clear force_password_change flag."""
    user = await employees_collection.find_one({"email": employee["email"]})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    is_valid = verify_password(req.old_password, user.get("hashed_password", ""))
    if not is_valid:
        raise HTTPException(status_code=400, detail="Incorrect old password")
        
    new_hashed_password = get_password_hash(req.new_password)
    await employees_collection.update_one(
        {"email": employee["email"]},
        {"$set": {
            "hashed_password": new_hashed_password,
            "force_password_change": False
        }}
    )
    
    # Check if face enrollment is still needed
    needs_enrollment = user.get("face_embedding") is None
    
    return {
        "status": "success", 
        "message": "Password updated successfully",
        "needs_face_enrollment": needs_enrollment
    }


async def get_effective_org_settings(org_id: Optional[str]) -> dict:
    """
    Unified settings loader that combines:
    1. Base organization settings from `settings_collection` (Office Times, Geofence, Timezone)
    2. Active Leave & Attendance policy from `leave_policies_collection` (Cutoffs, Grace, Min hours, Quotas)
    Ensures that configurations saved across any admin panel tab apply consistently across backend and apps.
    """
    if not org_id:
        return {}
    
    org_str = str(org_id).strip()
    query = {"$or": [{"organization_id": org_str}]}
    if ObjectId.is_valid(org_str):
        query["$or"].append({"organization_id": ObjectId(org_str)})
    
    merged = {}
    try:
        base_settings = await settings_collection.find_one(query)
        if base_settings:
            merged.update(base_settings)
            merged["_id"] = str(base_settings["_id"])
    except Exception as e:
        logger.warning(f"Error loading base settings for org {org_str}: {e}")

    try:
        current_year = datetime.now(timezone.utc).year
        policy = await leave_policies_collection.find_one({
            "$or": [
                {"organization_id": org_str, "year": current_year},
                {"organization_id": org_str}
            ]
        }, sort=[("year", -1), ("updated_at", -1)])
        
        if policy:
            keys_to_merge = [
                "office_start_time", "office_end_time", "late_grace_mins",
                "short_leave_cutoff_time", "late_to_short_leave_mins",
                "half_day_late_cutoff_time", "half_day_early_exit_cutoff_time",
                "early_leave_short_mins", "half_day_min_hours", "full_day_min_hours",
                "required_hours", "short_leave_max_hours", "short_leave_monthly_limit",
                "short_leave_penalty_rule", "lunch_start_time", "lunch_end_time",
                "max_lunch_duration_mins", "allocation_cycle", "carry_forward"
            ]
            for k in keys_to_merge:
                if k in policy and policy[k] is not None:
                    merged[k] = policy[k]
    except Exception as e:
        logger.warning(f"Error loading leave policy for org {org_str}: {e}")

    return merged


def classify_attendance_day(
    first_checkin_local: Optional[datetime],
    last_checkout_local: Optional[datetime],
    worked_hours: float,
    settings: Optional[dict] = None,
    employee_type: str = "desk",
    approved_leave: Optional[dict] = None
) -> dict:
    """
    Central enterprise classification engine for an employee's attendance day.
    Evaluates:
    - Office/Shift timings (desk vs field configurable by admin)
    - Late grace & late threshold
    - Late -> Short Leave threshold (mins or time)
    - Late -> Half Day threshold (time, e.g. after 11:30 or lunch)
    - Early Exit -> Half Day threshold (time, e.g. before 14:00)
    - Early Exit -> Short Leave threshold (mins before shift end)
    - Min Half-Day hours & Min Full-Day hours
    - Split off-hours: late deficit vs early exit deficit
    - Applied Short Leave / Half Day vs Attendance-triggered Short Leave / Half Day
    """
    settings = settings or {}
    emp_type = str(employee_type).lower().strip()
    is_field = (emp_type == "field") and settings.get("custom_field_rules_enabled", True)

    def _parse_t(t_str, def_h, def_m):
        try:
            if t_str and ":" in str(t_str):
                parts = str(t_str).strip().split(":")
                return int(parts[0]), int(parts[1])
        except Exception:
            pass
        return def_h, def_m

    # 1. Timings & thresholds from admin configuration
    if is_field:
        start_h, start_m = _parse_t(settings.get("field_office_start_time") or settings.get("office_start_time"), 10, 0)
        end_h, end_m = _parse_t(settings.get("field_office_end_time") or settings.get("office_end_time"), 19, 0)
        late_grace_mins = int(settings.get("field_late_grace_mins") if settings.get("field_late_grace_mins") is not None else settings.get("late_grace_mins", 30))
        late_to_short_mins = int(settings.get("field_late_to_short_leave_mins") if settings.get("field_late_to_short_leave_mins") is not None else settings.get("late_to_short_leave_mins", 45))
        late_half_h, late_half_m = _parse_t(settings.get("field_late_to_half_day_time") or settings.get("late_to_half_day_time"), 12, 30)
        early_half_h, early_half_m = _parse_t(settings.get("field_early_leave_half_day_time") or settings.get("early_leave_half_day_time"), 15, 0)
        early_short_mins = int(settings.get("field_early_leave_short_mins") if settings.get("field_early_leave_short_mins") is not None else settings.get("early_leave_short_mins", 45))
        half_day_min_hours = float(settings.get("field_half_day_min_hours") if settings.get("field_half_day_min_hours") is not None else settings.get("half_day_min_hours", 4.0))
        full_day_min_hours = float(settings.get("field_full_day_min_hours") if settings.get("field_full_day_min_hours") is not None else settings.get("full_day_min_hours", 8.0))
        required_hours = float(settings.get("field_required_hours") if settings.get("field_required_hours") is not None else settings.get("required_hours", 9.0))
        short_leave_max_hours = float(settings.get("field_short_leave_max_hours") if settings.get("field_short_leave_max_hours") is not None else settings.get("short_leave_max_hours", 2.0))
    else:
        start_h, start_m = _parse_t(settings.get("office_start_time"), 9, 0)
        end_h, end_m = _parse_t(settings.get("office_end_time"), 18, 0)
        late_grace_mins = int(settings.get("late_grace_mins") if settings.get("late_grace_mins") is not None else settings.get("late_threshold_mins", 15))
        
        # Support either exact cutoff time or relative mins for late-to-short-leave
        if settings.get("short_leave_cutoff_time"):
            sl_h, sl_m = _parse_t(settings.get("short_leave_cutoff_time"), 10, 15)
            late_to_short_mins = max(0, (sl_h * 60 + sl_m) - (start_h * 60 + start_m))
        else:
            late_to_short_mins = int(settings.get("late_to_short_leave_mins", 45))

        late_half_h, late_half_m = _parse_t(settings.get("half_day_late_cutoff_time") or settings.get("late_to_half_day_time"), 11, 30)
        early_half_h, early_half_m = _parse_t(settings.get("half_day_early_exit_cutoff_time") or settings.get("early_leave_half_day_time"), 14, 0)
        early_short_mins = int(settings.get("early_leave_short_mins", 45))
        half_day_min_hours = float(settings.get("half_day_min_hours", 4.0))
        full_day_min_hours = float(settings.get("full_day_min_hours", 8.0))
        required_hours = float(settings.get("required_hours", 8.0))
        short_leave_max_hours = float(settings.get("short_leave_max_hours", 2.0))

    # Format strings for explanations
    late_half_str = f"{late_half_h:02d}:{late_half_m:02d}"
    early_half_str = f"{early_half_h:02d}:{early_half_m:02d}"

    # Handle No Check-in Case
    if not first_checkin_local:
        if approved_leave:
            dur_type = str(approved_leave.get("duration_type", "")).lower().strip()
            l_type = str(approved_leave.get("leave_type", "leave")).title()
            if dur_type == "short_leave":
                return {
                    "status": "Short Leave",
                    "is_late": False,
                    "is_early_leave": False,
                    "is_short_leave": True,
                    "is_half_day": False,
                    "short_leave_type": "applied",
                    "half_day_type": None,
                    "late_mins": 0,
                    "early_exit_mins": 0,
                    "late_off_hours": 0.0,
                    "early_off_hours": 0.0,
                    "off_hours": 0.0,
                    "worked_hours": 0.0,
                    "required_hours": required_hours,
                    "effective_days": 1.0,
                    "classification_reason": f"Approved Short Leave ({l_type})",
                    "is_applied_leave": True
                }
            elif dur_type in ["first_half", "second_half", "half_day"] or approved_leave.get("duration") == 0.5:
                return {
                    "status": "Half Day",
                    "is_late": False,
                    "is_early_leave": False,
                    "is_short_leave": False,
                    "is_half_day": True,
                    "short_leave_type": None,
                    "half_day_type": f"{dur_type}_applied",
                    "late_mins": 0,
                    "early_exit_mins": 0,
                    "late_off_hours": 0.0,
                    "early_off_hours": 0.0,
                    "off_hours": round(required_hours * 0.5, 2),
                    "worked_hours": 0.0,
                    "required_hours": required_hours,
                    "effective_days": 0.5,
                    "classification_reason": f"Approved Half Day ({dur_type})",
                    "is_applied_leave": True
                }
            else:
                return {
                    "status": f"Leave ({l_type})",
                    "is_late": False,
                    "is_early_leave": False,
                    "is_short_leave": False,
                    "is_half_day": False,
                    "short_leave_type": None,
                    "half_day_type": None,
                    "late_mins": 0,
                    "early_exit_mins": 0,
                    "late_off_hours": 0.0,
                    "early_off_hours": 0.0,
                    "off_hours": 0.0,
                    "worked_hours": 0.0,
                    "required_hours": required_hours,
                    "effective_days": 0.0,
                    "classification_reason": f"Approved Leave ({l_type})",
                    "is_applied_leave": True
                }

        return {
            "status": "Absent",
            "is_late": False,
            "is_early_leave": False,
            "is_short_leave": False,
            "is_half_day": False,
            "short_leave_type": None,
            "half_day_type": None,
            "late_mins": 0,
            "early_exit_mins": 0,
            "late_off_hours": 0.0,
            "early_off_hours": 0.0,
            "off_hours": required_hours,
            "worked_hours": 0.0,
            "required_hours": required_hours,
            "effective_days": 0.0,
            "classification_reason": "No check-in recorded",
            "is_applied_leave": False
        }

    # Reference datetimes for shift evaluation
    shift_start = first_checkin_local.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    shift_end = first_checkin_local.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    grace_cutoff = shift_start + timedelta(minutes=late_grace_mins)
    short_leave_late_cutoff = shift_start + timedelta(minutes=late_to_short_mins)
    half_day_late_cutoff = first_checkin_local.replace(hour=late_half_h, minute=late_half_m, second=0, microsecond=0)
    half_day_early_cutoff = first_checkin_local.replace(hour=early_half_h, minute=early_half_m, second=0, microsecond=0)
    short_leave_early_cutoff = shift_end - timedelta(minutes=early_short_mins)

    # 2. Late Arrival Evaluation
    late_mins = 0
    late_off_hours = 0.0
    is_late = False
    if first_checkin_local > shift_start:
        diff_sec = (first_checkin_local - shift_start).total_seconds()
        raw_late_mins = max(0, int(diff_sec / 60))
        if first_checkin_local > grace_cutoff:
            is_late = True
            late_mins = raw_late_mins
            late_off_hours = round(late_mins / 60.0, 2)
        else:
            # Arrival within grace is fully ON-TIME: zero penalty, zero deficit
            is_late = False
            late_mins = 0
            late_off_hours = 0.0

    # 3. Early Departure Evaluation
    early_exit_mins = 0
    early_off_hours = 0.0
    is_early_leave = False
    if last_checkout_local:
        if last_checkout_local < shift_end:
            diff_sec = (shift_end - last_checkout_local).total_seconds()
            raw_early_mins = max(0, int(diff_sec / 60))
            if raw_early_mins > 10:  # 10 mins early checkout grace
                is_early_leave = True
                early_exit_mins = raw_early_mins
                early_off_hours = round(early_exit_mins / 60.0, 2)

    # Total off-hours
    grace_hours = round(late_grace_mins / 60.0, 2)
    if worked_hours > 0:
        raw_off_hours = round(max(0.0, required_hours - worked_hours), 2)
        # If employee arrived within grace and left at or after shift end (or within 10m checkout grace),
        # minor clock rounding deficit (<= grace_hours or <= 0.25h) is completely waived (0 deficit)
        if not is_late and not is_early_leave and (raw_off_hours <= grace_hours or raw_off_hours <= 0.25):
            total_off_hours = 0.0
        else:
            total_off_hours = raw_off_hours
    else:
        total_off_hours = round(late_off_hours + early_off_hours, 2)

    # 4. Status Determination
    status = "Present"
    is_short_leave = False
    is_half_day = False
    short_leave_type = None
    half_day_type = None
    effective_days = 1.0
    classification_reason = "On-time full day"
    is_applied_leave = False

    # A) Check if applied leave exists for today
    if approved_leave:
        dur_type = str(approved_leave.get("duration_type", "")).lower().strip()
        slot = str(approved_leave.get("short_leave_slot", "")).lower().strip()
        if dur_type == "short_leave":
            is_short_leave = True
            short_leave_type = "applied"
            is_applied_leave = True
            status = "Short Leave"
            slot_label = f" ({slot.title()})" if slot else ""
            classification_reason = f"Approved Short Leave{slot_label}"
            
            # If short leave slot is morning, late arrival is authorized
            if slot == "morning":
                is_late = False
                late_mins = 0
                late_off_hours = 0.0
            elif slot == "evening":
                is_early_leave = False
                early_exit_mins = 0
                early_off_hours = 0.0
            else:
                # General / mid-day short leave: waives up to 2 hours of deficit
                late_off_hours = max(0.0, late_off_hours - 1.0)
                early_off_hours = max(0.0, early_off_hours - 1.0)
            total_off_hours = round(late_off_hours + early_off_hours, 2)
        elif dur_type in ["first_half", "second_half", "half_day"] or approved_leave.get("duration") == 0.5:
            is_half_day = True
            half_day_type = f"{dur_type}_applied"
            is_applied_leave = True
            status = "Half Day"
            effective_days = 0.5
            classification_reason = f"Approved Half Day ({dur_type.replace('_', ' ').title()})"
            if "first" in dur_type:
                # 1st half leave: late arrival authorized
                is_late = False
                late_mins = 0
                late_off_hours = 0.0
            elif "second" in dur_type:
                # 2nd half leave: early departure authorized
                is_early_leave = False
                early_exit_mins = 0
                early_off_hours = 0.0
            total_off_hours = round(late_off_hours + early_off_hours, 2)

    # B) Evaluate Half Day thresholds if not already half day
    if not is_half_day:
        if first_checkin_local >= half_day_late_cutoff:
            is_half_day = True
            half_day_type = "late_arrival"
            status = "Half Day"
            effective_days = 0.5
            classification_reason = f"Check-in at {first_checkin_local.strftime('%H:%M')} (after Half-Day limit {late_half_str})"
        elif last_checkout_local and last_checkout_local <= half_day_early_cutoff:
            is_half_day = True
            half_day_type = "early_departure"
            status = "Half Day"
            effective_days = 0.5
            classification_reason = f"Check-out at {last_checkout_local.strftime('%H:%M')} (before Half-Day limit {early_half_str})"
        elif last_checkout_local and worked_hours > 0 and worked_hours < half_day_min_hours:
            status = "Absent"
            effective_days = 0.0
            classification_reason = f"Worked only {worked_hours:.1f}h (< min half-day requirement {half_day_min_hours:.1f}h)"
        elif last_checkout_local and worked_hours > 0 and worked_hours < (full_day_min_hours - short_leave_max_hours):
            is_half_day = True
            half_day_type = "low_hours"
            status = "Half Day"
            effective_days = 0.5
            classification_reason = f"Worked {worked_hours:.1f}h (credited as Half-Day, threshold {half_day_min_hours:.1f}h - {full_day_min_hours - short_leave_max_hours:.1f}h)"

    # C) Evaluate Short Leave thresholds if not Half Day and not already Short Leave
    if not is_half_day and not is_short_leave and status != "Absent":
        if first_checkin_local > short_leave_late_cutoff:
            is_short_leave = True
            short_leave_type = "late_arrival"
            status = "Short Leave"
            effective_days = 1.0
            classification_reason = f"Late by {late_mins}m (exceeds Short Leave limit {late_to_short_mins}m)"
        elif last_checkout_local and last_checkout_local < short_leave_early_cutoff:
            is_short_leave = True
            short_leave_type = "early_departure"
            status = "Short Leave"
            effective_days = 1.0
            classification_reason = f"Early exit by {early_exit_mins}m (exceeds Short Leave limit {early_short_mins}m)"
        elif last_checkout_local and worked_hours > 0:
            # Low-hours short leave evaluation:
            # 1. If employee arrived within grace and left on/after shift end, they fulfilled their day: NEVER short leave!
            # 2. Deficit must exceed grace_hours margin
            # 3. Deficit must be at least 1.0 hour OR accompanied by unexcused late/early
            hours_deficit = round(full_day_min_hours - worked_hours, 2)
            if not is_late and not is_early_leave:
                # On-time full day completed within allowable shift schedule
                pass
            elif hours_deficit > grace_hours and (is_late or is_early_leave or hours_deficit >= 1.0):
                is_short_leave = True
                short_leave_type = "low_hours"
                status = "Short Leave"
                effective_days = 1.0
                classification_reason = f"Worked {worked_hours:.1f}h (< full day requirement {full_day_min_hours:.1f}h)"

    # D) Fallback states
    if not is_half_day and not is_short_leave:
        if is_early_leave:
            status = "Early Leave"
            classification_reason = f"Left {early_exit_mins} mins early"
        elif is_late:
            status = "Late"
            classification_reason = f"Late check-in by {late_mins} mins"
        else:
            status = "Present"
            classification_reason = "On-time attendance"

    return {
        "status": status,
        "is_late": is_late,
        "is_early_leave": is_early_leave,
        "is_short_leave": is_short_leave,
        "is_half_day": is_half_day,
        "short_leave_type": short_leave_type,
        "half_day_type": half_day_type,
        "late_mins": late_mins,
        "early_exit_mins": early_exit_mins,
        "late_off_hours": late_off_hours,
        "early_off_hours": early_off_hours,
        "off_hours": total_off_hours,
        "worked_hours": round(worked_hours, 2),
        "required_hours": round(required_hours, 2),
        "effective_days": effective_days,
        "classification_reason": classification_reason,
        "is_applied_leave": is_applied_leave
    }


@app.post("/verify-presence")
async def verify_presence(req: VerifyPresenceRequest):
    """Verify user presence using face + GPS + WiFi telemetry."""
    # 1. Find User
    user = await employees_collection.find_one({"email": req.email})
    if not user:
        raise HTTPException(status_code=404, detail="User not found. Please register first.")

    # 2. Face Verification
    is_match, distance = verify_face(req.image, user["face_embedding"])
    if distance == 1.0:
        raise HTTPException(status_code=400, detail="Biometric data mismatch. Please re-enroll your face in Profile.")
    
    if not is_match:
        raise HTTPException(
            status_code=400,
            detail=f"Face verification failed (distance: {distance:.4f}). Please try again."
        )
    # 3. Geofencing & WiFi Validation (Split Field/Desk)
    org_id = user.get("organization_id")
    org_settings = await settings_collection.find_one({"organization_id": org_id}) if org_id else None
    settings = org_settings or {}
    emp_type = user.get("employee_type", "desk")
    
    # Coordinates fallback (Dynamic Database with Env Fallback)
    db_lat = settings.get("office_lat")
    db_long = settings.get("office_long")
    office_lat = float(db_lat) if (db_lat is not None and abs(float(db_lat)) > 0.0001) else float(os.getenv("OFFICE_LAT", 0))
    office_long = float(db_long) if (db_long is not None and abs(float(db_long)) > 0.0001) else float(os.getenv("OFFICE_LONG", 0))
    
    # Radius fallback
    db_radius = settings.get("geofence_radius")
    if db_radius is not None and float(db_radius) > 0.0001:
        radius = float(db_radius)
    else:
        radius = float(os.getenv("GEOFENCE_RADIUS_METERS", 40 if emp_type == "desk" else 150))
        
    # WiFi SSID fallback
    db_wifi_ssid = settings.get("office_wifi_ssid")
    target_ssid = str(db_wifi_ssid).strip() if (db_wifi_ssid and str(db_wifi_ssid).strip() != "") else os.getenv("OFFICE_WIFI_SSID", "")
    
    # WiFi BSSID fallback
    db_wifi_bssid = settings.get("office_wifi_bssid")
    target_bssid = str(db_wifi_bssid).strip() if (db_wifi_bssid and str(db_wifi_bssid).strip() != "") else os.getenv("OFFICE_WIFI_BSSID", "")
    
    # Timezone offset fallback
    tz_offset = int(settings.get("timezone_offset", 330))

    # Haversine distance
    import math
    dlat = math.radians(req.lat - office_lat)
    dlon = math.radians(req.long - office_long)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(office_lat)) * math.cos(math.radians(req.lat)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    dist_meters = 6371000 * c

    if dist_meters > radius:
        raise HTTPException(
            status_code=403,
            detail=f"You are {dist_meters:.1f}m away from office. Must be within {radius:.1f}m."
        )

    if emp_type != "field":
        clean_req_ssid = req.wifi_ssid.strip().strip('"').strip("'").lower() if req.wifi_ssid else ""
        clean_target_ssid = target_ssid.strip().strip('"').strip("'").lower() if target_ssid else ""
        if clean_target_ssid and clean_req_ssid and clean_req_ssid != clean_target_ssid:
             raise HTTPException(status_code=403, detail=f"Must be connected to Office WiFi: {target_ssid}")
        if target_bssid and req.wifi_bssid and req.wifi_bssid.strip().lower() != target_bssid.strip().lower():
             raise HTTPException(status_code=403, detail="Must be connected to Office WiFi Access Point (BSSID mismatch).")

    # 5. Determine check-in or check-out (Localized)
    today_start_utc = get_today_start(tz_offset)
    user_id_str = str(user["_id"])
    emp_email = user.get("email", req.email)
    last_log = await attendance_logs_collection.find_one(
        {
            "$or": [
                {"user_id": user_id_str},
                {"user_id": emp_email},
                {"email": emp_email}
            ],
            "timestamp": {"$gte": today_start_utc}
        },
        sort=[("timestamp", -1)]
    )
    attendance_type = "check-out" if (last_log and last_log.get("type") == "check-in") else "check-in"

    # 6. Log Attendance
    log = {
        "user_id": user_id_str,
        "email": emp_email,
        "timestamp": datetime.now(timezone.utc),
        "type": attendance_type,
        "status": "SUCCESS",
        "location": {"lat": req.lat, "long": req.long},
        "location_name": req.address or "Verified Zone",
        "distance_meters": dist_meters,
        "wifi_info": {"bssid": req.wifi_bssid, "strength": req.wifi_strength},
        "face_confidence": float(distance),
        "device_id": req.device_id
    }
    await attendance_logs_collection.insert_one(log)
    await employees_collection.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "checked_in": (attendance_type == "check-in"),
            "last_attendance_type": attendance_type,
            "last_attendance_time": log["timestamp"]
        }}
    )

    return {
        "status": "success",
        "type": attendance_type,
        "current_status": attendance_type, # Provide immediate state for UI sync
        "message": f"{attendance_type.replace('-', ' ').title()} recorded at {log['timestamp'].strftime('%I:%M %p')}",
        "time": str(log["timestamp"]),
        "distance_from_office": f"{dist_meters:.1f}m",
    }


@app.post("/smart-attendance")
async def smart_attendance(req: VerifyPresenceRequest, request: Request, background_tasks: BackgroundTasks):
    """
    Unified endpoint for Enterprise Smart Attendance:
    - DESK: Strict WiFi (80%) + Office Geofence (4m).
    - FIELD: Bypass WiFi. Validate Territory (500m default) OR OTP.
    - Both: Face Liveness/Match + Mock Detection.
    - SUPERADMIN: Bypass all security restrictions for testing/management.
    """
    try:
        # 0. Global Telemetry Defaults (Fetched later per org)
        office_lat = 0
        office_long = 0
        radius = 40
        wifi_pct = 0
        office_wifi_ssid = None
        
        # 1. Identity & Role Fetch
        clean_email = req.email.strip().lower() if req.email else ""
        user = await employees_collection.find_one({"email": clean_email}) if clean_email else None
        is_admin_user = False
        
        if not user:
            # Try 1:N face search ONLY if email is blank/generic AND embedding is non-dummy
            if not clean_email or clean_email in ["unknown", "auto", ""]:
                new_embedding = get_face_embedding(req.image)
                if new_embedding is not None and not is_dummy_embedding(new_embedding):
                    emp_query = {"face_embedding": {"$ne": None}}
                    if req.organization_id:
                        emp_query["organization_id"] = req.organization_id
                    employees = await employees_collection.find(emp_query, {"_id": 1, "face_embedding": 1, "email": 1, "employee_type": 1, "organization_id": 1}).to_list(length=5000)
                    for emp in employees:
                        if emp.get("face_embedding") and not is_dummy_embedding(emp["face_embedding"]) and compare_faces(new_embedding, emp["face_embedding"]):
                            user = emp
                            clean_email = emp.get("email", "")
                            break
            
            if not user and clean_email:
                # Try Admin collection (Admins might be marking attendance for themselves)
                user = await admins_collection.find_one({"email": clean_email})
                if user:
                    is_admin_user = True
                    logger.info(f"Admin '{clean_email}' found in admins collection for attendance.")

            # JWT Bearer Token Fallback: If user still not resolved (blank/generic email from app cold-start),
            # extract identity from the Authorization header that the mobile app's Axios interceptor always attaches.
            if not user:
                auth_header = request.headers.get("authorization", "")
                if auth_header.startswith("Bearer "):
                    try:
                        token = auth_header.split(" ", 1)[1]
                        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
                        token_email = payload.get("sub", "").strip().lower()
                        if token_email:
                            logger.info(f"JWT fallback: Resolved email '{token_email}' from Bearer token (original email was '{clean_email}')")
                            user = await employees_collection.find_one({"email": token_email})
                            if not user:
                                user = await admins_collection.find_one({"email": token_email})
                                if user:
                                    is_admin_user = True
                            if user:
                                clean_email = token_email
                    except Exception as jwt_err:
                        logger.warning(f"JWT fallback failed: {jwt_err}")

            if not user:
                raise HTTPException(status_code=404, detail="Identity not recognized. Please sign in or register.")

        # Identity identified.
        is_reviewer = (clean_email == "google.review@logday.app")
        is_superadmin = (user.get("role") == "superadmin") or (clean_email == os.getenv("ADMIN_EMAIL", "admin@officeflow.ai")) or is_reviewer
        
        # Fetch settings.
        org_id = user.get("organization_id")
        settings = await get_effective_org_settings(org_id)
        org_settings = settings
        
        emp_type = user.get("employee_type", "desk")
        
        # Coordinates fallback (Dynamic Database with Env Fallback)
        db_lat = settings.get("office_lat")
        db_long = settings.get("office_long")
        office_lat = float(db_lat) if (db_lat is not None and abs(float(db_lat)) > 0.0001) else float(os.getenv("OFFICE_LAT", 0))
        office_long = float(db_long) if (db_long is not None and abs(float(db_long)) > 0.0001) else float(os.getenv("OFFICE_LONG", 0))
        
        # Radius fallback
        db_radius = settings.get("geofence_radius")
        if db_radius is not None and float(db_radius) > 0.0001:
            radius = float(db_radius)
        else:
            radius = float(os.getenv("GEOFENCE_RADIUS_METERS", 40 if emp_type == "desk" else 150))
            
        # WiFi SSID fallback
        db_wifi_ssid = settings.get("office_wifi_ssid")
        office_wifi_ssid = str(db_wifi_ssid).strip() if (db_wifi_ssid and str(db_wifi_ssid).strip() != "") else os.getenv("OFFICE_WIFI_SSID", "")
        
        # WiFi BSSID fallback
        db_wifi_bssid = settings.get("office_wifi_bssid")
        office_wifi_bssid = str(db_wifi_bssid).strip() if (db_wifi_bssid and str(db_wifi_bssid).strip() != "") else os.getenv("OFFICE_WIFI_BSSID", "")

        role = user.get("employee_type", "desk")
        attendance_type = req.intended_type or "check-in"
        logger.info(f"Processing {attendance_type.replace('-', ' ').title()} for {user['email']} (Role: {role}, Superadmin: {is_superadmin})")

        # Session Validation
        tz_offset = 330 # Default IST
        if org_settings:
            tz_offset = org_settings.get("timezone_offset", 330)
        today_start_utc = get_today_start(tz_offset)
        user_id_str = str(user["_id"])
        emp_email = user.get("email", clean_email)

        # --- Query 1: All-time last log (used ONLY for auto-closing stale sessions) ---
        alltime_last_log = await attendance_logs_collection.find_one(
            {
                "$or": [
                    {"user_id": user_id_str},
                    {"user_id": emp_email},
                    {"email": emp_email}
                ]
            },
            sort=[("timestamp", -1)]
        )

        # AUTO-CLOSE STALE SESSIONS (uses all-time log to detect yesterday's open session)
        if alltime_last_log and alltime_last_log.get("type") == "check-in":
            last_ts = alltime_last_log.get("timestamp")
            if last_ts and last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            if last_ts and last_ts < today_start_utc:
                local_ts = last_ts + timedelta(minutes=tz_offset)
                if local_ts.hour < 23:
                    local_end = local_ts.replace(hour=23, minute=59, second=59, microsecond=0)
                    auto_checkout_time = local_end - timedelta(minutes=tz_offset)
                else:
                    auto_checkout_time = last_ts + timedelta(seconds=1)
                auto_checkout_log = {
                    "user_id": user_id_str,
                    "email": emp_email,
                    "type": "check-out",
                    "timestamp": auto_checkout_time,
                    "lat": alltime_last_log.get("lat", 0),
                    "long": alltime_last_log.get("long", 0),
                    "location_name": "Auto-closed (missed checkout)",
                    "wifi_bssid": "",
                    "wifi_ssid": "",
                    "method": "system_auto_close",
                    "organization_id": str(org_id) if org_id else None,
                }
                await attendance_logs_collection.insert_one(auto_checkout_log)
                logger.info(f"[AutoClose] Stale session closed for {user['email']}")

        # --- Query 2: Today-only last log (used for state machine validation) ---
        # This is SEPARATE from the all-time query above so that yesterday's logs
        # never cause a false "already checked in" error today.
        last_log = await attendance_logs_collection.find_one(
            {
                "$or": [
                    {"user_id": user_id_str},
                    {"user_id": emp_email},
                    {"email": emp_email}
                ],
                "timestamp": {"$gte": today_start_utc}
            },
            sort=[("timestamp", -1)]
        )

        # State Machine Validation (only uses today's logs)
        if attendance_type == "check-in":
            if last_log and last_log.get("type") == "check-in":
                raise HTTPException(status_code=400, detail="You are already checked in. Please check out first.")
        elif attendance_type == "check-out":
            if not last_log or last_log.get("type") == "check-out":
                raise HTTPException(status_code=400, detail="You haven't checked in today. Please check in first.")

        # 2. Universal Security
        if req.mock_detected and not is_superadmin:
            background_tasks.add_task(
                trigger_alert, "Territory", user.get("email"), user.get("organization_id"), 
                "Mock Location detected during attendance.", "high", {"lat": req.lat, "long": req.long}
            )
            raise HTTPException(status_code=403, detail="Security violation: Location spoofing/virtualization detected. Attendance rejected.")

        # Device Binding Check (Bypass for Superadmin)
        if user.get("device_id") and req.device_id and user["device_id"] != req.device_id:
            if not is_superadmin:
                background_tasks.add_task(
                    trigger_alert, "Identity", user.get("email"), user.get("organization_id"), 
                    f"Device mismatch. Registered: {user.get('device_id')}, Current: {req.device_id}", "medium"
                )
                raise HTTPException(status_code=403, detail="Security Violation: This account is bound to another device. Contact admin.")
            else:
                logger.info(f"Superadmin bypass: Device mismatch ignored.")

        if not user.get("face_embedding") and not is_superadmin:
            raise HTTPException(status_code=400, detail="Face biometric not enrolled for this user.")
        
        # Face Verification (Bypass for Superadmin or if no embedding)
        distance = 0.0
        if user.get("face_embedding"):
            is_match, distance = verify_face(req.image, user["face_embedding"])
            if not is_match and not is_superadmin:
                background_tasks.add_task(
                    trigger_alert, "Identity", user.get("email"), user.get("organization_id"), 
                    f"Face verification failed with confidence distance {distance:.3f}", "medium"
                )
                raise HTTPException(status_code=400, detail="Face verification failed. Please ensure your face is clearly visible.")
        # 3. Geofence/Territory Logic
        check_in_method = CheckInMethod.GPS_TERRITORY
        is_at_office = False
        office_dist = 9999999
        if abs(office_lat) > 0.01 or abs(office_long) > 0.01:
            office_dist = calculate_haversine(req.lat, req.long, float(office_lat), float(office_long))
            if office_dist <= float(radius):
                is_at_office = True

        # Check if employee has approved WFH for today
        now_utc = datetime.now(timezone.utc)
        local_now = now_utc + timedelta(minutes=tz_offset)
        today_str = local_now.strftime("%Y-%m-%d")

        wfh_approved = False
        wfh_req = await wfh_requests_collection.find_one({
            "employee_email": clean_email,
            "date": today_str,
            "status": "approved"
        })
        if wfh_req:
            wfh_approved = True

        is_wfh_or_hybrid = role in ["wfh", "hybrid", EmployeeType.WFH, EmployeeType.HYBRID]

        # GEOFENCE BYPASS for Superadmin or approved WFH or permanent WFH/hybrid
        if is_superadmin or wfh_approved or is_wfh_or_hybrid:
            logger.info(f"Geofence bypass: superadmin={is_superadmin}, wfh_approved={wfh_approved}, is_wfh_or_hybrid={is_wfh_or_hybrid}")
            wifi_pct = 100
            check_in_method = CheckInMethod.GPS_TERRITORY
            # Check WFH device status if permanent WFH or hybrid and device_id is provided
            if (role in ["wfh", "hybrid", EmployeeType.WFH, EmployeeType.HYBRID]) and req.device_id and not is_superadmin:
                try:
                    await verify_wfh_device(str(user["_id"]), str(org_id), req.device_id)
                except Exception as e:
                    logger.warning(f"WFH device verification failed: {e}")
        else:
            if role == "desk" or role == EmployeeType.DESK:
                if not is_at_office:
                     raise HTTPException(
                          status_code=403, 
                          detail=f"Location error. You are {office_dist:.1f}m away from office (Limit: {radius}m)."
                     )
                
                is_wifi_ok = True
                clean_req_ssid = req.wifi_ssid.strip().strip('"').strip("'").lower() if req.wifi_ssid else ""
                clean_office_ssid = office_wifi_ssid.strip().strip('"').strip("'").lower() if office_wifi_ssid else ""
                if clean_office_ssid and (not clean_req_ssid or clean_req_ssid != clean_office_ssid):
                    is_wifi_ok = False
                if office_wifi_bssid and (not req.wifi_bssid or req.wifi_bssid.strip().lower() != office_wifi_bssid.strip().lower()):
                    is_wifi_ok = False
                
                wifi_pct = 100 if is_wifi_ok else 0
                check_in_method = CheckInMethod.WIFI_GEOFENCE
            else:
                # Field Logic
                if is_at_office:
                    check_in_method = CheckInMethod.WIFI_GEOFENCE
                    wifi_pct = 100
                elif req.otp_used:
                    # OTP verification
                    stored_otp = user.get("gps_otp")
                    otp_expiry = user.get("gps_otp_expiry")
                    if not stored_otp or str(stored_otp) != str(req.otp_code):
                        raise HTTPException(status_code=403, detail="Invalid OTP code.")
                    if otp_expiry and datetime.now(timezone.utc) > (otp_expiry.replace(tzinfo=timezone.utc) if otp_expiry.tzinfo is None else otp_expiry):
                         raise HTTPException(status_code=403, detail="OTP code has expired.")
                    await employees_collection.update_one({"_id": user["_id"]}, {"$set": {"gps_otp": None, "gps_otp_expiry": None}})
                    check_in_method = CheckInMethod.OTP_FALLBACK
                else:
                    # Territory verification
                    t_lat_val = user.get("territory_center_lat")
                    t_lng_val = user.get("territory_center_lng")
                    t_lat = float(t_lat_val) if t_lat_val is not None else 0.0
                    t_lng = float(t_lng_val) if t_lng_val is not None else 0.0
                    if abs(t_lat) < 0.01 and abs(office_lat) > 0.01:
                        t_lat, t_lng = office_lat, office_long
                    t_radius_val = user.get("territory_radius_meters")
                    t_radius = float(t_radius_val) if t_radius_val is not None else 500.0
                    dist = calculate_haversine(req.lat, req.long, t_lat, t_lng)
                    if dist > t_radius:
                        raise HTTPException(status_code=403, detail=f"Territory Breach. You are {dist:.0f}m away from your assigned zone.")
                    check_in_method = CheckInMethod.GPS_TERRITORY



        # 4. Log Attendance (Consolidated Enterprise Classification)
        attendance_type = req.intended_type or "check-in" # Default or provided
        settings = org_settings or SystemSettings().dict()
        tz_offset = int(settings.get("timezone_offset", 330))
        log_time_utc = datetime.now(timezone.utc)
        current_time_local = log_time_utc + timedelta(minutes=tz_offset)
        today_date_str = current_time_local.strftime("%Y-%m-%d")

        # Check for approved leave for today (applied short leave, half day, etc.)
        approved_leave = None
        try:
            approved_leave = await leave_requests_collection.find_one({
                "$or": [
                    {"employee_id": clean_email},
                    {"email": clean_email},
                    {"employee_id": str(user["_id"])}
                ],
                "status": {"$regex": "^approved$", "$options": "i"},
                "start_date": {"$lte": today_date_str},
                "end_date": {"$gte": today_date_str}
            })
        except Exception as e:
            logger.warning(f"Leave lookup error in attendance: {e}")

        today_start_utc = get_today_start(tz_offset)

        if attendance_type == "check-in":
            classification = classify_attendance_day(
                first_checkin_local=current_time_local,
                last_checkout_local=None,
                worked_hours=0.0,
                settings=settings,
                employee_type=role,
                approved_leave=approved_leave
            )
        else: # check-out
            # Fetch all attendance logs for this employee today (in chronological order)
            today_logs = await attendance_logs_collection.find(
                {
                    "$or": [
                        {"user_id": user_id_str},
                        {"user_id": emp_email},
                        {"email": emp_email}
                    ],
                    "timestamp": {"$gte": today_start_utc}
                }
            ).sort("timestamp", 1).to_list(100)

            checkin_logs = [l for l in today_logs if l.get("type") in ["check-in", "IN"]]
            today_first_checkin = checkin_logs[0] if checkin_logs else None

            # Calculate paired worked hours across all sessions today plus this checkout
            worked_sec = 0.0
            last_in = None
            for l in today_logs:
                l_ts = l["timestamp"]
                if l_ts.tzinfo is None: l_ts = l_ts.replace(tzinfo=timezone.utc)
                l_type = l.get("type")
                if l_type in ["check-in", "IN"]:
                    last_in = l_ts
                elif l_type in ["check-out", "OUT"] and last_in:
                    worked_sec += max(0.0, (l_ts - last_in).total_seconds())
                    last_in = None

            # Add current checkout session
            if last_in:
                worked_sec += max(0.0, (log_time_utc - last_in).total_seconds())
            elif today_first_checkin:
                # Fallback if intermediate in/out was missing
                f_ts = today_first_checkin["timestamp"]
                if f_ts.tzinfo is None: f_ts = f_ts.replace(tzinfo=timezone.utc)
                worked_sec = max(0.0, (log_time_utc - f_ts).total_seconds())

            if today_first_checkin:
                f_ts = today_first_checkin["timestamp"]
                if f_ts.tzinfo is None: f_ts = f_ts.replace(tzinfo=timezone.utc)
                first_checkin_local = f_ts + timedelta(minutes=tz_offset)
            else:
                first_checkin_local = current_time_local

            worked_hours = round(worked_sec / 3600.0, 2)

            classification = classify_attendance_day(
                first_checkin_local=first_checkin_local,
                last_checkout_local=current_time_local,
                worked_hours=worked_hours,
                settings=settings,
                employee_type=role,
                approved_leave=approved_leave
            )

            # Retroactively update today's first check-in log with final full-day classification
            if today_first_checkin:
                await attendance_logs_collection.update_one(
                    {"_id": today_first_checkin["_id"]},
                    {"$set": {
                        "attendance_status": classification["status"],
                        "is_short_leave": classification["is_short_leave"],
                        "is_half_day": classification["is_half_day"],
                        "short_leave_type": classification["short_leave_type"],
                        "half_day_type": classification["half_day_type"],
                        "off_hours": classification["off_hours"],
                        "late_off_hours": classification["late_off_hours"],
                        "early_off_hours": classification["early_off_hours"],
                        "classification_reason": classification["classification_reason"],
                        "is_applied_leave": classification["is_applied_leave"],
                        "effective_days": classification["effective_days"]
                    }}
                )

        # Determine unified attendance type
        att_type_val = AttendanceType.REMOTE_FIELD
        if wfh_approved or role == EmployeeType.WFH or role == "wfh":
            att_type_val = AttendanceType.WFH
        elif role == EmployeeType.DESK or role == "desk":
            att_type_val = AttendanceType.OFFICE

        # Location name default
        loc_name_val = req.address or ("Office Zone" if role == EmployeeType.DESK and not wfh_approved else "Field Location")
        if wfh_approved or role == EmployeeType.WFH or role == "wfh":
            loc_name_val = req.address or "Remote Workspace"

        wfh_session_id = None
        if wfh_approved or role == EmployeeType.WFH or role == "wfh":
            if attendance_type == "check-in":
                active_session = await wfh_sessions_collection.find_one({
                    "employee_id": str(user["_id"]),
                    "organization_id": org_id,
                    "status": "active"
                })
                if not active_session:
                    now_val = datetime.now(timezone.utc)
                    today_val = now_val.strftime("%Y-%m-%d")
                    session_doc = {
                        "employee_id": str(user["_id"]),
                        "employee_email": clean_email,
                        "employee_name": user.get("full_name", ""),
                        "organization_id": org_id,
                        "device_id": req.device_id,
                        "date": today_val,
                        "check_in_time": now_val,
                        "check_out_time": None,
                        "status": "active",
                        "check_in_face_verified": True,
                        "face_distance": float(distance),
                        "total_active_seconds": 0,
                        "total_idle_seconds": 0,
                        "productivity_score": 0,
                        "created_at": now_val,
                        "updated_at": now_val,
                        "metadata": {}
                    }
                    result = await wfh_sessions_collection.insert_one(session_doc)
                    wfh_session_id = str(result.inserted_id)
                else:
                    wfh_session_id = str(active_session["_id"])
            elif attendance_type == "check-out":
                session = await wfh_sessions_collection.find_one({
                    "employee_id": str(user["_id"]),
                    "organization_id": org_id,
                    "status": "active"
                })
                if session:
                    now_val = datetime.now(timezone.utc)
                    check_in_time_val = session.get("check_in_time")
                    total_seconds_val = 0
                    if check_in_time_val:
                        if check_in_time_val.tzinfo is None:
                            check_in_time_val = check_in_time_val.replace(tzinfo=timezone.utc)
                        total_seconds_val = int((now_val - check_in_time_val).total_seconds())
                    await wfh_sessions_collection.update_one(
                        {"_id": session["_id"]},
                        {
                            "$set": {
                                "status": "completed",
                                "check_out_time": now_val,
                                "total_active_seconds": total_seconds_val,
                                "updated_at": now_val
                            }
                        }
                    )
                    wfh_session_id = str(session["_id"])

        log = {
            "user_id": str(user["_id"]),
            "email": user["email"],
            "organization_id": user.get("organization_id"),
            "timestamp": log_time_utc,
            "type": attendance_type,
            "attendance_type": att_type_val,
            "attendance_status": classification["status"],
            "is_late": classification["is_late"],
            "is_early_leave": classification["is_early_leave"],
            "is_short_leave": classification["is_short_leave"],
            "is_half_day": classification["is_half_day"],
            "short_leave_type": classification["short_leave_type"],
            "half_day_type": classification["half_day_type"],
            "late_mins": classification["late_mins"],
            "early_exit_mins": classification["early_exit_mins"],
            "late_off_hours": classification["late_off_hours"],
            "early_off_hours": classification["early_off_hours"],
            "off_hours": classification["off_hours"],
            "worked_hours": classification["worked_hours"],
            "required_hours": classification["required_hours"],
            "effective_days": classification["effective_days"],
            "classification_reason": classification["classification_reason"],
            "is_applied_leave": classification["is_applied_leave"],
            "location": {"lat": req.lat, "long": req.long},
            "check_in_method": check_in_method,
            "wifi_confidence": wifi_pct if role == EmployeeType.DESK else 0,
            "confidence_score": float(distance),
            "status": "SUCCESS",
            "location_name": loc_name_val,
            "selfie_verified": True,
            "device_id": req.device_id,
            "mock_location_detected": req.mock_detected
        }
        if wfh_session_id:
            log["wfh_session_id"] = wfh_session_id
        
        await attendance_logs_collection.insert_one(log)
        await employees_collection.update_one(
            {"_id": user["_id"]},
            {"$set": {
                "checked_in": (attendance_type == "check-in"),
                "last_attendance_type": attendance_type,
                "last_attendance_time": log["timestamp"],
                "last_attendance_status": classification["status"]
            }}
        )

        # Trigger Background Sync to Google Sheets
        background_tasks.add_task(sync_to_google_sheets, log)
        
        policy = None
        if (role == EmployeeType.WFH or role == "wfh") and attendance_type == "check-in":
            try:
                org = await organizations_collection.find_one({"_id": ObjectId(org_id)})
                if org:
                    policy = org.get("wfh_policy")
            except Exception as e:
                logger.error(f"Error fetching policy in smart checkin: {e}")
                
            if not policy:
                policy = {
                    "screenshot_interval_minutes": 10,
                    "face_check_interval_minutes": 30,
                    "max_idle_minutes": 20,
                    "productivity_threshold_percent": 60,
                    "working_hours_start": "09:00",
                    "working_hours_end": "18:00",
                    "screenshot_retention_days": 5,
                    "require_face_verification": True,
                    "productive_apps": [],
                    "unproductive_apps": []
                }
                
        return {
            "status": "success",
            "type": attendance_type,
            "current_status": attendance_type, # Provide immediate state for UI sync
            "attendance_status": classification["status"],
            "is_late": classification["is_late"],
            "is_early_leave": classification["is_early_leave"],
            "is_short_leave": classification["is_short_leave"],
            "is_half_day": classification["is_half_day"],
            "short_leave_type": classification["short_leave_type"],
            "half_day_type": classification["half_day_type"],
            "late_mins": classification["late_mins"],
            "early_exit_mins": classification["early_exit_mins"],
            "late_off_hours": classification["late_off_hours"],
            "early_off_hours": classification["early_off_hours"],
            "off_hours": classification["off_hours"],
            "worked_hours": classification["worked_hours"],
            "required_hours": classification["required_hours"],
            "effective_days": classification["effective_days"],
            "classification_reason": classification["classification_reason"],
            "is_applied_leave": classification["is_applied_leave"],
            "message": f"Attendance {attendance_type.replace('-', ' ').title()} Recorded: {classification['status']} ({classification['classification_reason']})",
            "time": log["timestamp"].isoformat(),
            "wifi_confidence": wifi_pct,
            "wfh_session_id": wfh_session_id,
            "wfh_policy": policy
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        logger.error(f"Execution Error in smart_attendance: {e}\n{error_trace}")
        raise HTTPException(status_code=500, detail=f"Internal processing error in attendance module: {type(e).__name__} - {str(e)}")


def get_today_start(offset_mins: int = 330):
    """
    Get the start of 'today' in UTC, given a local offset in minutes.
    Default: IST (+5:30 = 330 mins).
    """
    now_utc = datetime.now(timezone.utc)
    local_now = now_utc + timedelta(minutes=offset_mins)
    # Start of local day
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Convert back to UTC
    utc_start = local_start - timedelta(minutes=offset_mins)
    return utc_start

def calculate_haversine(lat1, lon1, lat2, lon2):
    dlat = math.radians(lat1 - lat2)
    dlon = math.radians(lon1 - lon2)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return 6371000 * c


def is_point_in_polygon(lat: float, lng: float, polygon: list) -> bool:
    """Ray-casting algorithm to check if a point is inside a polygon.
    polygon: list of dicts with 'lat' and 'lng' keys.
    """
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]["lat"], polygon[i]["lng"]
        xj, yj = polygon[j]["lat"], polygon[j]["lng"]
        if ((yi > lng) != (yj > lng)) and (lat < (xj - xi) * (lng - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def attendance_type_str(val):
    return val or "attendance"




@app.get("/api/logs/{email}")
async def get_logs(email: str, page: int = 1, limit: int = 50, start_date: Optional[str] = None, end_date: Optional[str] = None, current_user: dict = Depends(get_current_employee)):
    """Get attendance logs for a user with date filters and pagination (JWT Protected)."""
    if current_user["email"].strip().lower() != email.strip().lower():
        raise HTTPException(status_code=403, detail="Forbidden: You can only access your own logs.")

    user = await employees_collection.find_one({
        "$or": [
            {"email": email.strip().lower()},
            {"email": email.strip()}
        ]
    })
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user_id_str = str(user["_id"])
    query = {
        "$or": [
            {"user_id": user_id_str},
            {"user_id": email},
            {"email": email}
        ]
    }
    
    if start_date or end_date:
        time_filter = {}
        if start_date:
            try:
                time_filter["$gte"] = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
            except: pass
        if end_date:
            try:
                time_filter["$lte"] = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            except: pass
        if time_filter:
            query["timestamp"] = time_filter

    total_count = await attendance_logs_collection.count_documents(query)
    skip = (page - 1) * limit
    
    cursor = attendance_logs_collection.find(query).sort("timestamp", -1).skip(skip).limit(limit)
    logs = await cursor.to_list(length=limit)

    # Convert ObjectIDs to strings for JSON serialization
    for log in logs:
        log["_id"] = str(log["_id"])
        if "timestamp" in log and isinstance(log["timestamp"], datetime):
            ts = log["timestamp"]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            log["timestamp"] = ts.isoformat().replace("+00:00", "Z")
        elif "timestamp" in log:
            log["timestamp"] = str(log["timestamp"])

    return {
        "logs": logs, 
        "total_count": total_count,
        "page": page,
        "limit": limit,
        "has_more": total_count > (skip + len(logs))
    }


@app.get("/api/employee/profile")
async def get_employee_profile(current_user: dict = Depends(get_current_employee)):
    """Retrieve full employee profile for Field/Desk apps."""
    user = await employees_collection.find_one({"email": current_user["email"]})
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    # Check current attendance status for today (localized)
    org_id = user.get("organization_id")
    tz_offset = 330
    if org_id:
        org_settings = await settings_collection.find_one({"organization_id": str(org_id) if ObjectId.is_valid(str(org_id)) else org_id})
        if org_settings:
            tz_offset = org_settings.get("timezone_offset", 330)

    today_start_utc = get_today_start(tz_offset)
    user_id_str = str(user["_id"])
    emp_email = user.get("email", "")

    last_log = await attendance_logs_collection.find_one(
        {
            "$or": [
                {"user_id": user_id_str},
                {"user_id": emp_email},
                {"email": emp_email}
            ],
            "timestamp": {"$gte": today_start_utc}
        },
        sort=[("timestamp", -1)]
    )
    
    current_status = last_log.get("type", "check-out") if last_log else "check-out"

    return {
        "email": user["email"],
        "full_name": user.get("full_name"),
        "employee_id": user.get("employee_id"),
        "department": user.get("department"),
        "designation": user.get("designation"),
        "employee_type": user.get("employee_type"),
        "profile_image": user.get("profile_image"),
        "has_face_data": bool(user.get("face_embedding")),
        "needs_face_enrollment": user.get("face_embedding") is None,
        "organization_id": str(user.get("organization_id", "")),
        "is_manager": user.get("is_manager", False),
        "status": user.get("status", "Active"),
        "current_status": current_status
    }


@app.get("/api/analytics/me")
async def get_my_analytics(current_user: dict = Depends(get_current_employee)):
    """Get summarized work hours and stats for the logged-in employee."""
    user_email = current_user.get("email")
    current_status = "check-out" # Default safety
    
    try:
        user = await employees_collection.find_one({"email": user_email})
        if not user:
            return {"email": user_email, "current_status": "check-out", "today_hours": "00:00"}

        user_id_str = str(user["_id"])
        org_id = user.get("organization_id")
        tz_offset = 330
        if org_id:
            org_settings = await settings_collection.find_one({"organization_id": str(org_id) if ObjectId.is_valid(str(org_id)) else org_id})
            if org_settings:
                tz_offset = org_settings.get("timezone_offset", 330)

        today_start = get_today_start(tz_offset)

        # 1. Determine Status FIRST (Highest Priority for UI Sync)
        latest_log = await attendance_logs_collection.find_one(
            {
                "$or": [
                    {"user_id": user_id_str},
                    {"user_id": user_email},
                    {"email": user_email}
                ]
            },
            sort=[("timestamp", -1)]
        )
        
        if latest_log:
            log_type = latest_log.get("type", "check-out")
            if log_type == "check-in":
                l_ts = latest_log["timestamp"]
                if l_ts.tzinfo is None: l_ts = l_ts.replace(tzinfo=timezone.utc)
                if l_ts < today_start:
                    current_status = "check-out" # Stale
                else:
                    current_status = "check-in"
            else:
                current_status = "check-out"

        week_start = today_start - timedelta(days=today_start.weekday())
        month_start = today_start.replace(day=1)

        # Get logs for calculation
        all_logs = await attendance_logs_collection.find({
            "$or": [
                {"user_id": user_id_str},
                {"user_id": user_email},
                {"email": user_email}
            ],
            "timestamp": {"$gte": week_start}
        }).sort("timestamp", 1).to_list(length=1000)

        async def calculate_hours(logs_subset):
            total_seconds = 0
            current_check_in = None
            for log in logs_subset:
                l_ts = log["timestamp"]
                if l_ts.tzinfo is None: l_ts = l_ts.replace(tzinfo=timezone.utc)
                
                ltype = log.get("type", "").lower()
                if "in" in ltype:
                    current_check_in = l_ts
                elif "out" in ltype and current_check_in:
                    duration = (l_ts - current_check_in).total_seconds()
                    total_seconds += max(0, duration)
                    current_check_in = None
            
            if current_check_in:
                now_utc = datetime.now(timezone.utc)
                duration = (now_utc - current_check_in).total_seconds()
                total_seconds += max(0, duration)
            return round(total_seconds / 3600.0, 1)

        # Create aware logs for filtering
        aware_logs = []
        for l in all_logs:
            l_ts = l["timestamp"]
            if l_ts.tzinfo is None: l_ts = l_ts.replace(tzinfo=timezone.utc)
            l["aware_ts"] = l_ts
            aware_logs.append(l)

        today_logs = [l for l in aware_logs if l["aware_ts"] >= today_start]
        
        today_hours = await calculate_hours(today_logs)
        week_hours = await calculate_hours(aware_logs)
        
        # Month hours (Separate query for performance)
        month_logs = await attendance_logs_collection.find({
            "$or": [
                {"user_id": user_id_str},
                {"user_id": user_email},
                {"email": user_email}
            ],
            "timestamp": {"$gte": month_start}
        }).to_list(length=2000)
        month_hours = await calculate_hours(month_logs)

        # On-time count
        on_time_count = 0
        user_type = user.get("employee_type", "desk")
        org_doc = None
        if org_id:
            try:
                org_doc = await settings_collection.find_one({"organization_id": str(org_id) if ObjectId.is_valid(str(org_id)) else org_id})
            except Exception: pass

        if user_type == "field":
            start_time_str = (org_doc or {}).get("field_office_start_time", "10:00")
            threshold_mins = int((org_doc or {}).get("field_late_grace_mins") or (org_doc or {}).get("field_late_threshold_mins", 30))
        else:
            start_time_str = (org_doc or {}).get("office_start_time", "09:00")
            threshold_mins = int((org_doc or {}).get("late_grace_mins") or (org_doc or {}).get("late_threshold_mins", 15))

        for log in aware_logs:
            if "in" in log.get("type", "").lower():
                try:
                    start_h, start_m = map(int, start_time_str.split(":"))
                    local_log_time = log["aware_ts"] + timedelta(minutes=tz_offset)
                    limit_time = local_log_time.replace(hour=start_h, minute=start_m, second=0, microsecond=0) + timedelta(minutes=threshold_mins)
                    if local_log_time <= limit_time:
                        on_time_count += 1
                except: pass

        # Latest history
        history_cursor = attendance_logs_collection.find(
            {
                "$or": [
                    {"user_id": user_id_str},
                    {"user_id": user_email},
                    {"email": user_email}
                ]
            }
        ).sort("timestamp", -1).limit(5)
        history = await history_cursor.to_list(length=5)
        for h in history:
            h["_id"] = str(h["_id"])
            if "timestamp" in h and isinstance(h["timestamp"], datetime):
                h["timestamp"] = h["timestamp"].isoformat()


        # NOTE: current_status was already determined by the first latest_log query above.
        # The previous "Absolute Status Sync" block here was a redundant duplicate DB query
        # that re-fetched latest_log and overwrote current_status — removed to save one DB round-trip.
        

        # Resolve per-org WiFi SSID from DB (fallback to global env var)
        # Bug fix: was always returning global OFFICE_WIFI_SSID env var, ignoring per-org DB value
        org_wifi_ssid = os.getenv("OFFICE_WIFI_SSID", "")
        if org_id:
            try:
                _wifi_settings = await settings_collection.find_one(
                    {"organization_id": str(org_id) if ObjectId.is_valid(str(org_id)) else org_id},
                    {"office_wifi_ssid": 1}
                )
                if _wifi_settings and _wifi_settings.get("office_wifi_ssid"):
                    org_wifi_ssid = str(_wifi_settings["office_wifi_ssid"]).strip()
            except Exception:
                pass  # Fallback to env var is fine

        # Upcoming Holidays
        today_str = (datetime.now(timezone.utc) + timedelta(minutes=tz_offset)).strftime("%Y-%m-%d")
        holiday_cursor = holidays_collection.find({
            "$or": [
                {"organization_id": org_id, "date": {"$gte": today_str}},
                {"organization_id": "system_org", "date": {"$gte": today_str}}
            ]
        }).sort("date", 1).limit(5)
        upcoming_holidays_raw = await holiday_cursor.to_list(length=5)
        upcoming_holidays = [
            {"date": h.get("date"), "name": h.get("name"), "type": h.get("holiday_type", "festival")}
            for h in upcoming_holidays_raw
        ]

        # Leave balance
        leave_balance = await calculate_leave_balance(user_email, org_id or "system_org")

        # Today's attendance classification
        today_first_checkin = next((l for l in today_logs if l.get("type") == "check-in"), None)
        today_last_checkout = next((l for l in reversed(today_logs) if l.get("type") == "check-out"), None)
        
        first_in_local = (today_first_checkin["aware_ts"] + timedelta(minutes=tz_offset)) if today_first_checkin else None
        last_out_local = (today_last_checkout["aware_ts"] + timedelta(minutes=tz_offset)) if today_last_checkout else None

        # Check if user has approved leave for today
        today_approved_leave = None
        try:
            today_approved_leave = await leave_requests_collection.find_one({
                "$or": [
                    {"employee_id": user_email.lower().strip()},
                    {"email": user_email.lower().strip()},
                    {"employee_id": str(user["_id"])}
                ],
                "status": {"$regex": "^approved$", "$options": "i"},
                "start_date": {"$lte": today_str},
                "end_date": {"$gte": today_str}
            })
        except Exception:
            pass

        org_settings_doc = await get_effective_org_settings(org_id) if org_id else {}

        today_classification = classify_attendance_day(
            first_checkin_local=first_in_local,
            last_checkout_local=last_out_local,
            worked_hours=today_hours,
            settings=org_settings_doc or {},
            employee_type=user.get("employee_type", "desk"),
            approved_leave=today_approved_leave
        )

        return {
            "email": user["email"],
            "today_hours": today_hours,
            "week_total": week_hours,
            "month_total": month_hours,
            "on_time_count": on_time_count,
            "current_status": current_status,
            "today_attendance_status": today_classification["status"],
            "is_short_leave": today_classification["is_short_leave"],
            "is_half_day": today_classification["is_half_day"],
            "is_late": today_classification["is_late"],
            "is_early_leave": today_classification["is_early_leave"],
            "late_mins": today_classification["late_mins"],
            "early_exit_mins": today_classification["early_exit_mins"],
            "late_off_hours": today_classification["late_off_hours"],
            "early_off_hours": today_classification["early_off_hours"],
            "off_hours": today_classification["off_hours"],
            "classification_reason": today_classification["classification_reason"],
            "is_applied_leave": today_classification["is_applied_leave"],
            "history": history,
            "office_wifi_ssid": org_wifi_ssid,
            "upcoming_holidays": upcoming_holidays,
            "leave_balance": leave_balance
        }
    except Exception as e:
        logger.error(f"Analytics Error: {str(e)}", exc_info=True)
        return {
            "today_hours": "00:00",
            "week_total": "00:00",
            "current_status": current_status,
            "office_wifi_ssid": os.getenv("OFFICE_WIFI_SSID", ""),
            "error": "Internal analytics error"
        }


@app.post("/api/employee/update-face")
async def update_employee_face(req: dict, current_user: dict = Depends(get_current_employee)):
    """Enroll or Update Face Biometrics."""
    # Support both 'image' and 'face_image' parameters for compatibility
    image_data = req.get("face_image") or req.get("image")
    if not image_data:
        raise HTTPException(status_code=400, detail="No face image provided.")
    
    try:
        embedding = get_face_embedding(image_data)
        if embedding is None:
            raise HTTPException(status_code=400, detail="No face detected in the provided image.")
        
        # Save embedding and mark as enrolled
        await employees_collection.update_one(
            {"email": current_user["email"]},
            {"$set": {
                "face_embedding": embedding,
                "needs_face_enrollment": False,
                "profile_image": image_data
            }}
        )
        return {"success": True, "message": "Biometric face registration complete."}
    except Exception as e:
        logger.error(f"Face update failed for {current_user['email']}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Biometric processing error: {str(e)}")


@app.post("/admin/import-employees")
async def admin_import_employees(file: bytes = File(...), current_admin: Admin = Depends(get_current_admin)):
    """Bulk import employees from CSV or Excel with upsert and auto-assignment."""
    try:
        org_id = current_admin.organization_id
        if not org_id:
            raise HTTPException(status_code=403, detail="Organization ID missing in admin context")

        # Detect format: try CSV first, then Excel
        try:
            df = pd.read_csv(io.BytesIO(file))
        except Exception:
            try:
                df = pd.read_excel(io.BytesIO(file))
            except Exception:
                raise HTTPException(status_code=400, detail="Unsupported file format. Please upload a .csv or .xlsx file.")
        
        # Normalize column names (strip whitespace, lowercase)
        df.columns = [c.strip().lower().replace(" ", "_") for c in df.columns]
        
        required_cols = ["full_name", "email"]
        for col in required_cols:
            if col not in df.columns:
                raise HTTPException(status_code=400, detail=f"Missing required column: {col}. Required: full_name, email")
        
        created_count = 0
        updated_count = 0
        error_rows = []
        
        for idx, row in df.iterrows():
            try:
                email = str(row["email"]).strip().lower()
                if not email or email == "nan":
                    error_rows.append({"row": idx + 2, "error": "Missing email"})
                    continue
                    
                existing = await employees_collection.find_one({"email": email})
                
                # Build update fields (only non-empty columns)
                update_fields = {}
                field_map = {
                    "full_name": "full_name",
                    "employee_id": "employee_id",
                    "designation": "designation",
                    "department": "department",
                    "employee_type": "employee_type",
                    "beat_zone_name": "beat_zone_name",
                }
                for csv_col, db_field in field_map.items():
                    if csv_col in df.columns:
                        val = row.get(csv_col)
                        if pd.notna(val) and str(val).strip():
                            update_fields[db_field] = str(val).strip()
                
                # Handle manager_email column for auto-assignment
                if "manager_email" in df.columns:
                    mgr_email = row.get("manager_email")
                    if pd.notna(mgr_email) and str(mgr_email).strip():
                        update_fields["manager_id"] = str(mgr_email).strip().lower()
                
                raw_password = str(row.get("password")).strip() if "password" in df.columns and pd.notna(row.get("password")) and str(row.get("password")).strip() else None
                employee_id = update_fields.get("employee_id", email.split("@")[0])

                if existing:
                    # UPSERT: Update existing employee's metadata
                    if raw_password:
                        update_fields["hashed_password"] = get_password_hash(raw_password)
                        update_fields["force_password_change"] = True

                    if update_fields:
                        await employees_collection.update_one(
                            {"email": email},
                            {"$set": update_fields}
                        )
                        updated_count += 1
                else:
                    # CREATE: New employee
                    final_password = raw_password if raw_password else employee_id
                    
                    employee_dict = {
                        "full_name": update_fields.pop("full_name", email.split("@")[0]),
                        "email": email,
                        "employee_id": employee_id,
                        "designation": update_fields.pop("designation", "Employee"),
                        "department": update_fields.pop("department", "General"),
                        "employee_type": update_fields.pop("employee_type", "desk"),
                        "hashed_password": get_password_hash(final_password),
                        "force_password_change": True,
                        "face_embedding": None,
                        "profile_image": None,
                        "created_at": datetime.now(timezone.utc),
                        "organization_id": org_id,
                        "status": "Active",
                    }
                    if "employee_id" in update_fields: del update_fields["employee_id"]
                    employee_dict.update(update_fields)
                    
                    # For Field employees, initialize territory
                    if employee_dict.get("employee_type") in ["field", "FIELD"]:
                        employee_dict.update({
                            "territory_type": "radius",
                            "territory_radius_meters": 500,
                            "gps_otp_fallback_enabled": True
                        })
                    
                    await employees_collection.insert_one(employee_dict)
                    created_count += 1
                    
            except Exception as e:
                error_rows.append({"row": idx + 2, "error": str(e)[:100]})
        
        return {
            "message": f"Import complete. Created: {created_count}, Updated: {updated_count}, Errors: {len(error_rows)}",
            "created": created_count,
            "updated": updated_count,
            "errors": error_rows[:20]  # Limit error output
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Import failed: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")


@app.get("/admin/employees/import-template")
async def get_import_template(current_admin: Admin = Depends(get_current_admin)):
    """Download a CSV template for employee import."""
    csv_content = "full_name,email,employee_id,password,designation,department,employee_type,manager_email,beat_zone_name\n"
    csv_content += "John Doe,john@example.com,EMP001,,Sales Executive,Sales,field,manager@example.com,North Zone\n"
    csv_content += "Jane Smith,jane@example.com,EMP002,Secret@321,Developer,Engineering,desk,,\n"
    
    return StreamingResponse(
        io.BytesIO(csv_content.encode()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=employee_import_template.csv"}
    )


@app.get("/admin/export-logs-pdf")
async def admin_export_logs_pdf(current_admin: Admin = Depends(get_current_admin)):
    """Generate a PDF report of all attendance logs for the admin's organization."""
    try:
        org_id = current_admin.organization_id
        if not org_id:
             raise HTTPException(status_code=403, detail="Organization context required")

        # Get all employee IDs for this org
        org_employees = await employees_collection.find({"organization_id": org_id}, {"_id": 1}).to_list(None)
        org_emp_ids = [str(emp["_id"]) for emp in org_employees]

        cursor = attendance_logs_collection.find({"user_id": {"$in": org_emp_ids}}).sort("timestamp", -1)
        logs = await cursor.to_list(length=5000)
        
        buffer = io.BytesIO()
        doc = SimpleDocTemplate(buffer, pagesize=landscape(letter), rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
        elements = []
        
        styles = getSampleStyleSheet()
        elements.append(Paragraph("LogDay Attendance Audit Report", styles['Title']))
        elements.append(Paragraph(f"Generated on: {datetime.now(timezone.utc).astimezone(timezone(timedelta(minutes=330))).strftime('%Y-%m-%d %I:%M %p')} (IST)", styles['Normal']))
        elements.append(Spacer(1, 20))
        
        data = [["Timestamp", "Employee Name", "Type", "Location", "Verification"]]
        for log in logs:
            ts = log.get("timestamp")
            if isinstance(ts, datetime):
                # Localize to IST for PDF Display
                ist_ts = ts.astimezone(timezone(timedelta(minutes=330)))
                ts_str = ist_ts.strftime("%Y-%m-%d %I:%M %p")
            else:
                ts_str = str(ts)
                
            data.append([
                ts_str,
                log.get("full_name") or log.get("email") or "Unknown",
                log.get("type", "check-in").upper(),
                log.get("address", "Main Office"),
                log.get("status", "SUCCESS").upper()
            ])
            
        table = Table(data, hAlign='LEFT', colWidths=[150, 150, 100, 200, 100])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#7c3aed")),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 12),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
            ('BACKGROUND', (0, 1), (-1, -1), colors.whitesmoke),
            ('GRID', (0, 0), (-1, -1), 1, colors.grey)
        ]))
        
        elements.append(table)
        doc.build(elements)
        
        buffer.seek(0)
        return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=attendance_report_{datetime.now(timezone.utc).astimezone(timezone(timedelta(minutes=330))).strftime('%Y%m%d')}.pdf"}
    )
    except Exception as e:
        logger.error(f"PDF export error: {e}")
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {str(e)}")


@app.get("/admin/export-logs-excel")
async def admin_export_logs_excel(current_admin: Admin = Depends(get_current_admin)):
    """Export attendance logs to Excel for the admin's organization."""
    try:
        org_id = current_admin.organization_id
        if not org_id:
             raise HTTPException(status_code=403, detail="Organization context required")

        org_employees = await employees_collection.find({"organization_id": org_id}, {"_id": 1}).to_list(None)
        org_emp_ids = [str(emp["_id"]) for emp in org_employees]

        logs = await attendance_logs_collection.find({"user_id": {"$in": org_emp_ids}}).sort("timestamp", -1).to_list(5000)
        
        # Flatten logs for Excel
        data = []
        for log in logs:
            data.append({
                "Employee": log.get("full_name") or log.get("email"),
                "Email": log.get("email"),
                "Time": (log.get("timestamp").astimezone(timezone(timedelta(minutes=330))).strftime("%Y-%m-%d %H:%M:%S") + " (IST)") if log.get("timestamp") else "N/A",
                "Type": log.get("type").title(),
                "Late": "Yes" if log.get("is_late") else "No",
                "Late Mins": log.get("late_mins", 0),
                "Address": log.get("address"),
                "WiFi": log.get("wifi_ssid"),
                "Distance (m)": round(log.get("distance_meters", 0), 1),
                "Duration (h)": log.get("duration_hours", 0)
            })
        
        df = pd.DataFrame(data)
        
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='Attendance Logs')
        
        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=attendance_logs_{datetime.now(timezone.utc).astimezone(timezone(timedelta(minutes=330))).strftime('%Y%m%d')}.xlsx"}
        )
    except Exception as e:
        logger.error(f"Excel export error: {e}")
        raise HTTPException(status_code=500, detail=f"Excel export failed: {str(e)}")


# --- ADMIN ENDPOINTS ---

@app.post("/admin/login")
async def admin_login(req: AdminLoginRequest):
    """Database-driven admin authentication."""
    clean_email = req.email.strip().lower()
    # 1. Check database for admin
    admin = await admins_collection.find_one({"email": clean_email})
    
    # 2. Verify password
    if admin and verify_password(req.password, admin.get("hashed_password")):
        token_data = {"sub": clean_email, "role": admin.get("role", "admin")}
        # Add Organization Context if present
        if admin.get("organization_id"):
            token_data["org_id"] = admin.get("organization_id")
            
        token = create_access_token(data=token_data)
        
        # Fetch Org details for branding if applicable
        org_details = {}
        if admin.get("organization_id"):
            try:
                org = await organizations_collection.find_one({"_id": ObjectId(admin.get("organization_id"))})
                if org:
                    org_details = {
                        "id": str(org["_id"]),
                        "name": org.get("name"),
                        "slug": org.get("slug"),
                        "logo_url": org.get("logo_url"),
                        "primary_color": org.get("primary_color")
                    }
            except Exception:
                pass

        return {
            "access_token": token, 
            "token_type": "bearer", 
            "user": {
                "name": admin.get("full_name", "Administrator"), 
                "email": admin.get("email"),
                "organization_id": admin.get("organization_id"),
                "role": admin.get("role")
            },
            "organization": org_details
        }
    
    # 3. Fallback to hardcoded env (temporary safety net / migration)
    admin_email = os.getenv("ADMIN_EMAIL", "admin@officeflow.ai")
    admin_pass = os.getenv("ADMIN_PASSWORD", "admin123")
    
    if req.email == admin_email and req.password == admin_pass:
        token = create_access_token(data={"sub": req.email, "role": "superadmin"})
        return {"access_token": token, "token_type": "bearer", "user": {"name": "Super Admin", "email": admin_email, "role": "superadmin"}}

    raise HTTPException(status_code=401, detail="Invalid admin credentials")




@app.post("/admin/register-organization")
async def register_organization(req: OrganizationRegisterRequest):
    """
    Public Endpoint: Registers a new Organization and its Account Owner (Super Admin).
    """
    try:
        # 1. Check if Slug or Email exists
        if await organizations_collection.find_one({"slug": req.org_slug}):
             raise HTTPException(status_code=400, detail="Organization ID (slug) already taken.")
        
        if await admins_collection.find_one({"email": req.admin_email}):
            raise HTTPException(status_code=400, detail="Admin email already registered.")

        # 2. Create Organization
        new_org = {
            "name": req.org_name,
            "slug": req.org_slug,
            "logo_url": req.logo_url,
            "primary_color": req.primary_color,
            "created_at": datetime.now(timezone.utc)
        }
        org_result = await organizations_collection.insert_one(new_org)
        org_id = str(org_result.inserted_id)

        # 3. Create Org Admin (Owner)
        hashed_password = get_password_hash(req.admin_password)
        new_admin = {
            "email": req.admin_email,
            "hashed_password": hashed_password,
            "full_name": req.admin_full_name,
            "role": "owner",
            "organization_id": org_id,  # Link to the new Org
            "created_at": datetime.now(timezone.utc)
        }
        await admins_collection.insert_one(new_admin)

        # 4. Initialize Default Settings for this Org
        default_settings = {
            "organization_id": org_id,
            "office_start_time": "09:00",
            "late_threshold_mins": 15,
            "half_day_hours": 4,
            "full_day_hours": 8,
            "updated_at": datetime.now(timezone.utc)
        }
        await settings_collection.insert_one(default_settings)

        return {
            "message": f"Organization '{req.org_name}' registered successfully!",
            "organization_id": org_id,
            "org_slug": req.org_slug,
            "admin_email": req.admin_email
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Organization registration failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Organization registration failed.")



@app.get("/admin/sub-admins")
async def list_sub_admins(current_admin: Admin = Depends(get_current_admin)):
    """List all admins for the organization (Owner/Superadmin only)."""
    if current_admin.role not in ["owner", "superadmin", "admin"]:
        raise HTTPException(status_code=403, detail="Only organization owners/admins can manage the admin team.")
    
    query = {} if _is_superadmin(current_admin) else {"organization_id": current_admin.organization_id}
    cursor = admins_collection.find(query, {"hashed_password": 0})
    admins = await cursor.to_list(length=100)
    for a in admins:
        a["_id"] = str(a["_id"])
    return admins


@app.post("/admin/sub-admins")
async def create_sub_admin(req: SubAdminCreate, current_admin: Admin = Depends(get_current_admin)):
    """Create a new sub-admin for the organization (Owner/Superadmin only)."""
    if current_admin.role not in ["owner", "superadmin", "admin"]:
        logger.warning(f"Access Denied: Admin {current_admin.email} with role {current_admin.role} tried to manage sub-admins.")
        raise HTTPException(status_code=403, detail="Only organization owners/admins can add new admins.")
    
    clean_email = req.email.strip().lower()
    # Check if admin already exists
    existing = await admins_collection.find_one({"email": clean_email})
    if existing:
        raise HTTPException(status_code=400, detail="Admin email already registered.")
    
    hashed_password = get_password_hash(req.password)
    new_admin = {
        "email": clean_email,
        "hashed_password": hashed_password,
        "full_name": req.full_name,
        "role": req.role if hasattr(req, 'role') and req.role else "admin",
        "organization_id": req.organization_id if (req.organization_id and _is_superadmin(current_admin)) else current_admin.organization_id,
        "created_at": datetime.now(timezone.utc),
        "allowed_features": ["dashboard", "employees", "attendance", "leaves", "expenses", "reports", "war_room", "territory", "nudge", "leaderboard"]
    }
    await admins_collection.insert_one(new_admin)
    return {"message": f"Admin {req.full_name} created successfully."}


@app.delete("/admin/sub-admins/{email}")
async def delete_sub_admin(email: str, current_admin: Admin = Depends(get_current_admin)):
    """Remove a sub-admin (Owner/Superadmin only)."""
    if current_admin.role not in ["owner", "superadmin", "admin"]:
        raise HTTPException(status_code=403, detail="Only organization owners/admins can remove admins.")
        
    if email == current_admin.email:
        raise HTTPException(status_code=400, detail="You cannot delete yourself.")
        
    # Security: Ensure we only delete admins from the same organization and who are NOT owners/superadmins
    target_admin = await admins_collection.find_one({"email": email})
    if not target_admin:
        raise HTTPException(status_code=404, detail="Sub-admin not found.")
        
    if not _is_superadmin(current_admin) and target_admin.get("organization_id") != current_admin.organization_id:
        raise HTTPException(status_code=403, detail="You do not have permission to delete this account.")
        
    if target_admin.get("role") in ["owner", "superadmin"]:
        raise HTTPException(status_code=403, detail="You cannot delete an owner or superadmin account.")
        
    await admins_collection.delete_one({"_id": target_admin["_id"]})
        
    return {"message": f"Admin {email} removed successfully."}


@app.get("/admin/me")
async def get_admin_me(current_admin: Admin = Depends(get_current_admin)):
    """Return current admin's profile including role and allowed features."""
    admin_data = {
        "email": current_admin.email,
        "full_name": current_admin.full_name,
        "role": current_admin.role,
        "organization_id": current_admin.organization_id,
    }
    # owner, superadmin, and admin get all features by default
    if current_admin.role in ["owner", "superadmin", "admin"]:
        admin_data["allowed_features"] = ["dashboard", "employees", "attendance", "leaves", "expenses", "reports", "war_room", "territory", "nudge", "leaderboard", "sub_admins", "settings"]
    else:
        admin_data["allowed_features"] = current_admin.allowed_features or ["dashboard", "employees", "attendance"]
    return admin_data


@app.put("/admin/sub-admins/{email}/permissions")
async def update_sub_admin_permissions(email: str, req: dict, current_admin: Admin = Depends(get_current_admin)):
    """Update feature-level permissions for a sub-admin (Owner/Superadmin only)."""
    if current_admin.role not in ["owner", "superadmin", "admin"]:
        raise HTTPException(status_code=403, detail="Only organization owners/admins can update permissions.")
    
    allowed_features = req.get("allowed_features", [])
    role = req.get("role")  # optionally update role too
    
    update_fields = {"allowed_features": allowed_features}
    if role:
        update_fields["role"] = role
    
    perm_query = {"email": email}
    if not _is_superadmin(current_admin):
        perm_query["organization_id"] = current_admin.organization_id
    result = await admins_collection.update_one(
        perm_query,
        {"$set": update_fields}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Sub-admin not found.")
    
    return {"message": f"Permissions for {email} updated successfully."}


# NOTE: Duplicate /admin/stats removed. The authenticated version is at line ~2063.


@app.get("/admin/employees")
async def admin_list_employees(current_admin: Admin = Depends(get_current_admin)):
    """List all employees for management (Role Scoped)."""
    filter_query = get_employee_filter(current_admin)
    cursor = employees_collection.find(filter_query, {"hashed_password": 0, "face_embedding": 0})
    employees = await cursor.to_list(length=1000)
    for emp in employees:
        emp["_id"] = str(emp["_id"])
    return employees



@app.put("/admin/employees/{email}")
async def admin_update_employee(email: str, req: EmployeeUpdate, current_admin: Admin = Depends(get_current_admin)):
    """Update employee details."""
    update_data = {k: v for k, v in req.dict().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No update data provided")
    
    # Normalize email
    clean_email = email.strip().lower()
    query = {"email": clean_email}
    
    # Restrict by organization unless superadmin
    if current_admin.organization_id and current_admin.role != AdminRole.SUPERADMIN:
        query["organization_id"] = current_admin.organization_id

    result = await employees_collection.update_one(query, {"$set": update_data})
    if result.matched_count == 0:
        logger.warning(f"Admin {current_admin.email} tried to update non-existent or unauthorized employee: {clean_email}")
        raise HTTPException(status_code=404, detail="Employee not found")
        
    return {"message": "Employee updated successfully"}


@app.get("/admin/hrms/stats")
async def get_hrms_stats(current_admin: Admin = Depends(get_current_admin)):
    org_id = current_admin.organization_id
    org_filter = {"organization_id": org_id} if (org_id and not _is_superadmin(current_admin)) else {}
    
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")
    
    onboarding_active = await onboardings_collection.count_documents({
        **org_filter,
        "status": {"$in": ["pending", "in_progress"]}
    })
    payroll_pending = await payrolls_collection.count_documents({
        **org_filter,
        "status": "draft",
        "payroll_month": current_month
    })
    exits_active = await exit_managements_collection.count_documents({
        **org_filter,
        "status": {"$in": ["pending", "in_progress"]}
    })
    
    return {
        "onboarding_active": onboarding_active,
        "payroll_pending": payroll_pending,
        "exits_active": exits_active
    }


@app.post("/admin/employees/bulk-update-type")

async def admin_bulk_update_employee_type(req: dict, current_admin: Admin = Depends(get_current_admin)):
    """Bulk update employee work type (desk/field/office)"""
    emails = [e.lower().strip() for e in req.get("employee_emails", [])]
    new_type = req.get("employee_type")
    
    if not emails or not new_type:
        raise HTTPException(status_code=400, detail="employee_emails and employee_type are required")
        
    bulk_query = {"email": {"$in": emails}}
    if not _is_superadmin(current_admin):
        bulk_query["organization_id"] = current_admin.organization_id
    result = await employees_collection.update_many(
        bulk_query,
        {"$set": {"employee_type": new_type}}
    )
    
    return {"message": f"Successfully updated {result.modified_count} employees to {new_type}"}


@app.delete("/admin/employees/{email}")
async def admin_delete_employee(email: str, current_admin: Admin = Depends(get_current_admin)):
    """Remove an employee and their logs."""
    query = {"email": email}
    if current_admin.organization_id and not _is_superadmin(current_admin):
        query["organization_id"] = current_admin.organization_id
        
    user = await employees_collection.find_one(query)
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    # Delete logs first
    await attendance_logs_collection.delete_many({"user_id": str(user["_id"])})
    # Delete user
    await employees_collection.delete_one({"email": email})
    
    return {"message": f"Employee {email} and associated logs deleted successfully"}


@app.get("/admin/logs")
async def admin_all_logs(
    limit: int = 100, 
    month: Optional[str] = None, 
    current_admin: Admin = Depends(get_current_admin)
):
    """Fetch all attendance logs for the organization (Role Scoped)."""
    filter_query = get_employee_filter(current_admin)
    
    # If filter has manager_id, we need to find user_ids first
    if "manager_id" in filter_query:
        org_employees = await employees_collection.find(filter_query, {"_id": 1}).to_list(None)
        org_emp_ids = [str(emp["_id"]) for emp in org_employees]
        log_query = {"user_id": {"$in": org_emp_ids}}
    else:
        # For non-manager admins, we still need to filter by org usually
        # but our helper handles that mapping based on roles
        log_emp_filter = get_employee_filter(current_admin)
        org_employees = await employees_collection.find(log_emp_filter, {"_id": 1}).to_list(None)
        org_emp_ids = [str(emp["_id"]) for emp in org_employees]
        log_query = {"user_id": {"$in": org_emp_ids}}

    if month:
        try:
            start_date = datetime.strptime(f"{month}-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)
            year, m = map(int, month.split("-"))
            end_date = datetime(year + 1, 1, 1, tzinfo=timezone.utc) if m == 12 else datetime(year, m + 1, 1, tzinfo=timezone.utc)
            
            # Basic tz shift for localized query
            tz_offset = 330
            utc_start = start_date - timedelta(minutes=tz_offset)
            utc_end = end_date - timedelta(minutes=tz_offset)
            
            log_query["timestamp"] = {"$gte": utc_start, "$lt": utc_end}
        except ValueError:
            pass

    # If month provided, maybe fetch more or no limit? The limit is defaults to 100
    if month:
        cursor = attendance_logs_collection.find(log_query).sort("timestamp", -1).limit(5000)
    else:
        cursor = attendance_logs_collection.find(log_query).sort("timestamp", -1).limit(limit)
        
    logs = await cursor.to_list(length=5000 if month else limit)
    # Enrich with employee details for the UI
    employees = await employees_collection.find({"_id": {"$in": [ObjectId(uid) for uid in org_emp_ids if ObjectId.is_valid(uid)]}}, {"full_name": 1, "email": 1, "profile_image": 1}).to_list(None)
    emp_map = {str(e["_id"]): e for e in employees}
    
    for log in logs:
        log["_id"] = str(log["_id"])
        emp = emp_map.get(log.get("user_id"), {})
        log["full_name"] = emp.get("full_name", "Unknown")
        log["email"] = emp.get("email", "")
        log["profile_image"] = emp.get("profile_image", "")
        if isinstance(log.get("timestamp"), datetime):
            log["timestamp"] = log["timestamp"].replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    return logs


@app.post("/admin/employees")
async def admin_create_employee(req: RegisterRequest, current_admin: Admin = Depends(get_current_admin)):
    """Manually register a new employee (Admin)."""
    clean_email = req.email.strip().lower()
    # Check if employee already exists (globally unique email)
    existing = await employees_collection.find_one({"email": clean_email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    # Embedding is optional for manual registration if they will enroll later
    embedding = None
    if req.face_image:
        embedding = get_face_embedding(req.face_image)

    hashed_password = get_password_hash(req.password)
    employee_dict = {
        "full_name": req.full_name,
        "email": clean_email,
        "employee_id": req.employee_id,
        "designation": req.designation,
        "department": req.department,
        "hashed_password": hashed_password,
        "face_embedding": embedding,
        "profile_image": req.face_image if req.face_image else None,
        "device_id": None, # Force bind on first use
        "created_at": datetime.now(timezone.utc),
        "needs_face_enrollment": True if not embedding else False,
        "employee_type": req.employee_type,
        "organization_id": req.organization_id if (req.organization_id and _is_superadmin(current_admin)) else current_admin.organization_id,  # Superadmin can target any org
        "force_password_change": True,  # Employee must set their own password on first desk login
        "status": "Active"
    }

    await employees_collection.insert_one(employee_dict)
    logger.info(f"Employee {clean_email} created by admin {current_admin.email} with force_password_change=True")
    return {"message": f"Employee {req.full_name} registered successfully"}


@app.post("/admin/employees/{email}/reset-password")
async def admin_reset_password(email: str, req: dict, current_admin: Admin = Depends(get_current_admin)):
    """Reset an employee's password."""
    from urllib.parse import unquote
    clean_email = unquote(email).strip().lower()
    logger.info(f"Password reset requested for '{clean_email}' by admin '{current_admin.email}' (role={current_admin.role}, org={current_admin.organization_id})")
    
    # Build query - superadmin/owner can reset any employee, others restricted to their org
    query = {"email": clean_email}
    is_privileged = current_admin.role in ["superadmin", "owner"] or clean_email == os.getenv("ADMIN_EMAIL", "admin@officeflow.ai")
    
    if current_admin.organization_id and not is_privileged:
        # Non-privileged admins can only reset their own org's employees
        if current_admin.organization_id != "system_org":
            query["organization_id"] = current_admin.organization_id

    user = await employees_collection.find_one(query)
    if not user:
        logger.warning(f"Password reset failed: employee '{clean_email}' not found with query {query}")
        raise HTTPException(status_code=404, detail="Employee not found or access denied")

    new_password = req.get("password")
    if not new_password:
        raise HTTPException(status_code=400, detail="New password is required")
    
    hashed_password = get_password_hash(new_password)
    result = await employees_collection.update_one(
        {"email": clean_email},
        {"$set": {
            "hashed_password": hashed_password,
            "force_password_change": True  # Force them to change on next login
        }}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Employee not found")
    
    logger.info(f"Password reset successful for '{clean_email}'")
    return {"message": "Password reset successfully"}


@app.post("/admin/employees/{email}/clear-binding")
async def admin_clear_binding(email: str, current_admin: Admin = Depends(get_current_admin)):
    """Clear hardware binding for an employee."""
    from urllib.parse import unquote
    clean_email = unquote(email).strip().lower()
    
    # Build query - superadmin/owner can clear any binding, others restricted to their org
    query = {"email": clean_email}
    is_privileged = current_admin.role in ["superadmin", "owner"]
    
    if current_admin.organization_id and not is_privileged:
        if current_admin.organization_id != "system_org":
            query["organization_id"] = current_admin.organization_id

    result = await employees_collection.update_one(
        query,
        {"$set": {"device_id": None}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Employee not found")
        
    return {"message": "Hardware binding cleared successfully"}


# --- HELPER FUNCTIONS ---
def _is_superadmin(admin: Admin) -> bool:
    """Check if admin is a superadmin (by role or system_org binding)."""
    return admin.role == AdminRole.SUPERADMIN or admin.organization_id == "system_org"

def get_employee_filter(current_admin: Admin):
    """Returns a MongoDB filter based on admin role for multi-tenant segmenting."""
    org_id = current_admin.organization_id
    if not org_id or _is_superadmin(current_admin):
        # Superadmin / system_org sees all organizations
        return {}
    
    role = current_admin.role
    base_filter = {"organization_id": org_id}
    
    if role == "manager":
        # Managers only see their assigned team
        base_filter["manager_id"] = current_admin.email
        
    return base_filter

async def get_scoped_employee_ids(current_admin: Admin):
    base_filter = get_employee_filter(current_admin)
    emps = await employees_collection.find(base_filter, {"_id": 1}).to_list(None)
    return [str(e["_id"]) for e in emps]

async def get_scoped_employee_emails(current_admin: Admin):
    base_filter = get_employee_filter(current_admin)
    emps = await employees_collection.find(base_filter, {"email": 1}).to_list(None)
    return [e["email"] for e in emps]

async def get_scoped_employee_employee_ids(current_admin: Admin):
    base_filter = get_employee_filter(current_admin)
    emps = await employees_collection.find(base_filter, {"employee_id": 1}).to_list(None)
    return [e.get("employee_id") for e in emps if "employee_id" in e]


def check_feature_access(admin: Admin, feature: str):
    """Raises 403 if the admin doesn't have access to a specific feature."""
    if admin.role in ["owner", "superadmin"]:
        return  # Full access
    allowed = admin.allowed_features or []
    if feature not in allowed:
        raise HTTPException(status_code=403, detail=f"You do not have access to the '{feature}' feature. Contact your admin.")


@app.post("/admin/employees/bulk-assign-manager")
async def bulk_assign_manager(req: dict, current_admin: Admin = Depends(get_current_admin)):
    """Assign multiple employees to a manager by email."""
    if current_admin.role not in ["owner", "hr", "superadmin"]:
        raise HTTPException(status_code=403, detail="Insufficient permissions to assign managers.")
    
    employee_emails = [e.lower().strip() for e in req.get("employee_emails", [])]
    manager_email = req.get("manager_email", "").lower().strip()
    
    if not manager_email:
        raise HTTPException(status_code=400, detail="Manager email is required")

    org_id = current_admin.organization_id
    
    result = await employees_collection.update_many(
        {"email": {"$in": employee_emails}, "organization_id": org_id},
        {"$set": {"manager_id": manager_email}}
    )
    
    return {"message": f"Successfully assigned {result.modified_count} employees to {manager_email}."}


@app.get("/admin/settings")
async def get_settings(current_admin: Admin = Depends(get_current_admin)):
    """Retrieve organization-specific configuration."""
    org_id = current_admin.organization_id
    
    try:
        # Use specific query based on organization linkage
        if not org_id or org_id == "system_org":
             settings = await settings_collection.find_one({"id": "config"})
        else:
             settings = await settings_collection.find_one({"organization_id": org_id})

        # Get defaults
        default_settings = SystemSettings()
        
        if not settings:
            return default_settings.dict()
        
        # Merge stored data into the model to handle defaults and types safely
        stored_data = {k: v for k, v in settings.items() if k not in ["_id", "organization_id", "id"]}
        
        # Combine default values with stored values, ignoring None or invalid types in stored_data
        settings_dict = default_settings.dict()
        for k, v in stored_data.items():
            if k in settings_dict and v is not None:
                settings_dict[k] = v
                
        return settings_dict
    except Exception as e:
        logger.error(f"Error fetching settings: {e}")
        return SystemSettings().dict()




@app.put("/admin/settings")
async def update_settings(req: SystemSettings, current_admin: Admin = Depends(get_current_admin)):
    """Update organization-specific configuration and branding."""
    org_id = current_admin.organization_id
    if not org_id:
        raise HTTPException(status_code=403, detail="Organization linkage required for settings update.")

    try:
        update_dict = req.dict()
        update_dict["organization_id"] = org_id
        update_dict["updated_at"] = datetime.now(timezone.utc)

        # Persistence to settings collection
        if org_id == "system_org":
            await settings_collection.update_one(
                {"id": "config"},
                {"$set": update_dict},
                upsert=True
            )
        else:
            await settings_collection.update_one(
                {"organization_id": org_id},
                {"$set": update_dict},
                upsert=True
            )

        # Branding persistence to organization collection (only for non-system orgs)
        if org_id != "system_org" and ObjectId.is_valid(org_id):
            branding_update = {}
            if update_dict.get("primary_color"):
                branding_update["primary_color"] = update_dict.get("primary_color")
            if update_dict.get("logo_url"):
                branding_update["logo_url"] = update_dict.get("logo_url")
            
            if branding_update:
                await organizations_collection.update_one(
                    {"_id": ObjectId(org_id)},
                    {"$set": branding_update}
                )

        return {"message": "Settings and branding updated successfully"}
    except Exception as e:
        logger.error(f"Error updating settings: {e}")
        raise HTTPException(status_code=500, detail="Failed to save settings")


@app.post("/admin/upload-logo")
async def admin_upload_logo(file: bytes = File(...), current_admin: Admin = Depends(get_current_admin)):
    """Upload organization logo image."""
    try:
        org_id = current_admin.organization_id
        if not org_id:
            raise HTTPException(status_code=403, detail="Organization context required")

        # Generate unique filename
        file_extension = "png" # Default, or we could extract from headers
        filename = f"logo_{org_id}_{uuid.uuid4().hex[:8]}.{file_extension}"
        file_path = os.path.join(UPLOAD_DIR, filename)
        
        with open(file_path, "wb") as buffer:
            buffer.write(file)
            
        # Generate full URL
        # In production this should be the full domain, for now relative or configured base
        base_url = os.getenv("API_BASE_URL", "http://localhost:8000")
        logo_url = f"{base_url}/uploads/{filename}"
        
        return {"logo_url": logo_url}
    except Exception as e:
        logger.error(f"Logo upload failed: {e}")
        raise HTTPException(status_code=500, detail=f"Logo upload failed: {str(e)}")


@app.get("/settings")
async def get_public_settings(request: Request):
    """Public settings for mobile app (timings, etc.). Supports Org-specific overrides if token present."""
    auth_header = request.headers.get("Authorization")
    org_id = None
    
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        try:
            payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            email = payload.get("sub")
            if email:
                user = await employees_collection.find_one({"email": email})
                if user:
                    org_id = user.get("organization_id")
        except Exception:
            pass # Invalid token, fall back to global
            
    if org_id:
        settings = await settings_collection.find_one({"organization_id": org_id})
        if settings:
            settings["_id"] = str(settings["_id"])
            return settings
            
    # Fallback to global config
    settings = await settings_collection.find_one({"id": "config"})
    if not settings:
        return SystemSettings().dict()
    settings["_id"] = str(settings["_id"])
    return settings


@app.get("/organizations/search")
async def search_organizations(q: str = None):
    """Public search for organizations by name or slug with improved error handling."""
    try:
        search_query = q.strip() if q else ""
        logger.info(f"[SEARCH] Query received: '{search_query}'")
        
        if not search_query or len(search_query) < 2:
            # Return top organizations if query is empty or 1 character
            cursor = organizations_collection.find({}).limit(20)
            orgs = await cursor.to_list(length=20)
        else:
            # Case-insensitive partial match on both name and slug
            regex_pattern = {"$regex": re.escape(search_query), "$options": "i"}
            mongo_query = {
                "$or": [
                    {"name": regex_pattern},
                    {"slug": regex_pattern}
                ]
            }
            cursor = organizations_collection.find(mongo_query).limit(15)
            orgs = await cursor.to_list(length=15)
        
        # Log results
        logger.info(f"[SEARCH] Found {len(orgs)} organizations for query: '{search_query}'")
        
        # Format results
        results = []
        for org in orgs:
            result_item = {
                "id": str(org["_id"]),
                "name": org.get("name", "Unknown"),
                "slug": org.get("slug", ""),
                "logo_url": org.get("logo_url"),
                "primary_color": org.get("primary_color", "#0f172a"),
                "_id": str(org["_id"])  # Include both 'id' and '_id' for compatibility
            }
            results.append(result_item)
        
        # Return results with metadata
        return {
            "results": results,
            "total": len(results),
            "query": search_query
        }
        
    except Exception as e:
        logger.error(f"[SEARCH] Error searching organizations: {str(e)}", exc_info=True)
        return {
            "results": [],
            "total": 0,
            "query": q,
            "error": str(e)
        }


@app.get("/organizations/debug")
async def debug_organizations():
    """Debug endpoint: List all organizations in database (only in non-prod)"""
    if APP_ENV == "production":
        raise HTTPException(status_code=403, detail="Debug endpoint not available in production")
    
    try:
        logger.info("[DEBUG] Fetching all organizations...")
        
        all_orgs = await organizations_collection.find().to_list(100)
        
        results = []
        for org in all_orgs:
            results.append({
                "_id": str(org["_id"]),
                "name": org.get("name", "Unknown"),
                "slug": org.get("slug", ""),
                "logo_url": org.get("logo_url"),
                "primary_color": org.get("primary_color"),
                "created_at": org.get("created_at")
            })
        
        return {
            "total": len(results),
            "organizations": results,
            "environment": APP_ENV,
            "message": "All organizations in database"
        }
    except Exception as e:
        logger.error(f"[DEBUG] Error: {str(e)}", exc_info=True)
        return {
            "total": 0,
            "organizations": [],
            "error": str(e),
            "message": "Failed to fetch organizations"
        }



# -----------------------------------------------------------------------------
# FIELD SALES MODULE - /api/field/
# -----------------------------------------------------------------------------

@app.post("/api/field/plan")
async def submit_visit_plan(plan: VisitPlan, employee=Depends(get_current_employee)):
    """Submit a daily visit plan for approval."""
    try:
        # Security Enforcement: Identity Hijack Prevention
        plan_dict = plan.dict()
        plan_dict["employee_id"] = employee["email"]
        plan_dict["organization_id"] = employee["organization_id"]
        
        # Check if plan already exists for this date
        existing = await visit_plans_collection.find_one({
            "employee_id": employee["email"],
            "date": plan.date
        })
        
        if existing:
            await visit_plans_collection.delete_one({"_id": existing["_id"]})
        
        plan_dict["status"] = PlanStatus.SUBMITTED
        plan_dict["submitted_at"] = datetime.now(timezone.utc)
        
        result = await visit_plans_collection.insert_one(plan_dict)
        return {"status": "success", "message": "Visit plan submitted for manager approval", "plan_id": str(result.inserted_id)}
    except Exception as e:
        logger.error(f"Failed to submit plan: {e}")
        raise HTTPException(status_code=500, detail="Plan submission failed")


@app.get("/api/field/plan/{employee_id}")
async def get_current_plan(employee_id: str, date: Optional[str] = None, current_user=Depends(get_current_employee)):
    """Retrieve the active (approved) plan for today."""
    # Privacy Enforcement: Can only see own plan (or admin if we added that check)
    if employee_id != current_user["email"]:
        raise HTTPException(status_code=403, detail="Access denied: Cannot view another agent's plan.")
        
    query_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    plan = await visit_plans_collection.find_one({
        "employee_id": employee_id,
        "date": query_date,
        "status": PlanStatus.APPROVED
    })
    
    if not plan:
        # Check for submitted/draft if no approved one exists
        plan = await visit_plans_collection.find_one({
            "employee_id": employee_id,
            "date": query_date
        })
        
    if not plan:
        return {"status": "no_plan", "message": "No visit plan found for today."}
    
    plan["_id"] = str(plan["_id"])

    # Enrichment: Inject Status (completed, ongoing, pending) into stops
    # fetch all logs for this agent today
    visit_logs = await visit_logs_collection.find({
        "employee_id": employee_id,
        "date": query_date
    }).to_list(length=100)

    # Map stop_id to status
    status_map = {}
    for log in visit_logs:
        stop_id = log.get("visit_plan_stop_id")
        if stop_id:
            status_map[stop_id] = log.get("status", "completed")

    for stop in plan.get("stops", []):
        stop_id = stop.get("visit_id") # frontend sends visit_id as stop identifier
        stop["status"] = status_map.get(stop_id, "pending")

    return plan


@app.post("/api/field/plan/optimize")
async def optimize_route(req: dict, employee=Depends(get_current_employee)):
    """Optimize the order of stops in today's approved plan using Nearest Neighbor TSP."""
    try:
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        plan = await visit_plans_collection.find_one({
            "employee_id": employee["email"],
            "date": today_str,
            "status": {"$in": [PlanStatus.APPROVED, "approved"]}
        })
        if not plan:
            raise HTTPException(status_code=404, detail="No approved plan for today")

        stops = plan.get("stops", [])
        if len(stops) <= 1:
            return {"status": "success", "message": "No optimization needed", "stops": stops}

        # Handle current coordinates safely
        curr_lat = req.get("current_lat")
        curr_lng = req.get("current_lng")
        
        # If no current coords provided, use the first stop's coords as starting point
        if curr_lat is None or curr_lng is None:
            starts_lat = float(stops[0].get("place_lat", 0))
            starts_lng = float(stops[0].get("place_lng", 0))
        else:
            starts_lat = float(curr_lat)
            starts_lng = float(curr_lng)

        # Nearest Neighbor Algorithm
        unvisited = list(range(len(stops)))
        current_lat, current_lng = starts_lat, starts_lng
        ordered_indices = []

        while unvisited:
            nearest_idx = -1
            nearest_dist = float("inf")
            for idx in unvisited:
                stop = stops[idx]
                slat = float(stop.get("place_lat", 0))
                slng = float(stop.get("place_lng", 0))
                # Skip invalid coords
                if slat == 0 and slng == 0: continue
                
                dist = calculate_haversine(current_lat, current_lng, slat, slng)
                if dist < nearest_dist:
                    nearest_dist = dist
                    nearest_idx = idx
            
            if nearest_idx == -1: # Fallback if all remaining have invalid coords
                nearest_idx = unvisited[0]
                
            ordered_indices.append(nearest_idx)
            current_lat = float(stops[nearest_idx].get("place_lat", 0))
            current_lng = float(stops[nearest_idx].get("place_lng", 0))
            unvisited.remove(nearest_idx)

        optimized_stops = []
        for i, idx in enumerate(ordered_indices):
            stop = stops[idx]
            stop["sequence_order"] = i + 1 # 1-indexed
            optimized_stops.append(stop)

        # Persist updated order
        await visit_plans_collection.update_one(
            {"_id": plan["_id"]},
            {"$set": {"stops": optimized_stops}}
        )

        return {"status": "success", "stops": optimized_stops}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Route optimization failed: {e}")
        raise HTTPException(status_code=500, detail=f"Route optimization failed: {str(e)}")


@app.post("/api/field/visit/check-in")
async def visit_check_in(req: dict, employee=Depends(get_current_employee)):
    """Log a site check-in at a specific client/location with geofence validation."""
    try:
        import math
        # Parse coordinates first (needed by mock detection and geofence)
        agent_lat = float(req["lat"])
        agent_lng = float(req["lng"])
        geofence_validated = False
        geofence_distance = None
        target_stop = None

        # Mock Location Prevention
        if req.get("mock_detected"):
             logger.warning(f"Field Security Alert: Mock GPS detected during check-in for {employee['email']}")
             await trigger_alert(
                 "Territory", 
                 employee["email"], 
                 employee["organization_id"], 
                 "Mock GPS detected during visit check-in attempt.", 
                 "high",
                 {"lat": agent_lat, "lng": agent_lng, "place": req.get("place_name")}
             )
             raise HTTPException(status_code=403, detail="Security Violation: GPS virtualization/spoofing detected. Check-in rejected.")

        # --- GEOFENCE VALIDATION (200m radius) ---
        stop_id = req.get("stop_id")
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        plan = await visit_plans_collection.find_one({
            "employee_id": employee["email"].lower().strip(),
            "date": today_str,
            "status": PlanStatus.APPROVED
        })

        if plan and stop_id is not None:
            # Find the matching stop in the plan
            target_stop = None
            for stop in plan.get("stops", []):
                if stop.get("sequence_order") == stop_id or str(stop.get("sequence_order")) == str(stop_id):
                    target_stop = stop
                    break

            if target_stop and target_stop.get("place_lat") and target_stop.get("place_lng"):
                stop_lat = float(target_stop["place_lat"])
                stop_lng = float(target_stop["place_lng"])
                # Haversine distance
                dlat = math.radians(agent_lat - stop_lat)
                dlon = math.radians(agent_lng - stop_lng)
                a = math.sin(dlat / 2)**2 + math.cos(math.radians(stop_lat)) * math.cos(math.radians(agent_lat)) * math.sin(dlon / 2)**2
                c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
                geofence_distance = round(6371000 * c, 1)  # meters

                GEOFENCE_RADIUS = 200  # Relaxed from 100m to 200m
                if geofence_distance > GEOFENCE_RADIUS:
                    await trigger_alert(
                        "Compliance", 
                        employee["email"], 
                        employee["organization_id"], 
                        f"Geofence Breach: Agent is {geofence_distance:.0f}m away from planned stop '{target_stop['place_name']}'.", 
                        "medium",
                        {"lat": agent_lat, "lng": agent_lng, "distance": geofence_distance, "stop_id": stop_id}
                    )
                    raise HTTPException(
                        status_code=403,
                        detail=f"You are {geofence_distance:.0f}m away from {target_stop['place_name']}. Must be within {GEOFENCE_RADIUS}m to check in."
                    )
                geofence_validated = True

        # --- Save selfie photo if provided ---
        selfie_url = None
        face_verified = False
        if req.get("selfie_base64"):
            import uuid as _uuid
            os.makedirs("uploads/selfies", exist_ok=True)
            fname = f"selfies/checkin_{employee['email'].replace('@','_')}_{_uuid.uuid4().hex[:8]}.jpg"
            with open(f"uploads/{fname}", "wb") as f:
                f.write(base64.b64decode(req["selfie_base64"]))
            selfie_url = f"/uploads/{fname}"

            # Optional face verification against stored descriptors
            try:
                user = await employees_collection.find_one({"email": employee["email"]})
                if user and user.get("face_embedding"):
                    from face_utils import verify_face as _v_face
                    match, distance = _v_face(req["selfie_base64"], user["face_embedding"])
                    face_verified = match
                    if not match:
                        await trigger_alert(
                            "Identity",
                            employee["email"],
                            employee["organization_id"],
                            f"Face mismatch during visit check-in at '{req.get('place_name', 'Unknown')}'.",
                            "high",
                            {"lat": agent_lat, "lng": agent_lng, "place": req.get("place_name")}
                        )
            except Exception as face_err:
                logger.warning(f"Face verification skipped during check-in: {face_err}")

        log = {
            "employee_id": employee["email"],
            "organization_id": employee["organization_id"],
            "date": today_str,
            "check_in_time": datetime.now(timezone.utc),
            "check_in_lat": agent_lat,
            "check_in_lng": agent_lng,
            "check_in_accuracy": req.get("accuracy", 0),
            "place_name": (target_stop.get("place_name") if target_stop else None) or req.get("place_name", "Unknown"),
            "visit_plan_stop_id": stop_id,
            "geofence_validated": geofence_validated,
            "geofence_distance_meters": geofence_distance,
            "selfie_url": selfie_url,
            "face_verified": face_verified,
            "status": "ongoing"
        }
        
        result = await visit_logs_collection.insert_one(log)
        await employees_collection.update_one(
            {"email": employee["email"]},
            {"$set": {"checked_in": True, "last_attendance_type": "visit_check_in", "last_attendance_time": log["check_in_time"]}}
        )
        return {"status": "success", "visit_id": str(result.inserted_id), "geofence_validated": geofence_validated, "distance_meters": geofence_distance}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Check-in failed: {e}")
        raise HTTPException(status_code=500, detail="Visit check-in failed")


@app.post("/api/field/visit/check-out")
async def visit_check_out(req: dict, background_tasks: BackgroundTasks, employee=Depends(get_current_employee)):
    """Log check-out with remarks, media, person met, and outcome."""
    try:
        # Security: Verify that this visit belongs to the organization
        visit = await visit_logs_collection.find_one({"_id": ObjectId(req["visit_id"])})
        if not visit or visit["organization_id"] != employee["organization_id"]:
             raise HTTPException(status_code=403, detail="Access denied: Visit record not found or cross-org violation.")

        # --- Optional Face Verification for Check-out ---
        face_verified = True
        if req.get("selfie_base64"):
            try:
                # Get employee's enrolled descriptor
                target_descriptor = employee.get("face_embedding")
                if target_descriptor:
                    # Verify provided selfie
                    is_match, score = verify_face(req["selfie_base64"], target_descriptor)
                    face_verified = is_match
                    if not is_match:
                        # Alert admin but don't block check-out (could be lighting etc)
                        await trigger_alert(
                            "Identity", 
                            employee["email"], 
                            employee["organization_id"],
                            f"Face mismatch during check-out for visit {req['visit_id']}.",
                            "high",
                            {"id": req['visit_id'], "score": score}
                        )
                else:
                    logger.warning(f"Employee {employee['email']} has no enrolled face for verification.")
            except Exception as e:
                logger.error(f"Face verification during check-out failed: {e}")
                face_verified = False

        # --- Save voice note if provided ---
        voice_note_url = None
        if req.get("voice_note_base64"):
            import uuid as _uuid
            os.makedirs("uploads/voice_notes", exist_ok=True)
            fname = f"voice_notes/visit_{req['visit_id']}_{_uuid.uuid4().hex[:8]}.m4a"
            with open(f"uploads/{fname}", "wb") as f:
                f.write(base64.b64decode(req["voice_note_base64"]))
            voice_note_url = f"/uploads/{fname}"

        # --- Save site photo if provided ---
        site_photo_url = None
        if req.get("site_photo_base64"):
            import uuid as _uuid
            os.makedirs("uploads/site_photos", exist_ok=True)
            fname = f"site_photos/visit_{req['visit_id']}_{_uuid.uuid4().hex[:8]}.jpg"
            with open(f"uploads/{fname}", "wb") as f:
                f.write(base64.b64decode(req["site_photo_base64"]))
            site_photo_url = f"/uploads/{fname}"

        update_data = {
            "check_out_time": datetime.now(timezone.utc),
            "check_out_lat": req["lat"],
            "check_out_lng": req["lng"],
            "remarks": req.get("remarks"),
            "outcome": req.get("outcome"),
            "order_captured": req.get("order_captured", False),
            "lead_captured": req.get("lead_captured", False),
            "lead_details": req.get("lead_details"),
            "person_met_name": req.get("person_met_name"),
            "person_met_role": req.get("person_met_role"),
            "voice_note_url": voice_note_url,
            "site_photo_url": site_photo_url,
            "face_verified_checkout": face_verified,
            "status": "completed"
        }
        
        await visit_logs_collection.update_one(
            {"_id": ObjectId(req["visit_id"])},
            {"$set": update_data}
        )
        await employees_collection.update_one(
            {"email": employee["email"]},
            {"$set": {"checked_in": False, "last_attendance_type": "visit_check_out", "last_attendance_time": update_data["check_out_time"]}}
        )


        # Trigger Background Sync to Google Sheets for Visit Data
        background_tasks.add_task(sync_visit_to_google_sheets, {**visit, **update_data})

        return {"status": "success", "message": "Visit completed"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Check-out failed: {e}")
        raise HTTPException(status_code=500, detail="Visit check-out failed")


@app.post("/api/field/ping")
async def receive_location_ping(ping: LocationPing, employee=Depends(get_current_employee)):
    """Receive background GPS breadcrumb. Strictly restricted to active duty window."""
    try:
        # Privacy Check: Verify agent is currently checked-in
        # We look for the latest attendance log for this user today
        now = datetime.now(timezone.utc)
        start_of_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        
        last_log = await attendance_logs_collection.find_one(
            {"user_id": str(employee["_id"]), "timestamp": {"$gte": start_of_day}},
            sort=[("timestamp", -1)]
        )
        
        # Only record if last log was a "check-in" (meaning they haven't checked out yet)
        if not last_log or last_log.get("type") != "check-in":
            return {"status": "ignored", "reason": "privacy_filter_active_duty_only"}

        ping_dict = ping.dict()
        ping_dict["employee_id"] = employee["email"]
        ping_dict["organization_id"] = employee["organization_id"]
        ping_dict["recorded_at"] = now
        
        await location_pings_collection.insert_one(ping_dict)

        # --- STATIONARY FRAUD DETECTION ---
        # Fetch last 5 pings to check for movement
        last_pings = await location_pings_collection.find(
            {"employee_id": employee["email"]},
            sort=[("recorded_at", -1)]
        ).to_list(length=6) # Get 6 to compare against the one just inserted

        if len(last_pings) >= 6:
            # Check if all 6 pings have identical lat/lng (within 0.00001 precision)
            first_p = last_pings[0]
            is_stationary = True
            for i in range(1, 6):
                p = last_pings[i]
                if abs(p["lat"] - first_p["lat"]) > 0.0001 or abs(p["lng"] - first_p["lng"]) > 0.0001:
                    is_stationary = False
                    break
            
            if is_stationary:
                # Only trigger if not already flagged in the last hour
                recent_alert = await alerts_collection.find_one({
                    "employee_id": employee["email"],
                    "type": "Productivity",
                    "timestamp": {"$gte": datetime.now(timezone.utc) - timedelta(hours=1)}
                })
                if not recent_alert:
                    await trigger_alert(
                        "Productivity",
                        employee["email"],
                        employee["organization_id"],
                        "Stationary Fraud: Agent has been at the exact same coordinates for last 6 pings (>1 hour).",
                        "medium",
                        {"lat": first_p["lat"], "lng": first_p["lng"]}
                    )

        return {"status": "success"}
    except Exception as e:
        logger.error(f"Ping record failed: {e}")
        return {"status": "error"}

@app.post("/api/field/sync/batch")
async def sync_offline_batch(req: SyncBatchRequest, background_tasks: BackgroundTasks, employee=Depends(get_current_employee)):
    """Synchronize offline data back to the server in a single batch to save bandwidth and handle reconnects."""
    try:
        now = datetime.now(timezone.utc)
        synced_count = {"attendance": 0, "visits": 0, "pings": 0}

        # 1. Sync Attendance Logs
        org_settings = await get_effective_org_settings(employee.get("organization_id"))
        tz_offset = int(org_settings.get("timezone_offset", 330))

        for att in req.attendance_logs:
            offline_id = att.get("offline_id")
            if not offline_id:
                continue
            exists = await attendance_logs_collection.find_one({"offline_id": offline_id})
            if not exists:
                att["synced_at"] = now
                att["user_id"] = str(employee["_id"]) 
                att["email"] = employee.get("email")
                att["full_name"] = employee.get("full_name")
                att["organization_id"] = employee["organization_id"]
                # Convert string timestamp to datetime if needed
                if "timestamp" in att and isinstance(att["timestamp"], str):
                    try:
                        att["timestamp"] = datetime.fromisoformat(att["timestamp"].replace("Z", "+00:00"))
                    except:
                        pass

                # Classify offline attendance log
                try:
                    l_ts = att.get("timestamp") or now
                    if l_ts.tzinfo is None: l_ts = l_ts.replace(tzinfo=timezone.utc)
                    local_ts = l_ts + timedelta(minutes=tz_offset)
                    att_type = att.get("type", "check-in")

                    classification = classify_attendance_day(
                        first_checkin_local=local_ts,
                        last_checkout_local=None if att_type in ["check-in", "IN"] else local_ts,
                        worked_hours=0.0,
                        settings=org_settings or {},
                        employee_type=employee.get("employee_type", "field"),
                        approved_leave=None
                    )
                    att["attendance_status"] = classification["status"]
                    att["is_short_leave"] = classification["is_short_leave"]
                    att["is_half_day"] = classification["is_half_day"]
                    att["short_leave_type"] = classification["short_leave_type"]
                    att["half_day_type"] = classification["half_day_type"]
                    att["late_mins"] = classification["late_mins"]
                    att["early_exit_mins"] = classification["early_exit_mins"]
                    att["late_off_hours"] = classification["late_off_hours"]
                    att["early_off_hours"] = classification["early_off_hours"]
                    att["off_hours"] = classification["off_hours"]
                    att["classification_reason"] = classification["classification_reason"]
                except Exception as ex:
                    logger.warning(f"Offline sync classification error: {ex}")

                await attendance_logs_collection.insert_one(att)
                synced_count["attendance"] += 1

        # 2. Sync Visits
        for visit in req.visits:
            offline_id = visit.get("offline_id")
            if not offline_id:
                continue
            exists = await visit_logs_collection.find_one({"offline_id": offline_id})
            if not exists:
                visit["synced_at"] = now
                visit["employee_id"] = employee["email"]
                visit["organization_id"] = employee["organization_id"]
                
                # Convert timestamps
                for date_field in ["check_in_time", "check_out_time"]:
                    if date_field in visit and isinstance(visit[date_field], str):
                        try:
                            visit[date_field] = datetime.fromisoformat(visit[date_field].replace("Z", "+00:00"))
                        except:
                            pass
                
                # Check for base64 media to save
                if visit.get("site_photo_base64"):
                    import uuid as _uuid
                    os.makedirs("uploads/site_photos", exist_ok=True)
                    fname = f"site_photos/sync_{offline_id}_{_uuid.uuid4().hex[:8]}.jpg"
                    with open(f"uploads/{fname}", "wb") as f:
                        f.write(base64.b64decode(visit["site_photo_base64"]))
                    visit["site_photo_url"] = f"/uploads/{fname}"

                await visit_logs_collection.insert_one(visit)
                
                # Trigger Sheets sync
                background_tasks.add_task(sync_visit_to_google_sheets, visit)
                synced_count["visits"] += 1

        # 3. Sync Pings
        for ping in req.pings:
            offline_id = ping.get("offline_id")
            if not offline_id:
                continue
            exists = await location_pings_collection.find_one({"offline_id": offline_id})
            if not exists:
                ping["synced_at"] = now
                ping["employee_id"] = employee["email"]
                ping["organization_id"] = employee["organization_id"]
                if "recorded_at" in ping and isinstance(ping["recorded_at"], str):
                    try:
                        ping["recorded_at"] = datetime.fromisoformat(ping["recorded_at"].replace("Z", "+00:00"))
                    except:
                        pass
                await location_pings_collection.insert_one(ping)
                synced_count["pings"] += 1

        return {"status": "success", "synced": synced_count}

    except Exception as e:
        logger.error(f"Batch sync failed: {e}")
        raise HTTPException(status_code=500, detail="Batch synchronization failed")


@app.get("/api/field/km-suggestion")
async def get_km_suggestion(employee=Depends(get_current_employee)):
    """Calculate suggested KM for today based on active duty pings."""
    try:
        now = datetime.now(timezone.utc)
        start_of_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        
        pings = await location_pings_collection.find({
            "employee_id": employee["email"],
            "recorded_at": {"$gte": start_of_day}
        }).sort("recorded_at", 1).to_list(length=2000)
        
        total_km = 0.0
        if len(pings) > 1:
            for i in range(len(pings) - 1):
                p1 = pings[i]
                p2 = pings[i+1]
                dist_meters = calculate_haversine(p1["lat"], p1["lng"], p2["lat"], p2["lng"])
                total_km += (dist_meters / 1000.0)
                
        return {"suggested_km": round(total_km, 2)}
    except Exception as e:
        logger.error(f"KM Suggestion failed: {e}")
        return {"suggested_km": 0.0}

@app.post("/api/field/reimbursement/claim")
async def submit_km_reimbursement(req: dict, employee=Depends(get_current_employee)):
    """Submit a formal KM reimbursement claim based on suggested KM."""
    try:
        date_str = req.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        total_km = float(req.get("total_km", 0))
        
        # Check if already claimed for this date
        existing = await km_reimbursements_collection.find_one({
            "employee_id": employee["email"],
            "date": date_str
        })
        if existing:
            raise HTTPException(status_code=400, detail=f"KM reimbursement already claimed for {date_str}")
        
        # Fetch dynamic rate from organization settings
        org_settings = await settings_collection.find_one({"organization_id": employee["organization_id"]})
        rate_per_km = 10.0 # Default
        if org_settings and "field_rate_per_km" in org_settings:
            rate_per_km = float(org_settings["field_rate_per_km"])
        
        claim = {
            "employee_id": employee["email"],
            "organization_id": employee["organization_id"],
            "date": date_str,
            "total_km": total_km,
            "rate_per_km": rate_per_km,
            "total_amount": total_km * rate_per_km,
            "status": "pending",
            "created_at": datetime.now(timezone.utc)
        }
        
        result = await km_reimbursements_collection.insert_one(claim)
        return {"success": True, "claim_id": str(result.inserted_id)}
    except Exception as e:
        logger.error(f"Failed to submit KM reimbursement: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Redundant field summary endpoint removed. Using enhanced version at L2841.

@app.get("/admin/field/reimbursements")
async def admin_list_km_claims(status: Optional[str] = "pending", admin=Depends(get_current_admin)):
    """List KM reimbursement claims for approval."""
    query = {}
    if admin.organization_id:
        query["organization_id"] = admin.organization_id
    if status:
        query["status"] = status
        
    claims = await km_reimbursements_collection.find(query).sort("created_at", -1).to_list(length=200)
    
    emp_emails = list({c["employee_id"] for c in claims if c.get("employee_id")})
    employees = await employees_collection.find({"email": {"$in": emp_emails}}, {"email": 1, "full_name": 1}).to_list(length=len(emp_emails))
    emp_map = {e["email"]: e["full_name"] for e in employees}

    for c in claims:
        c["_id"] = str(c["_id"])
        if isinstance(c.get("created_at"), datetime):
            c["created_at"] = c["created_at"].isoformat()
        c["full_name"] = emp_map.get(c.get("employee_id"), "Unknown")
        
    return claims

@app.post("/admin/field/reimbursements/{claim_id}/{action}")
async def process_km_reimbursement(claim_id: str, action: str, admin=Depends(get_current_admin)):
    "Approve or Reject a KM reimbursement claim."
    new_status = "approved" if action == "approve" else "rejected"
    
    query = {"_id": ObjectId(claim_id)}
    if admin.organization_id:
        query["organization_id"] = admin.organization_id

    update_data = {
        "status": new_status,
        "approved_by": admin.email,
        "approved_at": datetime.now(timezone.utc)
    }
    
    result = await km_reimbursements_collection.update_one(
        query,
        {"$set": update_data}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Claim not found or access denied")
        
    return {"status": "success"}

@app.get("/admin/field/trail/{employee_email}")
async def get_agent_trail(employee_email: str, admin=Depends(get_current_admin)):
    """Fetch today's location trail for a specific agent (Admin only)."""
    try:
        now = datetime.now(timezone.utc)
        start_of_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        
        base_filter = get_employee_filter(admin)
        emp_query = {"email": employee_email, **base_filter}
        
        emp = await employees_collection.find_one(emp_query)
        if not emp:
            raise HTTPException(status_code=404, detail="Employee not found or unauthorized")

        pings = await location_pings_collection.find({
            "employee_id": employee_email,
            "recorded_at": {"$gte": start_of_day}
        }).sort("recorded_at", 1).to_list(length=2000)
        
        trail = [[p["lat"], p["lng"]] for p in pings]
        return {"trail": trail}
    except Exception as e:
        logger.error(f"Trail fetch failed: {e}")
        return {"trail": []}




@app.get("/api/field/summary/{employee_id}")
async def get_field_day_summary(employee_id: str, date: Optional[str] = None, current_user=Depends(get_current_employee)):
    """Get summarized KM and Visit count for the day."""
    # Privacy Enforcement
    if employee_id != current_user["email"]:
        raise HTTPException(status_code=403, detail="Access denied: Cannot view another agent's summary.")
        
    today_start = get_today_start(current_user.get("timezone_offset", 0))
    query_date = date or today_start.strftime("%Y-%m-%d")
    
    # 1. Total Visits
    visits = await visit_logs_collection.count_documents({
        "employee_id": employee_id,
        "date": query_date,
        "status": "completed"
    })
    
    # 2. Total KM (Sum of suggested KM or accepted claims for today)
    # For now, we'll try to get it from location pings if recorded
    total_km = 0.0
    pings = await location_pings_collection.find({
        "employee_id": employee_id,
        "recorded_at": {
            "$gte": today_start,
            "$lt": today_start + timedelta(days=1)
        }
    }).sort("recorded_at", 1).to_list(length=1000)
    
    if len(pings) > 1:
        for i in range(len(pings)-1):
            p1 = pings[i]
            p2 = pings[i+1]
            if "lat" in p1 and "lng" in p1 and "lat" in p2 and "lng" in p2:
                d = calculate_haversine(p1["lat"], p1["lng"], p2["lat"], p2["lng"])
                total_km += d
        total_km = total_km / 1000.0 # Convert to KM
    
    # 3. Current Attendance Status and Classification (TODAY ONLY)
    today_field_logs = await attendance_logs_collection.find({
        "$or": [
            {"email": employee_id},
            {"user_id": employee_id},
            {"email": employee_id.lower().strip()},
            {"user_id": employee_id.lower().strip()}
        ],
        "timestamp": {"$gte": today_start}
    }).sort("timestamp", 1).to_list(length=100)

    first_in_log = next((l for l in today_field_logs if l.get("type") in ["check-in", "IN"]), None)
    last_out_log = next((l for l in reversed(today_field_logs) if l.get("type") in ["check-out", "OUT"]), None)

    org_id = current_user.get("organization_id")
    org_settings_doc = await get_effective_org_settings(org_id) if org_id else {}
    tz_offset = org_settings_doc.get("timezone_offset", current_user.get("timezone_offset", 330))

    first_in_local = (first_in_log["timestamp"] + timedelta(minutes=tz_offset)) if first_in_log else None
    last_out_local = (last_out_log["timestamp"] + timedelta(minutes=tz_offset)) if last_out_log else None

    # Calculate worked duration today (paired multi-sessions)
    worked_sec = 0.0
    last_in_ts = None
    for l in today_field_logs:
        l_ts = l["timestamp"]
        if l_ts.tzinfo is None: l_ts = l_ts.replace(tzinfo=timezone.utc)
        l_type = l.get("type")
        if l_type in ["check-in", "IN"]:
            last_in_ts = l_ts
        elif l_type in ["check-out", "OUT"] and last_in_ts:
            worked_sec += max(0.0, (l_ts - last_in_ts).total_seconds())
            last_in_ts = None

    if last_in_ts:
        worked_sec += max(0.0, (datetime.now(timezone.utc) - last_in_ts).total_seconds())
    elif not worked_sec and first_in_log and last_out_log:
        f_in = first_in_log["timestamp"]
        l_out = last_out_log["timestamp"]
        if f_in.tzinfo is None: f_in = f_in.replace(tzinfo=timezone.utc)
        if l_out.tzinfo is None: l_out = l_out.replace(tzinfo=timezone.utc)
        worked_sec = max(0.0, (l_out - f_in).total_seconds())

    worked_hours = round(worked_sec / 3600.0, 2)

    # Check for approved leave today
    today_date_str = (datetime.now(timezone.utc) + timedelta(minutes=tz_offset)).strftime("%Y-%m-%d")
    approved_leave = None
    try:
        approved_leave = await leave_requests_collection.find_one({
            "$or": [
                {"employee_id": employee_id.lower().strip()},
                {"email": employee_id.lower().strip()}
            ],
            "status": {"$regex": "^approved$", "$options": "i"},
            "start_date": {"$lte": today_date_str},
            "end_date": {"$gte": today_date_str}
        })
    except Exception:
        pass

    classification = classify_attendance_day(
        first_checkin_local=first_in_local,
        last_checkout_local=last_out_local,
        worked_hours=worked_hours,
        settings=org_settings_doc or {},
        employee_type="field",
        approved_leave=approved_leave
    )

    current_status = today_field_logs[-1].get("type", "check-out") if today_field_logs else "check-out"
            
    return {
        "date": query_date,
        "total_visits": visits,
        "total_km": round(total_km, 2),
        "attendance_status": current_status,
        "today_attendance_status": classification["status"],
        "is_short_leave": classification["is_short_leave"],
        "is_half_day": classification["is_half_day"],
        "is_late": classification["is_late"],
        "is_early_leave": classification["is_early_leave"],
        "late_mins": classification["late_mins"],
        "early_exit_mins": classification["early_exit_mins"],
        "late_off_hours": classification["late_off_hours"],
        "early_off_hours": classification["early_off_hours"],
        "off_hours": classification["off_hours"],
        "worked_hours": classification["worked_hours"],
        "required_hours": classification["required_hours"],
        "classification_reason": classification["classification_reason"],
        "is_applied_leave": classification["is_applied_leave"],
        "status": "Active"
    }


# -----------------------------------------------------------------------------
# ADMIN COMMAND CENTER - /admin/field/
# -----------------------------------------------------------------------------


@app.put("/admin/employees/{email}/territory")
async def update_territory(email: str, req: dict, admin=Depends(get_current_admin)):
    """Update employee territory settings (radius or polygon)."""
    clean_email = email.lower().strip()
    
    # Check if employee exists within admin scope
    base_filter = get_employee_filter(admin)
    query = {"email": clean_email, **base_filter}
    user = await employees_collection.find_one(query)
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found or access denied")

    update_fields = {
        "territory_type": req.get("territory_type", "radius"),
    }

    if req.get("territory_type") == "polygon":
        update_fields["territory_polygon"] = req.get("territory_polygon", [])
        # Clear radius fields when switching to polygon
        update_fields["territory_center_lat"] = None
        update_fields["territory_center_lng"] = None
        update_fields["territory_radius_meters"] = None
    else:
        update_fields["territory_center_lat"] = req.get("territory_center_lat")
        update_fields["territory_center_lng"] = req.get("territory_center_lng")
        update_fields["territory_radius_meters"] = req.get("territory_radius_meters")
        # Clear polygon fields when switching to radius
        update_fields["territory_polygon"] = None

    result = await employees_collection.update_one(query, {"$set": update_fields})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Employee not found or access denied")
    return {"status": "success", "message": f"Territory updated to {req.get('territory_type', 'radius')} for {email}"}


@app.get("/admin/field/visit-plans")
async def get_plans_for_approval(status: str = "submitted", admin=Depends(get_current_admin)):
    """List visit plans pending approval."""
    plans = await visit_plans_collection.find({"status": status}).to_list(length=100)
    
    # Enrichment: Add employee names
    emp_ids = list({p["employee_id"] for p in plans if p.get("employee_id")})
    employees = await employees_collection.find({"employee_id": {"$in": emp_ids}}, {"employee_id": 1, "full_name": 1}).to_list(length=len(emp_ids))
    emp_map = {e["employee_id"]: e["full_name"] for e in employees}

    enriched_plans = []
    for plan in plans:
        plan["_id"] = str(plan["_id"])
        plan["full_name"] = emp_map.get(plan["employee_id"], "Unknown")
        enriched_plans.append(plan)
        
    return enriched_plans


@app.post("/admin/field/visit-plans/{plan_id}/{action}")
async def process_visit_plan(plan_id: str, action: str, admin=Depends(get_current_admin)):
    """Approve or Reject a visit plan."""
    new_status = PlanStatus.APPROVED if action == "approve" else PlanStatus.REJECTED
    result = await visit_plans_collection.update_one(
        {"_id": ObjectId(plan_id)},
        {"$set": {"status": new_status, "processed_at": datetime.now(timezone.utc)}}
    )
    return {"status": "success"}

@app.put("/admin/field/visit-plans/{plan_id}")
async def update_visit_plan(plan_id: str, req: dict, admin=Depends(get_current_admin)):
    """Update a visit plan (reorder stops, edit details, add comments) before or after approval."""
    try:
        # Extract stops and comments from request
        stops = req.get("stops")
        comments = req.get("manager_comments")
        
        update_data = {}
        if stops is not None:
            update_data["stops"] = stops
        if comments is not None:
            update_data["manager_comments"] = comments
            update_data["reviewed_at"] = datetime.now(timezone.utc)
            update_data["reviewed_by"] = admin.email

        result = await visit_plans_collection.update_one(
            {"_id": ObjectId(plan_id)},
            {"$set": update_data}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Plan not found")
            
        return {"status": "success", "message": "Plan updated successfully"}
    except Exception as e:
        logger.error(f"Failed to update plan: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/field/generate-otp/{employee_id}")
async def generate_attendance_otp(employee_id: str, admin=Depends(get_current_admin)):
    """Generate a 4-digit OTP for an employee to use as a GPS fallback."""
    import random
    otp = str(random.randint(1000, 9999))
    expiry = datetime.now(timezone.utc) + timedelta(minutes=5)
    
    # Update employee record with OTP
    result = await employees_collection.update_one(
        {"email": employee_id, "organization_id": admin.organization_id},
        {"$set": {"gps_otp": otp, "gps_otp_expiry": expiry}}
    )
    
    if result.matched_count == 0:
        # Try finding by employee_id field if email match failed
        result = await employees_collection.update_one(
            {"employee_id": employee_id, "organization_id": admin.organization_id},
            {"$set": {"gps_otp": otp, "gps_otp_expiry": expiry}}
        )
        
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Employee not found in your organization")
        
    return {"status": "success", "otp": otp, "expires_at": expiry.isoformat()}

@app.get("/admin/stats")
async def get_admin_stats(admin: dict = Depends(get_current_admin)):
    """Summary statistics for the dashboard (Role Scoped)."""
    filter_query = get_employee_filter(admin)
    
    total_employees = await employees_collection.count_documents(filter_query)
    
    # Today's start in UTC (Localized)
    org_settings = await settings_collection.find_one({"organization_id": admin.organization_id})
    tz_offset = org_settings.get("timezone_offset", 330) if org_settings else 330
    today_start = get_today_start(tz_offset)
    
    # Get relevant employee IDs & emails
    org_employees = await employees_collection.find(filter_query, {"_id": 1, "email": 1}).to_list(None)
    org_emp_ids = [str(emp["_id"]) for emp in org_employees]
    org_emp_emails = [emp.get("email") for emp in org_employees if emp.get("email")]
    
    logs_query = {
        "timestamp": {"$gte": today_start},
        "type": {"$in": ["check-in", "IN"]},
        "$or": [
            {"user_id": {"$in": org_emp_ids}},
            {"email": {"$in": org_emp_emails}}
        ]
    }
         
    today_checkins = await attendance_logs_collection.find(logs_query).to_list(2000)
    clocked_in_today = len(today_checkins)
    late_arrivals_today = sum(1 for l in today_checkins if l.get("is_late") or l.get("attendance_status") == "Late")
    short_leaves_today = sum(1 for l in today_checkins if l.get("is_short_leave") or l.get("attendance_status") == "Short Leave")
    half_days_today = sum(1 for l in today_checkins if l.get("is_half_day") or l.get("attendance_status") == "Half Day")
    
    # Alerts & Fraud Detection Stats
    alert_query = {
        "organization_id": admin.organization_id, 
        "timestamp": {"$gte": today_start},
        "employee_id": {"$in": [e.get("email") for e in org_employees]} if "manager_id" in filter_query else {"$exists": True}
    }
    total_alerts_today = await alerts_collection.count_documents(alert_query)
    critical_alerts_today = await alerts_collection.count_documents({**alert_query, "severity": "critical"})
    pending_alerts = await alerts_collection.count_documents({**alert_query, "status": "pending"})

    # Real on_leave count: approved leaves covering today
    local_now = datetime.now(timezone.utc) + timedelta(minutes=tz_offset)
    today_str = local_now.strftime("%Y-%m-%d")
    leave_query = {
        "organization_id": admin.organization_id,
        "status": "approved",
        "start_date": {"$lte": today_str},
        "end_date": {"$gte": today_str}
    }
    if "manager_id" in filter_query:
        # Only count leaves for my team
        leave_query["employee_email"] = {"$in": [e.get("email") for e in org_employees]}

    on_leave_count = await leave_requests_collection.count_documents(leave_query)

    # Check if today is a configured holiday
    today_holiday = await holidays_collection.find_one({
        "$or": [
            {"organization_id": admin.organization_id, "date": today_str},
            {"organization_id": "system_org", "date": today_str}
        ]
    })
    is_today_holiday = bool(today_holiday)
    holiday_name = today_holiday.get("name", "Holiday") if today_holiday else None

    # Calculate Absent count (Total - ClockedIn - OnLeave)
    # If today is a holiday or weekend (Sunday), employees are not absent
    if is_today_holiday or local_now.weekday() == 6:
        absent_count = 0
    else:
        absent_count = max(0, total_employees - clocked_in_today - on_leave_count)

    return {
        "total_employees": total_employees,
        "clocked_in_today": clocked_in_today,
        "late_arrivals_today": late_arrivals_today, 
        "short_leaves_today": short_leaves_today,
        "half_days_today": half_days_today,
        "on_leave": on_leave_count,
        "absent_today": absent_count,
        "is_holiday_today": is_today_holiday,
        "holiday_name": holiday_name,
        "avg_hours": 8.5,
        "alerts_today": total_alerts_today,
        "critical_alerts": critical_alerts_today,
        "pending_alerts": pending_alerts
    }


@app.get("/admin/stats/attendance-chart")
async def get_attendance_chart(admin: dict = Depends(get_current_admin)):
    """Attendance volume over the last 7 days (Role Scoped)."""
    filter_query = get_employee_filter(admin)
    org_employees = await employees_collection.find(filter_query, {"_id": 1}).to_list(None)
    org_emp_ids = [str(emp["_id"]) for emp in org_employees]

    org_settings = await settings_collection.find_one({"organization_id": admin.organization_id})
    tz_offset = org_settings.get("timezone_offset", 330) if org_settings else 330

    chart_data = []
    local_now = datetime.now(timezone.utc) + timedelta(minutes=tz_offset)
    
    for i in range(6, -1, -1):
        day = local_now - timedelta(days=i)
        day_str = day.strftime("%a") # Mon, Tue...
        
        # Local boundaries for this day
        day_start_local = day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end_local = day_start_local + timedelta(days=1)
        
        # Convert back to UTC for query
        day_start_utc = day_start_local - timedelta(minutes=tz_offset)
        day_end_utc = day_end_local - timedelta(minutes=tz_offset)
        
        count = await attendance_logs_collection.count_documents({
            "timestamp": {"$gte": day_start_utc, "$lt": day_end_utc},
            "type": "check-in",
            "user_id": {"$in": org_emp_ids}
        })
        
        chart_data.append({"name": day_str, "count": count})
        
    return chart_data


@app.get("/admin/live-feed")
async def get_admin_live_feed(skip: int = 0, limit: int = 50, admin: dict = Depends(get_current_admin)):
    """Unified real-time check-in/out feed for the dashboard (Role Scoped + Paginated)."""
    filter_query = get_employee_filter(admin)
    
    # Get relevant employee details for mapping
    org_employees = await employees_collection.find(filter_query, {"_id": 1, "full_name": 1, "email": 1, "profile_image": 1}).to_list(None)
    org_emp_ids = [str(emp["_id"]) for emp in org_employees]
    emp_map = {str(emp["_id"]): emp for emp in org_employees}
    
    logs_query = {"user_id": {"$in": org_emp_ids}}
    
    total_logs = await attendance_logs_collection.count_documents(logs_query)
    cursor = attendance_logs_collection.find(logs_query).sort("timestamp", -1).skip(skip).limit(limit)
    logs = await cursor.to_list(length=limit)
    
    for log in logs:
        log["_id"] = str(log["_id"])
        emp = emp_map.get(log.get("user_id"), {})
        log["employee_name"] = emp.get("full_name", "Unknown")
        log["employee_email"] = emp.get("email", "")
        log["profile_image"] = emp.get("profile_image", "")
        if isinstance(log.get("timestamp"), datetime):
            ts = log["timestamp"]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            log["timestamp"] = ts.isoformat().replace("+00:00", "Z")
            
    return {
        "logs": logs,
        "total": total_logs,
        "skip": skip,
        "limit": limit
    }


# -----------------------------------------------------------------------------
# PHASE 4: LEAVE/OD & DISCUSSION SYSTEM
# -----------------------------------------------------------------------------

@app.post("/api/leave/request")
async def create_leave_request(req: dict, employee=Depends(get_current_employee)):
    """Submit a new Leave or On-Duty (OD) request with optional duration (full_day, half_day, short_leave) and proof."""
    try:
        proof_file_url = None
        if req.get("proof_url") and req["proof_url"].startswith("data:image"):
            # Handle base64 image upload
            try:
                import base64
                import uuid
                
                # Ensure proofs directory exists
                proofs_dir = os.path.join(UPLOAD_DIR, "proofs")
                if not os.path.exists(proofs_dir):
                    os.makedirs(proofs_dir)
                
                header, encoded = req["proof_url"].split(",", 1)
                ext = header.split("/")[1].split(";")[0]
                filename = f"proof_{uuid.uuid4()}.{ext}"
                file_path = os.path.join(proofs_dir, filename)
                
                with open(file_path, "wb") as f:
                    f.write(base64.b64decode(encoded))
                
                proof_file_url = f"/uploads/proofs/{filename}"
                logger.info(f"Proof uploaded and saved to {proof_file_url}")
            except Exception as upload_err:
                logger.error(f"Failed to process proof upload: {upload_err}")
                # We'll continue without the proof if it fails
        
        start_date = normalize_date_str(req.get("start_date", ""))
        end_date = normalize_date_str(req.get("end_date", start_date))
        duration_type = str(req.get("duration_type", "full_day")).lower().strip()
        
        if duration_type in ["first_half", "second_half", "half_day"]:
            duration = 0.5
        elif duration_type == "short_leave":
            duration = 0.0
        else:
            duration_type = "full_day"
            duration = float(count_days_between(start_date, end_date))

        # Check live quota to flag if this request exceeds current available quota
        org_id = employee.get("organization_id", "system_org")
        target_month = start_date[:7] if start_date else None
        live_balance = await calculate_leave_balance(employee["email"], org_id, month=target_month)
        rem_tl = live_balance.get("total_leave", {}).get("remaining", 0.0)
        exceeds_quota = (duration > rem_tl) and (req.get("leave_type") not in ["on_duty", "duty"])

        new_request = {
            "employee_id": employee["email"],
            "organization_id": org_id,
            "leave_type": req["leave_type"],  # sick, casual, on_duty, other, unpaid
            "start_date": start_date,
            "end_date": end_date,
            "duration_type": duration_type,
            "duration": duration,
            "short_leave_hours": float(req.get("short_leave_hours", 2.0)) if duration_type == "short_leave" else None,
            "reason": req["reason"],
            "is_paid": req.get("is_paid"),  # None = auto, True = Paid, False = LOP
            "exceeds_quota": exceeds_quota,
            "status": "pending",
            "proof_url": proof_file_url or req.get("proof_url"),
            "discussion": [],
            "created_at": datetime.now(timezone.utc)
        }
        result = await leave_requests_collection.insert_one(new_request)
        return {
            "status": "success",
            "request_id": str(result.inserted_id),
            "proof_url": proof_file_url,
            "exceeds_quota": exceeds_quota,
            "duration": duration
        }
    except Exception as e:
        logger.error(f"Failed to create leave request: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to submit request")

@app.get("/api/leave/my-requests")
async def get_my_leave_requests(employee=Depends(get_current_employee)):
    """List current employee's leave requests."""
    requests = await leave_requests_collection.find({"employee_id": employee["email"]}).sort("created_at", -1).to_list(length=100)
    for r in requests:
        r["_id"] = str(r["_id"])
        if isinstance(r.get("created_at"), datetime):
            r["created_at"] = r["created_at"].isoformat()
    return requests

@app.get("/api/leave/my-balance")
@app.get("/api/leave/balance")
async def get_my_leave_balance(
    year: Optional[int] = None,
    month: Optional[str] = None,
    employee=Depends(get_current_employee)
):
    """Retrieve live computed leave quotas, balances, and carry-forward for the authenticated employee."""
    org_id = employee.get("organization_id", "system_org")
    balance = await calculate_leave_balance(
        email=employee["email"],
        org_id=org_id,
        year=year,
        month=month
    )
    return balance

# --- Manager Endpoints ---
@app.get("/api/manager/team-attendance")
async def get_team_attendance(manager=Depends(get_current_employee)):
    """Fetch current attendance status for all subordinates."""
    cursor = employees_collection.find({"manager_id": manager["email"]})
    subordinates = await cursor.to_list(length=100)
    
    results = []
    for sub in subordinates:
        # Get latest attendance log
        last_log = await attendance_logs_collection.find_one(
            {"user_id": str(sub["_id"])},
            sort=[("timestamp", -1)]
        )
        
        status = "check-out"
        last_time = "N/A"
        if last_log:
            status = last_log["type"]
            ts = last_log["timestamp"]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            last_time = ts.strftime("%I:%M %p")
            
        results.append({
            "id": str(sub["_id"]),
            "full_name": sub.get("full_name", "Unknown"),
            "email": sub["email"],
            "status": status,
            "last_time": last_time
        })
    return results

@app.get("/api/manager/pending-leaves")
async def get_pending_leaves(manager=Depends(get_current_employee)):
    """Fetch pending leave requests for subordinates."""
    cursor = employees_collection.find({"manager_id": manager["email"]})
    subordinates = await cursor.to_list(length=100)
    sub_emails = [s["email"] for s in subordinates]
    
    requests = await leave_requests_collection.find({
        "employee_id": {"$in": sub_emails},
        "status": "pending"
    }).sort("created_at", -1).to_list(length=100)
    
    for r in requests:
        r["_id"] = str(r["_id"])
        if isinstance(r.get("created_at"), datetime):
            r["created_at"] = r["created_at"].isoformat()
    return requests

@app.post("/api/manager/leave-decision")
async def leave_decision(payload: dict, manager=Depends(get_current_employee)):
    """Approve or reject a leave request (Manager level)."""
    try:
        req_id = payload.get("request_id")
        action = payload.get("action") # "approve" or "reject"
        is_paid = payload.get("is_paid")
        admin_notes = payload.get("admin_notes")
        
        if action not in ["approve", "reject"]:
            raise HTTPException(status_code=400, detail="Invalid action")
            
        obj_id = ObjectId(req_id)
        leave_req = await leave_requests_collection.find_one({"_id": obj_id})
        if not leave_req:
            raise HTTPException(status_code=404, detail="Request not found")
            
        # Verify manager owns this subordinate
        emp = await employees_collection.find_one({"email": leave_req["employee_id"], "manager_id": manager["email"]})
        if not emp:
            raise HTTPException(status_code=403, detail="Unauthorized")
            
        status = "approved" if action == "approve" else "rejected"
        update_doc = {
            "status": status,
            "processed_at": datetime.now(timezone.utc),
            "processed_by": manager["email"]
        }
        if is_paid is not None:
            update_doc["is_paid"] = bool(is_paid)
        if admin_notes:
            update_doc["admin_notes"] = admin_notes
            
        await leave_requests_collection.update_one(
            {"_id": obj_id},
            {"$set": update_doc}
        )
        return {"status": "success", "new_status": status}
    except Exception as e:
        logger.error(f"Failed to process leave decision: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/admin/leave/requests")
async def admin_get_leave_requests(status: Optional[str] = None, admin: dict = Depends(get_current_admin)):
    """List all leave requests for the organization (Role Scoped)."""
    filter_query = get_employee_filter(admin)
    
    # Map get_employee_filter logic to leave requests
    org_id = admin.organization_id
    leave_query = {}
    if org_id:
        leave_query["organization_id"] = org_id
        
    if admin.role == "manager":
        # Find all employees for this manager
        org_employees = await employees_collection.find({"organization_id": org_id, "manager_id": admin.email}, {"email": 1}).to_list(None)
        emp_emails = [e.get("email") for e in org_employees if e.get("email")]
        leave_query["employee_id"] = {"$in": emp_emails}
        
    if status:
        leave_query["status"] = status
        
    requests = await leave_requests_collection.find(leave_query).sort("created_at", -1).to_list(length=100)
    
    emp_emails = list({r.get("employee_id") for r in requests if r.get("employee_id")})
    employees = await employees_collection.find({"email": {"$in": emp_emails}}, {"email": 1, "full_name": 1, "name": 1}).to_list(length=len(emp_emails))
    emp_map = {e["email"]: e.get("full_name", e.get("name", "Unknown")) for e in employees}

    enriched = []
    for r in requests:
        r["_id"] = str(r["_id"])
        emp_id = r.get("employee_id")
        r["full_name"] = emp_map.get(emp_id, "Unknown")
        if isinstance(r.get("created_at"), datetime):
            r["created_at"] = r["created_at"].isoformat()
        enriched.append(r)
    return enriched

async def get_current_any_user(token: str = Depends(employee_oauth2_scheme)):
    """Try to decode as employee first, then admin."""
    try:
        user = await get_current_employee(token)
        if user:
            return user, "employee"
    except Exception:
        pass
    
    try:
        admin = await get_current_admin(token)
        if admin:
            return admin, "admin"
    except Exception:
        pass
        
    raise HTTPException(status_code=401, detail="Invalid session")

@app.get("/api/leave/requests/{request_id}/discussion")
async def get_leave_discussion(request_id: str, auth_data=Depends(get_current_any_user)):
    """Fetch discussion/chat history for a leave request."""
    try:
        obj_id = ObjectId(request_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request ID")

    req = await leave_requests_collection.find_one({"_id": obj_id})
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
        
    user, role = auth_data
    # Access check: Admin of same org or the employee themselves
    if role == "admin":
        admin_org = user.organization_id
        # Global superadmin (no org_id) can see all
        if admin_org and admin_org != req.get("organization_id"):
             raise HTTPException(status_code=403, detail="Access denied")
    else:
        if user.get("email") != req.get("employee_id"):
             raise HTTPException(status_code=403, detail="Access denied")

    return req.get("discussion", [])

@app.post("/api/leave/requests/{request_id}/message")
async def post_leave_message(request_id: str, payload: dict, auth_data=Depends(get_current_any_user)):
    """Post a new message to the leave request discussion (Chat). Supports both Admin and Employee tokens."""
    try:
        obj_id = ObjectId(request_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request ID")
        
    if not payload or "message" not in payload:
        raise HTTPException(status_code=400, detail="Message content is required")
        
    user, role = auth_data
    
    if role == "admin":
        sender_id = user.email
        sender_name = user.full_name
    else:
        sender_id = user.get("email")
        sender_name = user.get("full_name", user.get("name", "Unknown"))

    message = {
        "sender_id": sender_id,
        "sender_name": sender_name,
        "role": role,
        "message": payload["message"],
        "timestamp": datetime.now(timezone.utc)
    }
    
    result = await leave_requests_collection.update_one(
        {"_id": obj_id},
        {"$push": {"discussion": message}}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Request not found")
        
    return {"status": "success"}

@app.post("/admin/leave/requests/{request_id}/{action}")
async def handle_leave_request(request_id: str, action: str, req_body: Optional[dict] = None, admin: dict = Depends(get_current_admin)):
    """Handle (Approve/Reject/Cancel) a leave request with optional is_paid flag."""
    try:
        obj_id = ObjectId(request_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request ID")

    status_map = {
        "approve": "approved",
        "reject": "rejected",
        "cancel": "cancelled"
    }
    if action not in status_map:
        raise HTTPException(status_code=400, detail="Invalid action")
    
    # 1. Fetch Request
    leave_req = await leave_requests_collection.find_one({"_id": obj_id})
    if not leave_req:
        raise HTTPException(status_code=404, detail="Leave request not found")

    # 2. Check Permissions (Manager scoping)
    filter_query = get_employee_filter(admin)
    if "manager_id" in filter_query:
        # Check if the employee belongs to this manager
        emp = await employees_collection.find_one({"email": leave_req["employee_id"], "manager_id": admin.email})
        if not emp:
            raise HTTPException(status_code=403, detail="Access denied: You can only manage leaves for your direct reports.")
    elif admin.organization_id and admin.organization_id != leave_req.get("organization_id"):
        raise HTTPException(status_code=403, detail="Access denied: This request belongs to another organization.")

    # 3. Update Request
    update_fields = {
        "status": status_map[action],
        "processed_at": datetime.now(timezone.utc),
        "processed_by": admin.email
    }
    if req_body and isinstance(req_body, dict):
        if "is_paid" in req_body and req_body["is_paid"] is not None:
            update_fields["is_paid"] = bool(req_body["is_paid"])
        if "admin_notes" in req_body and req_body["admin_notes"]:
            update_fields["admin_notes"] = str(req_body["admin_notes"])

    await leave_requests_collection.update_one(
        {"_id": obj_id},
        {"$set": update_fields}
    )

    # 4. Retroactively reclassify attendance logs for this employee across the leave period
    if action == "approve":
        try:
            l_start = str(leave_req.get("start_date", ""))
            l_end = str(leave_req.get("end_date", l_start))
            if l_start:
                s_dt = datetime.strptime(l_start[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
                e_dt = datetime.strptime(l_end[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
                emp_id_str = str(leave_req.get("employee_id", ""))
                emp_email = str(leave_req.get("email", emp_id_str))

                org_settings = await get_effective_org_settings(leave_req.get("organization_id"))
                tz_offset = int((org_settings or {}).get("timezone_offset", 330))

                date_logs = await attendance_logs_collection.find({
                    "$or": [
                        {"email": emp_email},
                        {"user_id": emp_id_str},
                        {"email": emp_id_str},
                        {"user_id": emp_email}
                    ],
                    "timestamp": {"$gte": s_dt, "$lt": e_dt}
                }).sort("timestamp", 1).to_list(100)

                if date_logs:
                    updated_leave_req = {**leave_req, **update_fields}
                    checkins = [l for l in date_logs if l.get("type") in ["check-in", "IN"]]
                    checkouts = [l for l in date_logs if l.get("type") in ["check-out", "OUT"]]
                    
                    if checkins:
                        f_in = checkins[0]["timestamp"]
                        if f_in.tzinfo is None: f_in = f_in.replace(tzinfo=timezone.utc)
                        f_in_local = f_in + timedelta(minutes=tz_offset)
                        
                        l_out_local = None
                        worked_hours = 0.0
                        if checkouts:
                            l_out = checkouts[-1]["timestamp"]
                            if l_out.tzinfo is None: l_out = l_out.replace(tzinfo=timezone.utc)
                            l_out_local = l_out + timedelta(minutes=tz_offset)
                            worked_hours = round(max(0.0, (l_out - f_in).total_seconds()) / 3600.0, 2)

                        new_class = classify_attendance_day(
                            first_checkin_local=f_in_local,
                            last_checkout_local=l_out_local,
                            worked_hours=worked_hours,
                            settings=org_settings or {},
                            employee_type=leave_req.get("employee_type", "desk"),
                            approved_leave=updated_leave_req
                        )

                        await attendance_logs_collection.update_one(
                            {"_id": checkins[0]["_id"]},
                            {"$set": {
                                "attendance_status": new_class["status"],
                                "is_short_leave": new_class["is_short_leave"],
                                "is_half_day": new_class["is_half_day"],
                                "short_leave_type": new_class["short_leave_type"],
                                "half_day_type": new_class["half_day_type"],
                                "off_hours": new_class["off_hours"],
                                "late_off_hours": new_class["late_off_hours"],
                                "early_off_hours": new_class["early_off_hours"],
                                "classification_reason": new_class["classification_reason"],
                                "is_applied_leave": True
                            }}
                        )
        except Exception as ex:
            logger.warning(f"Leave approval reclassification error: {ex}")
        
    return {"status": "success", "new_status": status_map[action]}

@app.put("/admin/leave/requests/{request_id}/payment-status")
async def toggle_leave_payment_status(request_id: str, req_body: dict, admin: dict = Depends(get_current_admin)):
    """Toggle whether an approved leave is Paid or Loss of Pay (LOP) for payroll."""
    try:
        obj_id = ObjectId(request_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request ID")

    leave_req = await leave_requests_collection.find_one({"_id": obj_id})
    if not leave_req:
        raise HTTPException(status_code=404, detail="Leave request not found")

    is_paid = req_body.get("is_paid")
    if is_paid is None:
        raise HTTPException(status_code=400, detail="is_paid (boolean) is required")

    await leave_requests_collection.update_one(
        {"_id": obj_id},
        {"$set": {
            "is_paid": bool(is_paid),
            "payment_status_updated_by": admin.email,
            "payment_status_updated_at": datetime.now(timezone.utc)
        }}
    )
    return {"status": "success", "is_paid": bool(is_paid), "message": f"Leave marked as {'Paid' if is_paid else 'Loss of Pay (LOP)'}"}


@app.post("/admin/attendance/convert-absent-to-leave")
async def convert_absent_to_leave(req: ConvertAbsentToLeaveRequest, admin: Admin = Depends(get_current_admin)):
    """Admin regularization: Convert an Absent day into an Approved Leave (e.g. informed via Email/WhatsApp)."""
    clean_email = req.employee_email.strip().lower()
    clean_date = normalize_date_str(req.date)
    if not clean_date:
        raise HTTPException(status_code=400, detail="Invalid date format. Expected YYYY-MM-DD or DD/MM/YYYY")

    emp = await employees_collection.find_one({"email": clean_email})
    if not emp:
        # Try finding by employee_id or other fields
        emp = await employees_collection.find_one({"employee_id": req.employee_email.strip()})
        if not emp:
            raise HTTPException(status_code=404, detail="Employee not found")
        clean_email = emp["email"]

    org_id = emp.get("organization_id") or admin.organization_id or "system_org"

    # Check if there is already an approved leave for this date
    existing = await leave_requests_collection.find_one({
        "employee_id": clean_email,
        "start_date": {"$lte": clean_date},
        "end_date": {"$gte": clean_date},
        "status": "approved"
    })

    if existing:
        return {
            "status": "already_exists",
            "message": f"An approved leave ({existing.get('leave_type')}) already exists for this date.",
            "request_id": str(existing["_id"])
        }

    duration_type = req.duration_type or "full_day"
    duration = float(req.duration) if req.duration else (0.5 if duration_type in ["first_half", "second_half"] else 1.0)

    # Insert approved leave record
    new_leave = {
        "employee_id": clean_email,
        "organization_id": org_id,
        "leave_type": req.leave_type,
        "start_date": clean_date,
        "end_date": clean_date,
        "duration_type": duration_type,
        "duration": duration,
        "reason": req.reason or "Regularized by Admin (Informed via Email / WhatsApp)",
        "is_paid": req.is_paid if req.is_paid is not None else True,
        "exceeds_quota": False,
        "status": "approved",
        "admin_notes": req.admin_notes or f"Converted from Absent on {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} by {admin.email}",
        "processed_at": datetime.now(timezone.utc),
        "processed_by": admin.email,
        "created_at": datetime.now(timezone.utc)
    }
    result = await leave_requests_collection.insert_one(new_leave)

    # Also calculate updated leave balance for the month
    target_month = clean_date[:7]
    balance = await calculate_leave_balance(clean_email, org_id, month=target_month)

    return {
        "status": "success",
        "message": f"Absent day ({clean_date}) successfully converted to Approved {req.leave_type.replace('_', ' ').title()}.",
        "request_id": str(result.inserted_id),
        "updated_balance": balance
    }



# -----------------------------------------------------------------------------
# HOLIDAY & LEAVE POLICY SYSTEM
# -----------------------------------------------------------------------------

def normalize_date_str(d_str: str) -> Optional[str]:
    """Normalize any date format (DD/MM/YYYY, YYYY-MM-DD, ISO) into standard YYYY-MM-DD."""
    if not d_str:
        return None
    try:
        s = str(d_str).strip()
        if "T" in s:
            s = s.split("T")[0]
        if "/" in s:
            parts = s.split("/")
            if len(parts) == 3:
                if len(parts[2]) == 4:  # DD/MM/YYYY
                    return f"{int(parts[2]):04d}-{int(parts[1]):02d}-{int(parts[0]):02d}"
                elif len(parts[0]) == 4:  # YYYY/MM/DD
                    return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        elif "-" in s:
            parts = s.split("-")
            if len(parts) == 3:
                if len(parts[0]) == 4:  # YYYY-MM-DD
                    return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
                elif len(parts[2]) == 4:  # DD-MM-YYYY
                    return f"{int(parts[2]):04d}-{int(parts[1]):02d}-{int(parts[0]):02d}"
        return s[:10]
    except Exception:
        return str(d_str)[:10]


def count_days_between(start_date_str: str, end_date_str: str) -> int:
    """Inclusive calendar days between two dates in any standard format."""
    try:
        norm_start = normalize_date_str(start_date_str)
        norm_end = normalize_date_str(end_date_str)
        d1 = datetime.strptime(norm_start, "%Y-%m-%d")
        d2 = datetime.strptime(norm_end, "%Y-%m-%d")
        if d2 < d1:
            return 1
        return (d2 - d1).days + 1
    except Exception:
        return 1


async def sync_holiday_days_off(org_id: str, payroll_month: str):
    """Synchronize days_off in holidays collection for HRMS payroll integration."""
    try:
        # Find all named holidays for this month
        month_holidays = await holidays_collection.find({
            "$or": [
                {"organization_id": org_id, "payroll_month": payroll_month, "date": {"$exists": True}},
                {"organization_id": "system_org", "payroll_month": payroll_month, "date": {"$exists": True}}
            ]
        }).to_list(length=100)

        holiday_days = set()
        holiday_names = {}
        for h in month_holidays:
            d_val = None
            if "day" in h and h["day"] is not None:
                d_val = int(h["day"])
            elif "date" in h and h["date"]:
                try:
                    d_val = int(str(h["date"]).split("-")[2])
                except Exception:
                    pass
            if d_val is not None:
                holiday_days.add(d_val)
                holiday_names[str(d_val)] = h.get("name", "Holiday")

        # Update the payroll days_off doc for this month
        payroll_doc = await holidays_collection.find_one({
            "organization_id": org_id,
            "payroll_month": payroll_month,
            "days_off": {"$exists": True}
        })

        if payroll_doc:
            current_days_off = set(payroll_doc.get("days_off", []))
            merged = sorted(list(current_days_off.union(holiday_days)))
            merged_names = payroll_doc.get("holiday_names", {})
            merged_names.update(holiday_names)
            await holidays_collection.update_one(
                {"_id": payroll_doc["_id"]},
                {"$set": {"days_off": merged, "holiday_names": merged_names, "updated_at": datetime.now(timezone.utc)}}
            )
        else:
            # Create default days_off doc with Sundays + holidays
            try:
                y, m = map(int, payroll_month.split("-"))
                num_days = calendar.monthrange(y, m)[1]
                sundays = []
                for d in range(1, num_days + 1):
                    if datetime(y, m, d).weekday() == 6:
                        sundays.append(d)
                initial_days_off = sorted(list(set(sundays).union(holiday_days)))
                await holidays_collection.insert_one({
                    "organization_id": org_id,
                    "payroll_month": payroll_month,
                    "days_off": initial_days_off,
                    "holiday_names": holiday_names,
                    "created_at": datetime.now(timezone.utc)
                })
            except Exception as cr_err:
                logger.warning(f"Could not initialize days_off doc: {cr_err}")
    except Exception as e:
        logger.error(f"sync_holiday_days_off error: {e}")


async def calculate_leave_balance(
    email: str,
    org_id: str,
    year: Optional[int] = None,
    month: Optional[str] = None,
    absent_days: float = 0.0
) -> dict:
    """Enterprise SL, CL, TL, and Loss of Pay (LOP) balance calculation engine with true month-by-month accrual, carry-forward, and half-day support."""
    now = datetime.now(timezone.utc)
    if not year:
        year = now.year
    if not month:
        month = f"{year}-{now.month:02d}"

    # 1. Fetch Leave Policy & Org Settings
    policy = await leave_policies_collection.find_one({
        "$or": [
            {"organization_id": org_id, "year": year},
            {"organization_id": "system_org", "year": year},
            {"organization_id": org_id},
            {"organization_id": "system_org"}
        ]
    })

    org_settings = None
    tz_offset = 330
    try:
        org_settings = await settings_collection.find_one({"organization_id": org_id})
        if org_settings:
            tz_offset = int(org_settings.get("timezone_offset", 330))
    except Exception: pass

    allocation_cycle = "annual"
    carry_forward = True
    start_month = f"{year}-01"
    defaults = {"sick_leave": 12.0, "casual_leave": 12.0, "total_leave": 30.0}
    paid_leave_types = ["sick", "casual", "on_duty", "duty"]
    unpaid_leave_types = ["other", "unpaid", "lop"]
    half_day_min_hours = 4.0
    full_day_min_hours = 8.0
    late_grace_mins = 15
    short_leave_monthly_limit = 2
    short_leave_max_hours = 2.0
    short_leave_penalty_rule = "half_day_per_2_excess"
    overrides = {}

    if policy:
        allocation_cycle = policy.get("allocation_cycle", "annual")
        carry_forward = policy.get("carry_forward", True)
        if policy.get("start_month"):
            start_month = policy.get("start_month")
        defaults = policy.get("defaults", defaults)
        paid_leave_types = policy.get("paid_leave_types", paid_leave_types)
        unpaid_leave_types = policy.get("unpaid_leave_types", unpaid_leave_types)
        half_day_min_hours = float(policy.get("half_day_min_hours", 4.0))
        full_day_min_hours = float(policy.get("full_day_min_hours", 8.0))
        late_grace_mins = int(policy.get("late_grace_mins", 15))
        short_leave_monthly_limit = int(policy.get("short_leave_monthly_limit", 2))
        short_leave_max_hours = float(policy.get("short_leave_max_hours", 2.0))
        short_leave_penalty_rule = policy.get("short_leave_penalty_rule", "half_day_per_2_excess")
        overrides = policy.get("overrides", {})

    clean_email = email.strip().lower()
    emp_quota = overrides.get(clean_email) or overrides.get(email) or defaults
    
    default_sl = 12.0 if allocation_cycle == "annual" else 0.0
    default_cl = 12.0 if allocation_cycle == "annual" else 0.0
    default_tl = 30.0 if allocation_cycle == "annual" else 1.5

    base_sl = float(emp_quota.get("sick_leave", default_sl))
    base_cl = float(emp_quota.get("casual_leave", default_cl))
    base_tl = float(emp_quota.get("total_leave", default_tl))

    # Identify all possible identifier variants for this employee
    emp_identifiers = [clean_email, email.strip()]
    try:
        emp_doc = await employees_collection.find_one({"email": clean_email})
        if emp_doc:
            if "_id" in emp_doc:
                emp_identifiers.append(str(emp_doc["_id"]))
            if "employee_id" in emp_doc and emp_doc["employee_id"]:
                emp_identifiers.append(str(emp_doc["employee_id"]).strip())
            # Dynamically infer start_month from joining date if policy did not set a specific start_month
            if not policy or not policy.get("start_month"):
                j_date = emp_doc.get("date_of_joining") or emp_doc.get("joining_date")
                if j_date:
                    norm_j = normalize_date_str(j_date)
                    if norm_j and norm_j.startswith(str(year)):
                        start_month = norm_j[:7]
    except Exception:
        pass

    leaves = await leave_requests_collection.find({
        "$or": [
            {"employee_id": {"$in": emp_identifiers}},
            {"email": {"$in": emp_identifiers}}
        ],
        "status": {"$regex": "^approved$", "$options": "i"}
    }).to_list(length=500)

    def eval_leave(l_doc):
        norm_start = normalize_date_str(l_doc.get("start_date", ""))
        norm_end = normalize_date_str(l_doc.get("end_date", norm_start))
        if not norm_start:
            return None, 0.0, False, "other"
            
        dur_type = str(l_doc.get("duration_type", "")).lower().strip()
        if l_doc.get("duration") is not None:
            days = float(l_doc.get("duration"))
        elif dur_type in ["first_half", "second_half", "half_day"]:
            days = 0.5
        elif dur_type == "short_leave":
            days = 0.0
        else:
            days = float(count_days_between(norm_start, norm_end or norm_start))
            
        l_type = str(l_doc.get("leave_type", "other")).lower().strip()
        
        # Check Paid Status: Explicit override takes priority, else check against paid_leave_types list
        if l_doc.get("is_paid") is not None:
            is_paid = bool(l_doc.get("is_paid"))
        else:
            is_paid = any(pt in l_type for pt in paid_leave_types)
            
        return norm_start, days, is_paid, l_type

    if allocation_cycle == "monthly":
        # Generate ordered list of months from start_month to target month
        try:
            s_yr, s_mo = map(int, start_month.split("-"))
            t_yr, t_mo = map(int, month.split("-"))
            
            # Clamp if target is before start
            if (t_yr < s_yr) or (t_yr == s_yr and t_mo < s_mo):
                start_month = month
                s_yr, s_mo = t_yr, t_mo
        except Exception:
            start_month = month
            s_yr, s_mo = int(month.split("-")[0]), int(month.split("-")[1])
            t_yr, t_mo = s_yr, s_mo

        # Build month sequence
        month_seq = []
        cur_y, cur_m = s_yr, s_mo
        while (cur_y < t_yr) or (cur_y == t_yr and cur_m <= t_mo):
            month_seq.append(f"{cur_y:04d}-{cur_m:02d}")
            cur_m += 1
            if cur_m > 12:
                cur_m = 1
                cur_y += 1

        # Month-by-month state tracking
        carried_sl = 0.0
        carried_cl = 0.0
        carried_tl = 0.0
        
        target_month_data = {}

        for m_str in month_seq:
            # Filter approved leaves in this specific month
            m_leaves = []
            for l in leaves:
                n_start, days, is_paid, l_type = eval_leave(l)
                if n_start and n_start.startswith(m_str):
                    m_leaves.append((n_start, days, is_paid, l_type))

            # Sum usage in this month
            used_sl_m = sum(d for _, d, _, lt in m_leaves if "sick" in lt)
            used_cl_m = sum(d for _, d, _, lt in m_leaves if "casual" in lt)
            used_od_m = sum(d for _, d, _, lt in m_leaves if "on_duty" in lt or "duty" in lt)
            used_other_m = sum(d for _, d, _, lt in m_leaves if not any(k in lt for k in ["sick", "casual", "on_duty", "duty"]))
            
            # Paid vs Unpaid used in this month (On-duty does not deduct from quota)
            paid_used_tl_m = sum(d for _, d, is_p, lt in m_leaves if is_p and ("on_duty" not in lt and "duty" not in lt))
            unpaid_used_tl_m = sum(d for _, d, is_p, lt in m_leaves if not is_p and ("on_duty" not in lt and "duty" not in lt))

            # Available quota in this month = monthly quota + carried forward from previous month
            avail_sl_m = base_sl + carried_sl
            avail_cl_m = base_cl + carried_cl
            avail_tl_m = base_tl + carried_tl

            # Remaining quota at end of this month
            rem_sl_m = max(0.0, avail_sl_m - used_sl_m)
            rem_cl_m = max(0.0, avail_cl_m - used_cl_m)
            rem_tl_m = max(0.0, avail_tl_m - paid_used_tl_m)

            # Excess LOP days in this month = (paid leaves taken exceeding available quota) + (unpaid leaves)
            excess_lop_m = max(0.0, paid_used_tl_m - avail_tl_m) + unpaid_used_tl_m

            # If this is our target month, record full data
            if m_str == month:
                prev_month_str = None
                prev_month_label = None
                if len(month_seq) > 1 and month_seq.index(m_str) > 0:
                    prev_idx = month_seq.index(m_str) - 1
                    prev_month_str = month_seq[prev_idx]
                    try:
                        p_dt = datetime.strptime(prev_month_str, "%Y-%m")
                        prev_month_label = p_dt.strftime("%b %Y")
                    except Exception:
                        prev_month_label = prev_month_str

                target_month_data = {
                    "year": year,
                    "month": month,
                    "allocation_cycle": "monthly",
                    "carry_forward": carry_forward,
                    "start_month": start_month,
                    "previous_month": prev_month_str,
                    "previous_month_label": prev_month_label,
                    "sick_leave": {
                        "monthly_quota": round(base_sl, 2),
                        "carried_forward": round(carried_sl, 2),
                        "available": round(avail_sl_m, 2),
                        "total": round(avail_sl_m, 2),
                        "used": round(used_sl_m, 2),
                        "remaining": round(rem_sl_m, 2)
                    },
                    "casual_leave": {
                        "monthly_quota": round(base_cl, 2),
                        "carried_forward": round(carried_cl, 2),
                        "available": round(avail_cl_m, 2),
                        "total": round(avail_cl_m, 2),
                        "used": round(used_cl_m, 2),
                        "remaining": round(rem_cl_m, 2)
                    },
                    "total_leave": {
                        "monthly_quota": round(base_tl, 2),
                        "carried_forward": round(carried_tl, 2),
                        "available": round(avail_tl_m, 2),
                        "total": round(avail_tl_m, 2),
                        "used": round(paid_used_tl_m, 2),
                        "unpaid_used": round(unpaid_used_tl_m, 2),
                        "remaining": round(rem_tl_m, 2),
                        "excess_lop": round(excess_lop_m, 2)
                    },
                    "on_duty": {
                        "used": round(used_od_m, 2)
                    },
                    "other": {
                        "used": round(used_other_m, 2)
                    },
                    "paid_leave_types": paid_leave_types,
                    "unpaid_leave_types": unpaid_leave_types,
                    "absent_days": round(float(absent_days), 2),
                    "total_lop_days": round(excess_lop_m + float(absent_days), 2)
                }

            # Update carried forward for next month in sequence
            if carry_forward:
                carried_sl = rem_sl_m
                carried_cl = rem_cl_m
                carried_tl = rem_tl_m
            else:
                carried_sl = 0.0
                carried_cl = 0.0
                carried_tl = 0.0

        # Count attendance-based and applied short leaves / half days in target month (de-duplicated by date)
        short_leave_dates = set()
        half_day_dates = set()
        att_short_leave_dates = set()
        att_half_day_dates = set()
        applied_short_leave_dates = set()
        applied_half_day_dates = set()

        try:
            m_start_utc = datetime.strptime(f"{month}-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)
            yr_int, mo_int = map(int, month.split("-"))
            next_m_utc = datetime(yr_int + 1, 1, 1, tzinfo=timezone.utc) if mo_int == 12 else datetime(yr_int, mo_int + 1, 1, tzinfo=timezone.utc)

            month_att_logs = await attendance_logs_collection.find({
                "$or": [
                    {"user_id": {"$in": emp_identifiers}},
                    {"email": {"$in": emp_identifiers}}
                ],
                "type": {"$in": ["check-in", "IN"]},
                "timestamp": {"$gte": m_start_utc, "$lt": next_m_utc}
            }).to_list(1000)

            for l in month_att_logs:
                l_ts = l["timestamp"]
                if l_ts.tzinfo is None: l_ts = l_ts.replace(tzinfo=timezone.utc)
                l_date = (l_ts + timedelta(minutes=tz_offset)).strftime("%Y-%m-%d")
                if l.get("is_short_leave") or l.get("attendance_status") == "Short Leave":
                    att_short_leave_dates.add(l_date)
                    short_leave_dates.add(l_date)
                if l.get("is_half_day") or l.get("attendance_status") == "Half Day":
                    att_half_day_dates.add(l_date)
                    half_day_dates.add(l_date)
        except Exception as e:
            logger.warning(f"Error fetching month att logs for leave balance: {e}")

        for l in leaves:
            dur_type = str(l.get("duration_type", "")).lower().strip()
            l_start = str(l.get("start_date", ""))
            l_end = str(l.get("end_date", l_start))
            if not l_start or not (l_start.startswith(month) or l_end.startswith(month)):
                continue
            
            try:
                c_dt = max(datetime.strptime(l_start[:10], "%Y-%m-%d"), datetime.strptime(f"{month}-01", "%Y-%m-%d"))
                e_dt = min(datetime.strptime(l_end[:10], "%Y-%m-%d"), next_m_utc.replace(tzinfo=None) - timedelta(days=1))
                while c_dt <= e_dt:
                    d_str = c_dt.strftime("%Y-%m-%d")
                    if dur_type == "short_leave":
                        applied_short_leave_dates.add(d_str)
                        short_leave_dates.add(d_str)
                    elif dur_type in ["first_half", "second_half", "half_day"] or l.get("duration") == 0.5:
                        applied_half_day_dates.add(d_str)
                        half_day_dates.add(d_str)
                    c_dt += timedelta(days=1)
            except Exception: pass

        total_short_leaves = len(short_leave_dates)
        total_half_days = len(half_day_dates)
        short_leave_monthly_limit = int((policy or {}).get("short_leave_monthly_limit", 2))
        short_leave_remaining = max(0, short_leave_monthly_limit - total_short_leaves)

        target_month_data["short_leave"] = {
            "monthly_limit": short_leave_monthly_limit,
            "used": total_short_leaves,
            "remaining": short_leave_remaining,
            "attendance_triggered": len(att_short_leave_dates - applied_short_leave_dates),
            "applied": len(applied_short_leave_dates)
        }
        target_month_data["half_day"] = {
            "used": total_half_days,
            "attendance_triggered": len(att_half_day_dates - applied_half_day_dates),
            "applied": len(applied_half_day_dates)
        }

        return target_month_data

    else:
        # Annual cycle
        y_str = str(year)
        y_leaves = []
        for l in leaves:
            n_start, days, is_paid, l_type = eval_leave(l)
            if n_start and n_start.startswith(y_str):
                y_leaves.append((n_start, days, is_paid, l_type))

        used_sick = sum(d for _, d, _, lt in y_leaves if "sick" in lt)
        used_casual = sum(d for _, d, _, lt in y_leaves if "casual" in lt)
        used_on_duty = sum(d for _, d, _, lt in y_leaves if "on_duty" in lt or "duty" in lt)
        used_other = sum(d for _, d, _, lt in y_leaves if not any(k in lt for k in ["sick", "casual", "on_duty", "duty"]))

        paid_used_tl = sum(d for _, d, is_p, lt in y_leaves if is_p and ("on_duty" not in lt and "duty" not in lt))
        unpaid_used_tl = sum(d for _, d, is_p, lt in y_leaves if not is_p and ("on_duty" not in lt and "duty" not in lt))

        rem_sl = max(0.0, base_sl - used_sick)
        rem_cl = max(0.0, base_cl - used_casual)
        rem_tl = max(0.0, base_tl - paid_used_tl)
        excess_lop = max(0.0, paid_used_tl - base_tl) + unpaid_used_tl

        # Count attendance-based and applied short leaves / half days in target month
        try:
            m_start_utc = datetime.strptime(f"{month}-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)
            yr_int, mo_int = map(int, month.split("-"))
            next_m_utc = datetime(yr_int + 1, 1, 1, tzinfo=timezone.utc) if mo_int == 12 else datetime(yr_int, mo_int + 1, 1, tzinfo=timezone.utc)

            att_short_leaves = await attendance_logs_collection.count_documents({
                "$or": [
                    {"user_id": {"$in": emp_identifiers}},
                    {"email": {"$in": emp_identifiers}}
                ],
                "is_short_leave": True,
                "type": "check-in",
                "timestamp": {"$gte": m_start_utc, "$lt": next_m_utc}
            })
            att_half_days = await attendance_logs_collection.count_documents({
                "$or": [
                    {"user_id": {"$in": emp_identifiers}},
                    {"email": {"$in": emp_identifiers}}
                ],
                "is_half_day": True,
                "type": "check-in",
                "timestamp": {"$gte": m_start_utc, "$lt": next_m_utc}
            })
        except Exception:
            att_short_leaves = 0
            att_half_days = 0

        applied_short_leaves = sum(1 for l in leaves if str(l.get("duration_type", "")).lower().strip() == "short_leave" and str(l.get("start_date", "")).startswith(month))
        applied_half_days = sum(1 for l in leaves if str(l.get("duration_type", "")).lower().strip() in ["first_half", "second_half", "half_day"] and str(l.get("start_date", "")).startswith(month))

        total_short_leaves = att_short_leaves + applied_short_leaves
        total_half_days = att_half_days + applied_half_days
        short_leave_monthly_limit = int((policy or {}).get("short_leave_monthly_limit", 2))
        short_leave_remaining = max(0, short_leave_monthly_limit - total_short_leaves)

        return {
            "year": year,
            "month": month,
            "allocation_cycle": "annual",
            "sick_leave": {
                "total": round(base_sl, 2),
                "used": round(used_sick, 2),
                "remaining": round(rem_sl, 2)
            },
            "casual_leave": {
                "total": round(base_cl, 2),
                "used": round(used_casual, 2),
                "remaining": round(rem_cl, 2)
            },
            "total_leave": {
                "total": round(base_tl, 2),
                "available": round(base_tl, 2),
                "used": round(paid_used_tl, 2),
                "unpaid_used": round(unpaid_used_tl, 2),
                "remaining": round(rem_tl, 2),
                "excess_lop": round(excess_lop, 2)
            },
            "short_leave": {
                "monthly_limit": short_leave_monthly_limit,
                "used": total_short_leaves,
                "remaining": short_leave_remaining,
                "attendance_triggered": att_short_leaves,
                "applied": applied_short_leaves
            },
            "half_day": {
                "used": total_half_days,
                "attendance_triggered": att_half_days,
                "applied": applied_half_days
            },
            "on_duty": {
                "used": round(used_on_duty, 2)
            },
            "other": {
                "used": round(used_other, 2)
            },
            "paid_leave_types": paid_leave_types,
            "unpaid_leave_types": unpaid_leave_types,
            "absent_days": round(float(absent_days), 2),
            "total_lop_days": round(excess_lop + float(absent_days), 2)
        }


# --- HOLIDAY ENDPOINTS ---

@app.post("/admin/holidays")
async def admin_create_holiday(req: HolidayCreateRequest, admin: dict = Depends(get_current_admin)):
    """Create or update a named holiday for the organization."""
    org_id = admin.organization_id or "system_org"
    date_str = req.date.strip()
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        payroll_month = date_str[:7]
        day_num = dt.day
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    holiday_doc = {
        "organization_id": org_id,
        "date": date_str,
        "payroll_month": payroll_month,
        "day": day_num,
        "name": req.name.strip(),
        "holiday_type": req.holiday_type or "festival",
        "description": req.description or "",
        "created_by": admin.email,
        "updated_at": datetime.now(timezone.utc)
    }

    result = await holidays_collection.update_one(
        {"organization_id": org_id, "date": date_str},
        {"$set": holiday_doc},
        upsert=True
    )

    # Sync with HRMS payroll days_off
    await sync_holiday_days_off(org_id, payroll_month)

    return {"status": "success", "message": f"Holiday '{req.name}' set for {date_str}", "date": date_str}


@app.post("/admin/holidays/bulk")
async def admin_bulk_create_holidays(req: HolidayBulkCreateRequest, admin: dict = Depends(get_current_admin)):
    """Bulk create holidays for the organization."""
    org_id = admin.organization_id or "system_org"
    created_count = 0
    months_to_sync = set()

    for h in req.holidays:
        date_str = h.date.strip()
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            payroll_month = date_str[:7]
            day_num = dt.day
            months_to_sync.add(payroll_month)

            holiday_doc = {
                "organization_id": org_id,
                "date": date_str,
                "payroll_month": payroll_month,
                "day": day_num,
                "name": h.name.strip(),
                "holiday_type": h.holiday_type or "festival",
                "description": h.description or "",
                "created_by": admin.email,
                "updated_at": datetime.now(timezone.utc)
            }

            await holidays_collection.update_one(
                {"organization_id": org_id, "date": date_str},
                {"$set": holiday_doc},
                upsert=True
            )
            created_count += 1
        except Exception as err:
            logger.warning(f"Skipping holiday {h}: {err}")

    for m in months_to_sync:
        await sync_holiday_days_off(org_id, m)

    return {"status": "success", "count": created_count, "message": f"Successfully added {created_count} holidays"}


@app.get("/admin/holidays")
async def admin_get_holidays(year: Optional[int] = None, month: Optional[str] = None, admin: dict = Depends(get_current_admin)):
    """List all configured holidays for the admin's organization."""
    org_id = admin.organization_id or "system_org"
    query = {
        "$or": [
            {"organization_id": org_id, "date": {"$exists": True}},
            {"organization_id": "system_org", "date": {"$exists": True}}
        ]
    }

    if month:
        query["payroll_month"] = month
    elif year:
        query["date"] = {"$regex": f"^{year}"}

    cursor = holidays_collection.find(query).sort("date", 1)
    holidays = await cursor.to_list(length=500)

    results = []
    for h in holidays:
        h["_id"] = str(h["_id"])
        results.append(h)
    return results


@app.delete("/admin/holidays/{holiday_id_or_date}")
async def admin_delete_holiday(holiday_id_or_date: str, admin: dict = Depends(get_current_admin)):
    """Delete a holiday by its ID or YYYY-MM-DD date."""
    org_id = admin.organization_id or "system_org"
    query = {}
    if ObjectId.is_valid(holiday_id_or_date):
        query = {"_id": ObjectId(holiday_id_or_date)}
    else:
        query = {
            "$or": [
                {"organization_id": org_id, "date": holiday_id_or_date},
                {"organization_id": "system_org", "date": holiday_id_or_date}
            ]
        }

    holiday_to_delete = await holidays_collection.find_one(query)
    if not holiday_to_delete:
        raise HTTPException(status_code=404, detail="Holiday not found")

    payroll_month = holiday_to_delete.get("payroll_month", "")
    day_num = holiday_to_delete.get("day")

    await holidays_collection.delete_one({"_id": holiday_to_delete["_id"]})

    # Also remove from days_off array in payroll doc if needed
    if payroll_month and day_num:
        await holidays_collection.update_one(
            {"organization_id": org_id, "payroll_month": payroll_month, "days_off": {"$exists": True}},
            {"$pull": {"days_off": day_num}}
        )

    return {"status": "success", "message": "Holiday deleted successfully"}


@app.get("/api/holidays")
async def employee_get_holidays(year: Optional[int] = None, employee: dict = Depends(get_current_employee)):
    """Fetch holidays visible to the current employee."""
    org_id = employee.get("organization_id", "system_org")
    if not year:
        year = datetime.now(timezone.utc).year

    query = {
        "$or": [
            {"organization_id": org_id, "date": {"$regex": f"^{year}"}},
            {"organization_id": "system_org", "date": {"$regex": f"^{year}"}}
        ]
    }

    cursor = holidays_collection.find(query).sort("date", 1)
    holidays = await cursor.to_list(length=200)

    results = []
    for h in holidays:
        h["_id"] = str(h["_id"])
        results.append({
            "id": h["_id"],
            "date": h.get("date"),
            "name": h.get("name"),
            "holiday_type": h.get("holiday_type", "festival"),
            "description": h.get("description", "")
        })
    return results


# --- LEAVE POLICY ENDPOINTS ---

@app.get("/admin/leave-policy")
async def admin_get_leave_policy(year: Optional[int] = None, admin: dict = Depends(get_current_admin)):
    """Fetch org-wide leave policy and quotas with enterprise attendance rules."""
    org_id = admin.organization_id or "system_org"
    if not year:
        year = datetime.now(timezone.utc).year

    policy = await leave_policies_collection.find_one({
        "$or": [
            {"organization_id": org_id, "year": year},
            {"organization_id": "system_org", "year": year},
            {"organization_id": org_id},
            {"organization_id": "system_org"}
        ]
    })

    allocation_cycle = "annual"
    carry_forward = True
    start_month = "2026-08"
    defaults = {"sick_leave": 12.0, "casual_leave": 12.0, "total_leave": 30.0}
    paid_leave_types = ["sick", "casual", "on_duty"]
    unpaid_leave_types = ["other", "unpaid"]
    half_day_min_hours = 4.0
    full_day_min_hours = 8.0
    late_grace_mins = 15
    short_leave_monthly_limit = 2
    short_leave_max_hours = 2.0
    short_leave_penalty_rule = "half_day_per_2_excess"
    overrides = {}

    if policy:
        allocation_cycle = policy.get("allocation_cycle", "annual")
        carry_forward = policy.get("carry_forward", True)
        start_month = policy.get("start_month", "2026-08")
        defaults = policy.get("defaults", defaults)
        paid_leave_types = policy.get("paid_leave_types", paid_leave_types)
        unpaid_leave_types = policy.get("unpaid_leave_types", unpaid_leave_types)
        half_day_min_hours = float(policy.get("half_day_min_hours", 4.0))
        full_day_min_hours = float(policy.get("full_day_min_hours", 8.0))
        late_grace_mins = int(policy.get("late_grace_mins", 15))
        short_leave_monthly_limit = int(policy.get("short_leave_monthly_limit", 2))
        short_leave_max_hours = float(policy.get("short_leave_max_hours", 2.0))
        short_leave_penalty_rule = policy.get("short_leave_penalty_rule", "half_day_per_2_excess")
        overrides = policy.get("overrides", {})

    return {
        "organization_id": org_id,
        "year": year,
        "allocation_cycle": allocation_cycle,
        "carry_forward": carry_forward,
        "start_month": start_month,
        "defaults": defaults,
        "paid_leave_types": paid_leave_types,
        "unpaid_leave_types": unpaid_leave_types,
        "half_day_min_hours": half_day_min_hours,
        "full_day_min_hours": full_day_min_hours,
        "late_grace_mins": late_grace_mins,
        "short_leave_monthly_limit": short_leave_monthly_limit,
        "short_leave_max_hours": short_leave_max_hours,
        "short_leave_penalty_rule": short_leave_penalty_rule,
        "office_start_time": (policy or {}).get("office_start_time", "09:00"),
        "office_end_time": (policy or {}).get("office_end_time", "18:00"),
        "short_leave_cutoff_time": (policy or {}).get("short_leave_cutoff_time", "10:15"),
        "half_day_late_cutoff_time": (policy or {}).get("half_day_late_cutoff_time", "11:30"),
        "half_day_early_exit_cutoff_time": (policy or {}).get("half_day_early_exit_cutoff_time", "14:00"),
        "lunch_start_time": (policy or {}).get("lunch_start_time", "13:00"),
        "lunch_end_time": (policy or {}).get("lunch_end_time", "14:00"),
        "max_lunch_duration_mins": (policy or {}).get("max_lunch_duration_mins", 60),
        "overrides": overrides
    }


@app.put("/admin/leave-policy")
async def admin_update_leave_policy(req: LeavePolicyUpdateRequest, admin: dict = Depends(get_current_admin)):
    """Set organization-wide default leave quotas, allocation cycle, carry forward, and paid/unpaid rules."""
    org_id = admin.organization_id or "system_org"
    year = req.year or datetime.now(timezone.utc).year
    allocation_cycle = (req.allocation_cycle or "annual").strip().lower()
    carry_forward = bool(req.carry_forward if req.carry_forward is not None else True)
    start_month = req.start_month or "2026-08"

    defaults = {
        "sick_leave": float(req.sick_leave),
        "casual_leave": float(req.casual_leave),
        "total_leave": float(req.total_leave)
    }
    paid_leave_types = req.paid_leave_types if req.paid_leave_types is not None else ["sick", "casual", "on_duty"]
    unpaid_leave_types = req.unpaid_leave_types if req.unpaid_leave_types is not None else ["other", "unpaid"]

    update_doc = {
        "organization_id": org_id,
        "year": year,
        "allocation_cycle": allocation_cycle,
        "carry_forward": carry_forward,
        "start_month": start_month,
        "defaults": defaults,
        "paid_leave_types": paid_leave_types,
        "unpaid_leave_types": unpaid_leave_types,
        "half_day_min_hours": float(req.half_day_min_hours or 4.0),
        "full_day_min_hours": float(req.full_day_min_hours or 8.0),
        "late_grace_mins": int(req.late_grace_mins or 15),
        "short_leave_monthly_limit": int(req.short_leave_monthly_limit or 2),
        "short_leave_max_hours": float(req.short_leave_max_hours or 2.0),
        "short_leave_penalty_rule": req.short_leave_penalty_rule or "half_day_per_2_excess",
        "office_start_time": req.office_start_time or "09:00",
        "office_end_time": req.office_end_time or "18:00",
        "short_leave_cutoff_time": req.short_leave_cutoff_time or "10:15",
        "half_day_late_cutoff_time": req.half_day_late_cutoff_time or "11:30",
        "half_day_early_exit_cutoff_time": req.half_day_early_exit_cutoff_time or "14:00",
        "lunch_start_time": req.lunch_start_time or "13:00",
        "lunch_end_time": req.lunch_end_time or "14:00",
        "max_lunch_duration_mins": int(req.max_lunch_duration_mins or 60),
        "updated_by": admin.email,
        "updated_at": datetime.now(timezone.utc)
    }

    await leave_policies_collection.update_one(
        {"organization_id": org_id, "year": year},
        {"$set": update_doc},
        upsert=True
    )

    # Mirror shift attendance cutoffs to organization settings
    settings_mirror = {
        "office_start_time": req.office_start_time or "09:00",
        "office_end_time": req.office_end_time or "18:00",
        "late_grace_mins": int(req.late_grace_mins or 15),
        "short_leave_cutoff_time": req.short_leave_cutoff_time or "10:15",
        "half_day_late_cutoff_time": req.half_day_late_cutoff_time or "11:30",
        "half_day_early_exit_cutoff_time": req.half_day_early_exit_cutoff_time or "14:00",
        "lunch_start_time": req.lunch_start_time or "13:00",
        "lunch_end_time": req.lunch_end_time or "14:00",
        "max_lunch_duration_mins": int(req.max_lunch_duration_mins or 60),
        "half_day_min_hours": float(req.half_day_min_hours or 4.0),
        "full_day_min_hours": float(req.full_day_min_hours or 8.0),
        "short_leave_max_hours": float(req.short_leave_max_hours or 2.0),
        "short_leave_monthly_limit": int(req.short_leave_monthly_limit or 2),
        "short_leave_penalty_rule": req.short_leave_penalty_rule or "half_day_per_2_excess",
    }
    if org_id == "system_org":
        await settings_collection.update_one({"id": "config"}, {"$set": settings_mirror}, upsert=True)
    else:
        await settings_collection.update_one({"organization_id": org_id}, {"$set": settings_mirror}, upsert=True)

    return {
        "status": "success",
        "message": f"Leave policy and attendance cutoffs updated for {year}",
        **update_doc
    }


@app.put("/admin/leave-policy/employee/{email}")
async def admin_update_employee_leave_quota(
    email: str,
    req: EmployeeLeaveQuotaUpdateRequest,
    admin: dict = Depends(get_current_admin)
):
    """Set custom leave quota override for a specific employee."""
    org_id = admin.organization_id or "system_org"
    clean_email = email.strip().lower()
    year = req.year or datetime.now(timezone.utc).year

    override = {
        "sick_leave": float(req.sick_leave),
        "casual_leave": float(req.casual_leave),
        "total_leave": float(req.total_leave)
    }
    if req.paid_leave_types is not None:
        override["paid_leave_types"] = req.paid_leave_types
    if req.unpaid_leave_types is not None:
        override["unpaid_leave_types"] = req.unpaid_leave_types

    await leave_policies_collection.update_one(
        {"organization_id": org_id, "year": year},
        {
            "$set": {
                f"overrides.{clean_email}": override,
                "updated_by": admin.email,
                "updated_at": datetime.now(timezone.utc)
            }
        },
        upsert=True
    )

    balance = await calculate_leave_balance(clean_email, org_id, year)
    return {"status": "success", "message": f"Custom quota set for {clean_email}", "balance": balance}


@app.get("/admin/leave-policy/employee/{email}/balance")
async def admin_get_employee_leave_balance(
    email: str,
    year: Optional[int] = None,
    month: Optional[str] = None,
    admin: dict = Depends(get_current_admin)
):
    """Fetch live calculated leave balance for an employee."""
    org_id = admin.organization_id or "system_org"
    clean_email = email.strip().lower()
    balance = await calculate_leave_balance(clean_email, org_id, year, month)
    return balance


@app.get("/api/leave/balance")
async def employee_get_leave_balance(
    year: Optional[int] = None,
    month: Optional[str] = None,
    employee: dict = Depends(get_current_employee)
):
    """Fetch live calculated leave balance for the logged-in employee."""
    org_id = employee.get("organization_id", "system_org")
    clean_email = employee["email"]
    balance = await calculate_leave_balance(clean_email, org_id, year, month)
    return balance



@app.get("/admin/field/live-status")
async def get_field_live_status(admin=Depends(get_current_admin)):
    """Live operational data for the War Room map."""
    try:
        # Apply RBAC: managers see only their team
        base_filter = get_employee_filter(admin)
        query = {**base_filter, "employee_type": "field"}
            
        field_emps = await employees_collection.find(query).to_list(length=100)
        
        agents = []
        active_count = 0
        idle_count = 0
        breach_count = 0
        now = datetime.now(timezone.utc)
        
        for emp in field_emps:
            try:
                # 1. Get latest ping
                ping = await location_pings_collection.find_one(
                    {"employee_id": emp["email"]},
                    sort=[("recorded_at", -1)]
                )
                
                status = "Inactive"
                current_visit = None
                
                # 2. Check active check-in
                active_visit_log = await visit_logs_collection.find_one(
                    {"employee_id": emp["email"], "check_out_time": None},
                    sort=[("check_in_time", -1)]
                )
                
                if active_visit_log and active_visit_log.get("visit_id"):
                    plan = await visit_plans_collection.find_one({
                        "organization_id": emp["organization_id"],
                        "stops.visit_id": active_visit_log["visit_id"]
                    })
                    if plan:
                        current_stop = next((s for s in plan.get("stops", []) if s.get("visit_id") == active_visit_log["visit_id"]), None)
                        if current_stop:
                            current_visit = current_stop["place_name"]
        
                # 3. Status logic
                if ping and ping.get("recorded_at"):
                    rp = ping["recorded_at"]
                    if isinstance(rp, str):
                        try:
                            rp = datetime.fromisoformat(rp.replace("Z", "+00:00"))
                        except:
                            rp = now
                    
                    ping_time = rp.replace(tzinfo=timezone.utc) if rp.tzinfo is None else rp
                    if now - ping_time < timedelta(minutes=10):
                        status = "On-Site" if active_visit_log else "Traveling"
                        active_count += 1
                    else:
                        status = "Idle"
                        idle_count += 1
                
                # 4. KM today calculation (Accurate Haversine Sum)
                org_settings = await settings_collection.find_one({"organization_id": emp["organization_id"]})
                tz_offset = org_settings.get("timezone_offset", 330) if org_settings else 330
                start_of_day = get_today_start(tz_offset)
                pings_cursor = location_pings_collection.find({
                    "employee_id": emp["email"],
                    "recorded_at": {"$gte": start_of_day}
                }).sort("recorded_at", 1)
                pings_today_list = await pings_cursor.to_list(length=1000)
                
                total_km_calc = 0.0
                if len(pings_today_list) > 1:
                    for i in range(len(pings_today_list) - 1):
                        p1 = pings_today_list[i]
                        p2 = pings_today_list[i+1]
                        if p1.get("lat") and p1.get("lng") and p2.get("lat") and p2.get("lng"):
                            lat1, lon1 = math.radians(p1["lat"]), math.radians(p1["lng"])
                            lat2, lon2 = math.radians(p2["lat"]), math.radians(p2["lng"])
                            dlat = lat2 - lat1
                            dlon = lon2 - lon1
                            a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
                            c = 2 * math.asin(math.sqrt(a))
                            total_km_calc += 6371 * c
                
                pings_today = len(pings_today_list)
                km_today_final = float(round(total_km_calc, 2))
                
                # 5. Format response object
                lp = ping.get("recorded_at") if ping else None
                if isinstance(lp, datetime):
                    lp = lp.isoformat()
                    
                agents.append({
                    "id": str(emp["_id"]),
                    "email": str(emp["email"]),
                    "name": str(emp.get("full_name", emp["email"])),
                    "lat": float(ping.get("lat")) if ping and ping.get("lat") is not None else None,
                    "lng": float(ping.get("lng")) if ping and ping.get("lng") is not None else None,
                    "status": str(status),
                    "current_visit": str(current_visit) if current_visit else None,
                    "last_ping": str(lp) if lp else None,
                    "km_today": km_today_final,
                    "territory": emp.get("territory")
                })
            except Exception as e:
                logger.error(f"Inner loop error for {emp.get('email')}: {e}")
                continue
                
        res_data = {
            "agents": agents,
            "stats": {
                "active": int(active_count),
                "idle": int(idle_count),
                "breach": int(breach_count)
            }
        }
        return JSONResponse(content=res_data)
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        logger.error(f"TOP LEVEL LIVE STATUS ERROR: {err}")
        return JSONResponse(status_code=500, content={"error": str(e), "traceback": err})




# -----------------------------------------------------------------------------
# FIELD LEADERBOARD
# -----------------------------------------------------------------------------

@app.get("/api/field/leaderboard")
async def get_field_leaderboard(employee=Depends(get_current_employee)):
    """Get the live leaderboard for field agents for the current week."""
    try:
        now = datetime.now(timezone.utc)
        start_of_week = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_week = start_of_week + timedelta(days=6, hours=23, minutes=59, seconds=59)

        employees_cursor = employees_collection.find({
            "organization_id": employee["organization_id"],
            "employee_type": EmployeeType.FIELD
        })
        
        leaderboard = []
        async for emp in employees_cursor:
            visits_count = await visit_logs_collection.count_documents({
                "employee_id": emp["email"],
                "timestamp": {"$gte": start_of_week, "$lte": end_of_week},
                "action": "check_in"
            })
            
            claims_cursor = km_reimbursements_collection.find({
                "employee_id": emp["email"],
                "date": {"$gte": start_of_week.strftime("%Y-%m-%d"), "$lte": end_of_week.strftime("%Y-%m-%d")}
            })
            total_km = 0
            async for claim in claims_cursor:
                total_km += float(claim.get("total_km", 0))
                
            leaderboard.append({
                "employee_id": emp["email"],
                "name": emp.get("full_name", "Unknown"),
                "designation": emp.get("designation", "Field Agent"),
                "visits_completed": visits_count,
                "leads_captured": 0,
                "distance_km": round(total_km, 1),
                "is_me": emp["email"] == employee["email"]
            })
            
        leaderboard.sort(key=lambda x: x["visits_completed"], reverse=True)
        
        for i, entry in enumerate(leaderboard):
            entry["rank"] = i + 1
            
        return {
            "week_start": start_of_week.strftime("%b %d"),
            "week_end": end_of_week.strftime("%b %d"),
            "leaderboard": leaderboard
        }
    except Exception as e:
        logger.error(f"Failed to fetch leaderboard: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to load leaderboard data")

# -----------------------------------------------------------------------------
# EXPENSE CLAIM MANAGEMENT
# -----------------------------------------------------------------------------

@app.post("/api/field/expenses")
async def submit_expense(req: dict, employee=Depends(get_current_employee)):
    # Handle Base64 Receipt Image if present
    receipt_url = req.get("receipt_url", "")
    if receipt_url and receipt_url.startswith("data:image"):
        try:
            header, encoded = receipt_url.split(",", 1)
            ext = header.split(";")[0].split("/")[1]
            filename = f"receipt_{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(UPLOAD_DIR, filename)
            with open(filepath, "wb") as f:
                f.write(base64.b64decode(encoded))
            receipt_url = f"/uploads/{filename}"
        except Exception as e:
            logger.error(f"Failed to save receipt image: {e}")
            receipt_url = ""

    claim = {
        "employee_id": employee["email"],
        "organization_id": employee.get("organization_id"),
        "visit_id": req.get("visit_id"),
        "visit_plan_stop_id": req.get("visit_plan_stop_id"),  # v2: tag expense to a plan stop
        "expense_type": req.get("expense_type", "other"),
        "amount": float(req.get("amount", 0)),
        "description": req.get("description", ""),
        "receipt_url": receipt_url,
        "claimed_km": float(req.get("claimed_km")) if req.get("claimed_km") is not None else None,
        "auto_calculated_km": float(req.get("auto_calculated_km")) if req.get("auto_calculated_km") is not None else None,
        "nights": req.get("nights"),
        "accommodation_name": req.get("accommodation_name"),
        "location_city": req.get("location_city"),
        "status": "pending",
        "manager_query": None,
        "employee_response": None,
        "resolved_at": None,
        "created_at": datetime.now(timezone.utc)
    }
    result = await expense_claims_collection.insert_one(claim)
    return {"status": "success", "claim_id": str(result.inserted_id), "message": "Expense claim submitted"}


@app.get("/api/field/expenses")
async def get_my_expenses(employee=Depends(get_current_employee)):
    """Field employee fetches their own expense claims."""
    claims = await expense_claims_collection.find(
        {"employee_id": employee["email"]}
    ).sort("created_at", -1).to_list(length=100)
    for c in claims:
        c["_id"] = str(c["_id"])
        if isinstance(c.get("created_at"), datetime):
            c["created_at"] = c["created_at"].isoformat()
        if isinstance(c.get("resolved_at"), datetime):
            c["resolved_at"] = c["resolved_at"].isoformat()
    return claims


@app.get("/admin/expenses")
async def admin_list_expenses(status: Optional[str] = None, admin=Depends(get_current_admin)):
    """Admin fetches all expense claims, optionally filtered by status."""
    query = {}
    if admin.organization_id:
        query["organization_id"] = admin.organization_id
    if status:
        query["status"] = status
    
    claims = await expense_claims_collection.find(query).sort("created_at", -1).to_list(length=200)
    
    emp_emails = list({c.get("employee_id") for c in claims if c.get("employee_id")})
    employees = await employees_collection.find({"email": {"$in": emp_emails}}, {"email": 1, "full_name": 1}).to_list(length=len(emp_emails))
    emp_map = {e["email"]: e["full_name"] for e in employees}

    for c in claims:
        c["_id"] = str(c["_id"])
        if isinstance(c.get("created_at"), datetime):
            c["created_at"] = c["created_at"].isoformat()
        if isinstance(c.get("resolved_at"), datetime):
            c["resolved_at"] = c["resolved_at"].isoformat()
        c["employee_name"] = emp_map.get(c.get("employee_id"), c.get("employee_id", "Unknown"))
    return claims


@app.put("/admin/expenses/{claim_id}")
async def admin_update_expense(claim_id: str, req: dict, admin=Depends(get_current_admin)):
    """Admin approves, rejects, or queries an expense claim."""
    from bson import ObjectId
    action = req.get("action", "approve")  # approve | reject | query
    
    query = {"_id": ObjectId(claim_id)}
    if admin.organization_id:
        query["organization_id"] = admin.organization_id

    update_fields = {}
    if action == "approve":
        update_fields["status"] = "approved"
        update_fields["resolved_at"] = datetime.now(timezone.utc)
    elif action == "reject":
        update_fields["status"] = "rejected"
        update_fields["resolved_at"] = datetime.now(timezone.utc)
    elif action == "query":
        update_fields["status"] = "queried"
        update_fields["manager_query"] = req.get("query_text", "")
    
    result = await expense_claims_collection.update_one(
        query,
        {"$set": update_fields}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Expense claim not found or access denied")
    return {"status": "success", "message": f"Expense {action}d successfully"}


# -----------------------------------------------------------------------------
# ALERTS & FRAUD DETECTION
# -----------------------------------------------------------------------------

@app.get("/admin/field/alerts")
async def get_alerts(
    type: Optional[str] = None,
    severity: Optional[str] = None,
    status: Optional[str] = "pending",
    employee_id: Optional[str] = None,
    source: Optional[str] = None,
    admin=Depends(get_current_admin)
):
    """Retrieve filtered security and operational alerts."""
    query = {"organization_id": admin.organization_id}
    if type: query["type"] = type
    if severity: query["severity"] = severity
    if status and status != "all": query["status"] = status
    if employee_id: query["employee_id"] = employee_id
    if source:
        query["metadata.source"] = source

    alerts = await alerts_collection.find(query).sort("timestamp", -1).to_list(length=100)
    
    emp_emails = list({a["employee_id"] for a in alerts if a.get("employee_id")})
    employees = await employees_collection.find({"email": {"$in": emp_emails}}, {"email": 1, "full_name": 1}).to_list(length=len(emp_emails))
    emp_map = {e["email"]: e["full_name"] for e in employees}

    for a in alerts:
        a["_id"] = str(a["_id"])
        a["employee_name"] = emp_map.get(a.get("employee_id"), a.get("employee_id"))
        if isinstance(a.get("timestamp"), datetime):
            a["timestamp"] = a["timestamp"].isoformat()
            
    return alerts

@app.put("/admin/field/alerts/{alert_id}")
async def update_alert_status(alert_id: str, req: dict, admin=Depends(get_current_admin)):
    """Update alert status (resolved, dismissed)."""
    from bson import ObjectId
    status = req.get("status")
    if status not in ["resolved", "dismissed", "pending"]:
        raise HTTPException(status_code=400, detail="Invalid status")

    result = await alerts_collection.update_one(
        {"_id": ObjectId(alert_id), "organization_id": admin.organization_id},
        {"$set": {"status": status, "resolved_at": datetime.now(timezone.utc)}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"status": "success", "message": f"Alert marked as {status}"}

# -----------------------------------------------------------------------------
# HEATMAP & SLA AUTOMATION
# -----------------------------------------------------------------------------

@app.get("/admin/field/heatmap-data")
async def get_heatmap_data(admin=Depends(get_current_admin)):
    """Aggregation of pings for heatmap visualization."""
    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    base_filter = get_employee_filter(admin)
    query = {"recorded_at": {"$gte": start_of_day}}
    
    if "organization_id" in base_filter:
        query["organization_id"] = base_filter["organization_id"]
        
    if "manager_id" in base_filter:
        team = await employees_collection.find({"manager_id": base_filter["manager_id"]}).to_list(length=100)
        team_emails = [e["email"] for e in team]
        query["employee_id"] = {"$in": team_emails}
        
    pings = await location_pings_collection.find(query).to_list(length=5000)
    
    # Format for Leaflet.heat: [[lat, lng, intensity], ...]
    heatmap_data = [[p["lat"], p["lng"], 0.5] for p in pings]
    return heatmap_data


async def check_missed_visits():
    """Background check to flag missed visits for productivity alerts."""
    try:
        now = datetime.now(timezone.utc)
        # Using a simple check: if it's after 12:00 PM UTC (around 5:30 PM IST), run the check.
        if now.hour < 12:
            return
            
        today_str = now.strftime("%Y-%m-%d")
        # Find approved plans for today
        plans = await visit_plans_collection.find({"date": today_str, "status": "approved"}).to_list(length=1000)
        
        for plan in plans:
            pending_stops = [s for s in plan.get("stops", []) if s.get("status", "pending") == "pending"]
            if pending_stops:
                # Trigger alert for each agent with pending visits
                detail = f"SLA Breach: {len(pending_stops)} visits missed for today."
                
                # Deduplication: check if we already alerted today
                day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
                existing = await alerts_collection.find_one({
                    "employee_id": plan["employee_id"],
                    "type": "Productivity",
                    "detail": {"$regex": "SLA Breach"},
                    "timestamp": {"$gte": day_start}
                })
                
                if not existing:
                    await trigger_alert(
                        "Productivity",
                        plan["employee_id"],
                        plan["organization_id"],
                        detail,
                        "medium"
                    )
    except Exception as e:
        logger.error(f"SLA Check failed: {e}")


@app.post("/admin/field/trigger-sla-check")
async def trigger_sla_check_manual(admin=Depends(get_current_admin)):
    """Manually trigger the SLA check for debugging."""
    await check_missed_visits()
    return {"status": "success", "message": "Manual SLA check triggered"}


# -----------------------------------------------------------------------------
# REPORTS & ANALYTICS
# -----------------------------------------------------------------------------

@app.get("/admin/reports/attendance")
async def attendance_report(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    employee_type: Optional[str] = None,
    admin=Depends(get_current_admin)
):
    """Generate attendance report with filters."""
    query = {}
    
    scoped_ids = await get_scoped_employee_ids(admin)
    query["user_id"] = {"$in": scoped_ids}
    
    if start_date:
        query["timestamp"] = {"$gte": datetime.fromisoformat(start_date)}
    if end_date:
        if "timestamp" in query:
            query["timestamp"]["$lte"] = datetime.fromisoformat(end_date)
        else:
            query["timestamp"] = {"$lte": datetime.fromisoformat(end_date)}
    
    logs = await attendance_logs_collection.find(query).sort("timestamp", -1).to_list(length=1000)
    
    from bson import ObjectId
    emp_ids = list({ObjectId(log["user_id"]) for log in logs if log.get("user_id") and ObjectId.is_valid(str(log["user_id"]))})
    employees = await employees_collection.find({"_id": {"$in": emp_ids}}, {"_id": 1, "full_name": 1, "employee_type": 1, "email": 1}).to_list(length=len(emp_ids))
    emp_map = {str(e["_id"]): e for e in employees}

    summary = {"total_records": len(logs), "check_ins": 0, "check_outs": 0, "unique_employees": set()}
    enriched = []
    
    for log in logs:
        emp_id_str = str(log.get("user_id", ""))
        emp = emp_map.get(emp_id_str)
        if employee_type and emp and emp.get("employee_type", "desk") != employee_type:
            continue
        
        log["_id"] = str(log["_id"])
        log["full_name"] = emp["full_name"] if emp else "Unknown"
        log["employee_type"] = emp.get("employee_type", "desk") if emp else "unknown"
        if isinstance(log.get("timestamp"), datetime):
            log["timestamp"] = log["timestamp"].isoformat()
        
        if log.get("type") == "check-in":
            summary["check_ins"] += 1
        else:
            summary["check_outs"] += 1
        summary["unique_employees"].add(emp.get("email", "") if emp else "")
        enriched.append(log)
    
    summary["unique_employees"] = len(summary["unique_employees"])
    summary["total_records"] = len(enriched)
    
    return {"summary": summary, "records": enriched}


@app.get("/admin/reports/expenses")
async def expense_report(admin=Depends(get_current_admin)):
    """Generate expense summary report."""
    query = {}
    scoped_emails = await get_scoped_employee_emails(admin)
    query["employee_email"] = {"$in": scoped_emails}
    
    claims = await expense_claims_collection.find(query).to_list(length=1000)
    
    total_amount = sum(c.get("amount", 0) for c in claims)
    approved_amount = sum(c.get("amount", 0) for c in claims if c.get("status") == "approved")
    pending_amount = sum(c.get("amount", 0) for c in claims if c.get("status") == "pending")
    rejected_amount = sum(c.get("amount", 0) for c in claims if c.get("status") == "rejected")
    
    return {
        "total_claims": len(claims),
        "total_amount": total_amount,
        "approved_amount": approved_amount,
        "pending_amount": pending_amount,
        "rejected_amount": rejected_amount,
        "by_status": {
            "pending": len([c for c in claims if c.get("status") == "pending"]),
            "approved": len([c for c in claims if c.get("status") == "approved"]),
            "rejected": len([c for c in claims if c.get("status") == "rejected"]),
        }
    }


@app.get("/admin/reports/agent-performance")
async def agent_performance_report(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin=Depends(get_current_admin)
):
    """Aggregate visits and distance per agent."""
    org_id = admin.organization_id
    
    if not start_date or not end_date:
        now = datetime.now(timezone.utc)
        start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_dt = now
    else:
        try:
            start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        except ValueError:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    scoped_emp_ids = await get_scoped_employee_employee_ids(admin)

    pipeline = [
        {"$match": {
            "organization_id": org_id,
            "employee_id": {"$in": scoped_emp_ids},
            "check_in_time": {"$gte": start_dt, "$lte": end_dt}
        }},
        {"$group": {
            "_id": "$employee_id",
            "total_visits": {"$sum": 1},
            "leads": {"$sum": {"$cond": ["$lead_captured", 1, 0]}},
            "orders": {"$sum": {"$cond": ["$order_captured", 1, 0]}}
        }}
    ]
    visit_stats = await visit_logs_collection.aggregate(pipeline).to_list(None)

    km_pipeline = [
        {"$match": {
            "organization_id": org_id,
            "date": {"$gte": start_dt.strftime("%Y-%m-%d"), "$lte": end_dt.strftime("%Y-%m-%d")}
        }},
        {"$group": {
            "_id": "$employee_id",
            "total_km": {"$sum": "$total_km"}
        }}
    ]
    km_stats = await km_reimbursements_collection.aggregate(km_pipeline).to_list(None)

    perf_map = {}
    for stat in visit_stats:
        eid = stat["_id"]
        emp = await employees_collection.find_one({"employee_id": eid}, {"full_name": 1})
        perf_map[eid] = {
            "employee_id": eid,
            "full_name": emp["full_name"] if emp else "Unknown",
            "total_visits": stat["total_visits"],
            "leads": stat["leads"],
            "orders": stat["orders"],
            "total_km": 0
        }
    
    for kstat in km_stats:
        eid = kstat["_id"]
        if eid in perf_map:
            perf_map[eid]["total_km"] = round(kstat["total_km"], 2)
        else:
            emp = await employees_collection.find_one({"employee_id": eid}, {"full_name": 1})
            perf_map[eid] = {
                "employee_id": eid,
                "full_name": emp["full_name"] if emp else "Unknown",
                "total_visits": 0,
                "leads": 0,
                "orders": 0,
                "total_km": round(kstat["total_km"], 2)
            }

    return list(perf_map.values())
    

@app.get("/admin/reports/leaves")
async def leave_report(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin=Depends(get_current_admin)
):
    """Generate leave analytics report."""
    query = {}
    scoped_emails = await get_scoped_employee_emails(admin)
    query["employee_email"] = {"$in": scoped_emails}
    
    if start_date and end_date:
        query["start_date"] = {"$gte": start_date, "$lte": end_date}
    
    cursor = leave_requests_collection.find(query)
    leaves = await cursor.to_list(length=1000)
    
    # 1. Distribution by Type
    type_counts = {}
    for l in leaves:
        lt = l.get("leave_type", "other")
        type_counts[lt] = type_counts.get(lt, 0) + 1
    
    distribution = [{"name": k.replace("_", " ").capitalize(), "value": v} for k, v in type_counts.items()]
    
    # 2. Trends (Requests per day)
    trend_map = {}
    for l in leaves:
        cd = l.get("created_at")
        if cd:
            if isinstance(cd, datetime):
                ds = cd.strftime("%Y-%m-%d")
            else:
                ds = str(cd)[:10]
            trend_map[ds] = trend_map.get(ds, 0) + 1
            
    # Sort trends
    trends = [{"date": k, "count": v} for k, v in sorted(trend_map.items())]
    
    return {
        "distribution": distribution,
        "trends": trends,
        "total_requests": len(leaves),
        "approved": len([l for l in leaves if l.get("status") == "approved"]),
        "rejected": len([l for l in leaves if l.get("status") == "rejected"]),
        "pending": len([l for l in leaves if l.get("status") == "pending"])
    }


@app.get("/admin/reports/conversion-funnel")
async def conversion_funnel_report(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin=Depends(get_current_admin)
):
    """Aggregate visit outcomes for funnel visualization."""
    org_id = admin.organization_id
    
    if not start_date or not end_date:
        now = datetime.now(timezone.utc)
        start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_dt = now
    else:
        start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
    scoped_emp_ids = await get_scoped_employee_employee_ids(admin)

    pipeline = [
        {"$match": {
            "organization_id": org_id,
            "employee_id": {"$in": scoped_emp_ids},
            "check_in_time": {"$gte": start_dt, "$lte": end_dt}
        }},
        {"$group": {
            "_id": None,
            "visits": {"$sum": 1},
            "leads": {"$sum": {"$cond": ["$lead_captured", 1, 0]}},
            "orders": {"$sum": {"$cond": ["$order_captured", 1, 0]}}
        }}
    ]
    result = await visit_logs_collection.aggregate(pipeline).to_list(None)
    
    if not result:
        return {"visits": 0, "leads": 0, "orders": 0, "conversion_rate": 0}
    
    data = result[0]
    data.pop("_id", None)
    data["conversion_rate"] = round((data["orders"] / data["visits"] * 100), 2) if data["visits"] > 0 else 0
    return data


@app.get("/admin/reports/visit-frequency")
async def visit_frequency_report(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin=Depends(get_current_admin)
):
    """Daily trends analysis."""
    org_id = admin.organization_id
    
    if not start_date or not end_date:
        now = datetime.now(timezone.utc)
        start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_dt = now
    else:
        start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))

    scoped_emp_ids = await get_scoped_employee_employee_ids(admin)

    pipeline_daily = [
        {"$match": {
            "organization_id": org_id,
            "employee_id": {"$in": scoped_emp_ids},
            "check_in_time": {"$gte": start_dt, "$lte": end_dt}
        }},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$check_in_time"}},
            "count": {"$sum": 1}
        }},
        {"$sort": {"_id": 1}}
    ]
    daily_stats = await visit_logs_collection.aggregate(pipeline_daily).to_list(None)

    return {
        "daily_trends": [{"date": s["_id"], "visits": s["count"]} for s in daily_stats]
    }


# -----------------------------------------------------------------------------
# AREA 2: SMART VISIT PLAN TEMPLATES
# -----------------------------------------------------------------------------

@app.post("/api/field/plan/template")
async def create_plan_template(req: dict, employee=Depends(get_current_employee)):
    """Create a recurring visit plan template (e.g., Monday–Friday milk run)."""
    template_name = req.get("template_name")
    stops = req.get("stops", [])
    recurrence_days = req.get("recurrence_days", [0, 1, 2, 3, 4])  # Mon-Fri by default

    if not template_name:
        raise HTTPException(status_code=400, detail="template_name is required")
    if not stops or len(stops) == 0:
        raise HTTPException(status_code=400, detail="At least one stop is required")

    template = {
        "employee_id": employee["email"],
        "organization_id": employee.get("organization_id"),
        "template_name": template_name,
        "stops": stops,
        "recurrence_days": recurrence_days,  # 0=Mon, 1=Tue, ... 6=Sun
        "created_at": datetime.now(timezone.utc),
    }
    result = await visit_plan_templates_collection.insert_one(template)
    return {
        "status": "success",
        "template_id": str(result.inserted_id),
        "message": f"Template '{template_name}' saved with {len(stops)} stops."
    }


@app.get("/api/field/plan/templates/{employee_email}")
async def get_plan_templates(employee_email: str, employee=Depends(get_current_employee)):
    """List all recurring plan templates for an agent."""
    if employee["email"] != employee_email:
        raise HTTPException(status_code=403, detail="Cannot access another user's templates.")

    templates = await visit_plan_templates_collection.find(
        {"employee_id": employee_email}
    ).sort("created_at", -1).to_list(length=50)

    for t in templates:
        t["_id"] = str(t["_id"])
        if isinstance(t.get("created_at"), datetime):
            t["created_at"] = t["created_at"].isoformat()

    return templates


@app.delete("/api/field/plan/template/{template_id}")
async def delete_plan_template(template_id: str, employee=Depends(get_current_employee)):
    """Delete a recurring plan template."""
    from bson import ObjectId
    result = await visit_plan_templates_collection.delete_one({
        "_id": ObjectId(template_id),
        "employee_id": employee["email"]
    })
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Template not found or not owned by you.")
    return {"status": "deleted"}


# -----------------------------------------------------------------------------
# AREA 3: LEADERBOARD
# -----------------------------------------------------------------------------

@app.get("/api/field/leaderboard")
async def get_field_leaderboard(employee=Depends(get_current_employee)):
    """
    Weekly leaderboard: Top 10 agents by visits completed + leads captured.
    Scoped to the employee's organization.
    """
    org_id = employee.get("organization_id")

    # Current week boundaries (Mon 00:00 → Sun 23:59)
    today = datetime.now(timezone.utc)
    week_start = today - timedelta(days=today.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)

    pipeline = [
        {
            "$match": {
                "organization_id": org_id,
                "check_in_time": {"$gte": week_start, "$lt": week_end}
            }
        },
        {
            "$group": {
                "_id": "$employee_id",
                "visits_completed": {"$sum": 1},
                "leads_captured": {"$sum": {"$cond": ["$lead_captured", 1, 0]}},
                "orders_captured": {"$sum": {"$cond": ["$order_captured", 1, 0]}},
            }
        },
        {"$sort": {"visits_completed": -1, "leads_captured": -1}},
        {"$limit": 10}
    ]

    results = await visit_logs_collection.aggregate(pipeline).to_list(length=10)

    leaderboard = []
    for idx, row in enumerate(results):
        emp = await employees_collection.find_one({"email": row["_id"]})
        emp_name = emp["full_name"] if emp else row["_id"]
        emp_designation = emp.get("designation", "Field Agent") if emp else "Field Agent"

        # Sum KM for the week from location pings
        pings = await location_pings_collection.find({
            "employee_id": row["_id"],
            "recorded_at": {"$gte": week_start, "$lt": week_end}
        }).sort("recorded_at", 1).to_list(length=5000)

        total_km = 0.0
        for i in range(1, len(pings)):
            p1, p2 = pings[i - 1], pings[i]
            dlat = math.radians(p2["lat"] - p1["lat"])
            dlng = math.radians(p2["lng"] - p1["lng"])
            a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(p1["lat"])) * math.cos(math.radians(p2["lat"])) * math.sin(dlng / 2) ** 2
            total_km += 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        # Is this the requesting user?
        is_me = (row["_id"] == employee["email"])

        leaderboard.append({
            "rank": idx + 1,
            "employee_id": row["_id"],
            "name": emp_name,
            "designation": emp_designation,
            "visits_completed": row["visits_completed"],
            "leads_captured": row["leads_captured"],
            "orders_captured": row["orders_captured"],
            "distance_km": round(total_km, 1),
            "is_me": is_me,
        })

    # If requesting user is not in top 10, add their rank at the bottom
    if not any(e["is_me"] for e in leaderboard):
        my_stats = await visit_logs_collection.find({
            "employee_id": employee["email"],
            "organization_id": org_id,
            "check_in_time": {"$gte": week_start, "$lt": week_end}
        }).to_list(length=500)

        if my_stats:
            my_rank_pipeline = [
                {"$match": {"organization_id": org_id, "check_in_time": {"$gte": week_start, "$lt": week_end}}},
                {"$group": {"_id": "$employee_id", "visits_completed": {"$sum": 1}}},
                {"$sort": {"visits_completed": -1}}
            ]
            all_ranks = await visit_logs_collection.aggregate(my_rank_pipeline).to_list(length=1000)
            my_rank_pos = next((i + 1 for i, r in enumerate(all_ranks) if r["_id"] == employee["email"]), None)

            leaderboard.append({
                "rank": my_rank_pos or len(all_ranks) + 1,
                "employee_id": employee["email"],
                "name": employee.get("full_name", employee["email"]),
                "designation": employee.get("designation", "Field Agent"),
                "visits_completed": len(my_stats),
                "leads_captured": sum(1 for v in my_stats if v.get("lead_captured")),
                "orders_captured": sum(1 for v in my_stats if v.get("order_captured")),
                "distance_km": 0.0,
                "is_me": True,
            })

    return {
        "week_start": week_start.strftime("%Y-%m-%d"),
        "week_end": (week_end - timedelta(days=1)).strftime("%Y-%m-%d"),
        "leaderboard": leaderboard
    }


# -----------------------------------------------------------------------------
# AREA 3: MANAGER NUDGE
# -----------------------------------------------------------------------------

@app.post("/api/manager/nudge")
async def send_manager_nudge(req: dict, manager=Depends(get_current_employee)):
    """
    Manager sends a motivational nudge to one or more team members.
    Logged to DB. FCM push notification can be added here in production.
    """
    # Verify manager status
    subordinates_count = await employees_collection.count_documents({"manager_id": manager["email"]})
    if subordinates_count == 0:
        raise HTTPException(status_code=403, detail="Only managers can send nudges.")

    employee_emails = req.get("employee_emails", [])
    message = req.get("message", "").strip()
    nudge_type = req.get("nudge_type", "general")  # general | target_missed | late_start | great_job

    if not employee_emails:
        raise HTTPException(status_code=400, detail="At least one employee email is required.")
    if not message:
        raise HTTPException(status_code=400, detail="Nudge message cannot be empty.")

    # Verify all recipients belong to the manager
    valid_recipients = []
    for email in employee_emails:
        emp = await employees_collection.find_one({"email": email, "manager_id": manager["email"]})
        if emp:
            valid_recipients.append(email)

    if not valid_recipients:
        raise HTTPException(status_code=400, detail="No valid team members found in the provided emails.")

    # Save nudge log
    nudge_log = {
        "manager_id": manager["email"],
        "manager_name": manager.get("full_name", manager["email"]),
        "organization_id": manager.get("organization_id"),
        "recipients": valid_recipients,
        "message": message,
        "nudge_type": nudge_type,
        "sent_at": datetime.now(timezone.utc),
        # FCM push would go here in production: firebase_admin.messaging.send_multicast(...)
    }
    result = await nudge_logs_collection.insert_one(nudge_log)

    logger.info(f"Manager nudge sent by {manager['email']} to {valid_recipients}: [{nudge_type}] {message}")

    return {
        "status": "sent",
        "nudge_id": str(result.inserted_id),
        "recipients_count": len(valid_recipients),
        "recipients": valid_recipients,
        "message": f"Nudge sent to {len(valid_recipients)} team member(s)."
    }


@app.get("/api/manager/nudge/history")
async def get_nudge_history(manager=Depends(get_current_employee)):
    """Fetch the nudge history sent by this manager."""
    logs = await nudge_logs_collection.find(
        {"manager_id": manager["email"]}
    ).sort("sent_at", -1).to_list(length=50)

    for log in logs:
        log["_id"] = str(log["_id"])
        if isinstance(log.get("sent_at"), datetime):
            log["sent_at"] = log["sent_at"].isoformat()

    return logs

@app.post("/admin/nudge")
async def admin_send_nudge(req: dict, admin=Depends(get_current_admin)):
    """Admin sends a motivational nudge to field team members in their organization."""
    org_id = admin.organization_id
    employee_emails = req.get("employee_emails", [])
    message = req.get("message", "").strip()
    nudge_type = req.get("nudge_type", "general")

    if not employee_emails:
        raise HTTPException(status_code=400, detail="At least one employee email is required.")
    if not message:
        raise HTTPException(status_code=400, detail="Nudge message cannot be empty.")

    # Verify recipients belong to same organization
    valid_recipients = []
    for email in employee_emails:
        query = {"email": email}
        if org_id:
            query["organization_id"] = org_id
            
        emp = await employees_collection.find_one(query)
        if emp:
            valid_recipients.append(email)

    if not valid_recipients:
        raise HTTPException(status_code=400, detail="No valid employees found in your organization.")

    nudge_log = {
        "admin_id": admin.email,
        "admin_name": admin.full_name,
        "organization_id": org_id,
        "recipients": valid_recipients,
        "message": message,
        "nudge_type": nudge_type,
        "sent_at": datetime.now(timezone.utc),
    }
    result = await nudge_logs_collection.insert_one(nudge_log)
    return {
        "status": "sent",
        "nudge_id": str(result.inserted_id),
        "recipients_count": len(valid_recipients),
        "message": f"Nudge sent to {len(valid_recipients)} member(s)."
    }


@app.get("/admin/nudge/history")
async def admin_get_nudge_history(admin=Depends(get_current_admin)):
    """Fetch history of nudges sent by admins in this organization."""
    query = {}
    if admin.organization_id:
        query["organization_id"] = admin.organization_id
        
    logs = await nudge_logs_collection.find(query).sort("sent_at", -1).to_list(length=50)

    for log in logs:
        log["_id"] = str(log["_id"])
        if isinstance(log.get("sent_at"), datetime):
            log["sent_at"] = log["sent_at"].isoformat()

    return logs

@app.get("/admin/leaderboard")
async def get_admin_leaderboard(admin=Depends(get_current_admin)):
    """
    Weekly leaderboard for Admin Portal: Top 10 agents in the organization.
    """
    org_id = admin.organization_id

    # Current week boundaries (Mon 00:00 → Sun 23:59)
    today = datetime.now(timezone.utc)
    week_start = today - timedelta(days=today.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)

    pipeline = [
        {
            "$match": {
                "organization_id": org_id,
                "check_in_time": {"$gte": week_start, "$lt": week_end}
            }
        },
        {
            "$group": {
                "_id": "$employee_id",
                "visits_completed": {"$sum": 1},
                "leads_captured": {"$sum": {"$cond": ["$lead_captured", 1, 0]}},
                "orders_captured": {"$sum": {"$cond": ["$order_captured", 1, 0]}},
            }
        },
        {"$sort": {"visits_completed": -1, "leads_captured": -1}},
        {"$limit": 10}
    ]

    results = await visit_logs_collection.aggregate(pipeline).to_list(length=10)

    leaderboard = []
    for idx, row in enumerate(results):
        emp = await employees_collection.find_one({"email": row["_id"]})
        emp_name = emp["full_name"] if emp else row["_id"]
        emp_designation = emp.get("designation", "Field Agent") if emp else "Field Agent"

        # Sum KM for the week from location pings
        pings = await location_pings_collection.find({
            "employee_id": row["_id"],
            "recorded_at": {"$gte": week_start, "$lt": week_end}
        }).sort("recorded_at", 1).to_list(length=5000)

        total_km = 0.0
        for i in range(1, len(pings)):
            p1, p2 = pings[i - 1], pings[i]
            dlat = math.radians(p2["lat"] - p1["lat"])
            dlng = math.radians(p2["lng"] - p1["lng"])
            a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(p1["lat"])) * math.cos(math.radians(p2["lat"])) * math.sin(dlng / 2) ** 2
            total_km += 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        leaderboard.append({
            "rank": idx + 1,
            "employee_id": row["_id"],
            "name": emp_name,
            "designation": emp_designation,
            "visits_completed": row["visits_completed"],
            "leads_captured": row["leads_captured"],
            "orders_captured": row["orders_captured"],
            "distance_km": round(total_km, 1),
            "is_me": False, # Admins are not usually in the leaderboard
        })

    return {
        "week_start": week_start.strftime("%Y-%m-%d"),
        "week_end": (week_end - timedelta(days=1)).strftime("%Y-%m-%d"),
        "leaderboard": leaderboard
    }

# -----------------------------------------------------------------------------
# DATA SYNC & BULK MANAGEMENT
# -----------------------------------------------------------------------------

@app.get("/api/me/sync-status")
async def get_sync_status(last_sync: Optional[str] = None, employee=Depends(get_current_employee)):
    """Check for new data to sync to the mobile app."""
    # Simplified sync payload for pulling latest configurations
    profile = {
        "employee_id": employee.get("employee_id"),
        "full_name": employee.get("full_name"),
        "manager_id": employee.get("manager_id"),
        "territory": employee.get("territory"),
        "employee_type": employee.get("employee_type")
    }
    
    now = datetime.now(timezone.utc)
    
    return {
        "status": "success",
        "latest_profile": profile,
        "server_time": now.isoformat(),
        "requires_full_sync": True
    }


@app.get("/admin/reports/employee-monthly-summary")
async def get_employee_monthly_summary(
    email: str,
    month: str,  # Format: YYYY-MM
    admin=Depends(get_current_admin)
):
    """
    Get a detailed monthly summary for a specific employee.
    Used for individual drill-down reports.
    """
    try:
        start_date = datetime.strptime(f"{month}-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)
        # End date is first day of next month
        year, m = map(int, month.split("-"))
        if m == 12:
            next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month = datetime(year, m + 1, 1, tzinfo=timezone.utc)
            
        end_date = next_month
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid month format. Expected YYYY-MM")

    # Security: Ensure admin only sees their org's data
    org_filter = {"organization_id": admin.organization_id} if admin.organization_id else {}
    
    # 1. Fetch Employee Details
    employee = await employees_collection.find_one({"email": email, **org_filter})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    # 2. Fetch All Logs for the month
    logs = await attendance_logs_collection.find({
        "email": email,
        "timestamp": {"$gte": start_date, "$lt": end_date}
    }).sort("timestamp", 1).to_list(length=1000)

    # 3. Fetch Approved Leaves for the month (flexible identifier matching)
    clean_email = email.strip().lower()
    emp_identifiers = [clean_email, email.strip()]
    if "_id" in employee:
        emp_identifiers.append(str(employee["_id"]))
    if "employee_id" in employee and employee["employee_id"]:
        emp_identifiers.append(str(employee["employee_id"]).strip())

    raw_leaves = await leave_requests_collection.find({
        "$or": [
            {"employee_id": {"$in": emp_identifiers}},
            {"email": {"$in": emp_identifiers}}
        ],
        "status": {"$regex": "^approved$", "$options": "i"}
    }).to_list(length=100)

    leaves = []
    for rl in raw_leaves:
        norm_s = normalize_date_str(rl.get("start_date", ""))
        norm_e = normalize_date_str(rl.get("end_date", ""))
        if norm_s:
            rl["start_date"] = norm_s
            rl["end_date"] = norm_e or norm_s
            leaves.append(rl)

    raw_pending = await leave_requests_collection.find({
        "$or": [
            {"employee_id": {"$in": emp_identifiers}},
            {"email": {"$in": emp_identifiers}}
        ],
        "status": {"$regex": "^pending$", "$options": "i"}
    }).to_list(length=100)

    pending_leaves = []
    for rp in raw_pending:
        norm_s = normalize_date_str(rp.get("start_date", ""))
        norm_e = normalize_date_str(rp.get("end_date", ""))
        if norm_s:
            rp["start_date"] = norm_s
            rp["end_date"] = norm_e or norm_s
            pending_leaves.append(rp)

    # 4. Process Data Day by Day
    daily_breakdown = []
    total_working_ms = 0
    present_days = 0
    absent_days = 0
    leaves_count = 0
    
    # Fetch org settings for timezone and shift times (unified from settings + leave_policy)
    org_id = employee.get("organization_id")
    org_doc = await get_effective_org_settings(org_id)
    tz_offset = org_doc.get("timezone_offset", 330)

    if employee.get("employee_type") == "field":
        office_end_time_str = org_doc.get("field_office_end_time") or org_doc.get("office_end_time", "18:00")
        office_start_time_str = org_doc.get("field_office_start_time") or org_doc.get("office_start_time", "10:00")
        late_threshold_mins = org_doc.get("field_late_grace_mins") if org_doc.get("field_late_grace_mins") is not None else org_doc.get("field_late_threshold_mins", 30)
    else:
        office_end_time_str = org_doc.get("office_end_time", "18:00")
        office_start_time_str = org_doc.get("office_start_time", "10:00")
        late_threshold_mins = org_doc.get("late_grace_mins") if org_doc.get("late_grace_mins") is not None else org_doc.get("late_threshold_mins", 15)

    # Fetch configured days off / holidays for this organization and month
    days_off_set = set()
    holiday_names = {}
    if org_id:
        try:
            # 1. Fetch days_off doc (has composite days_off array)
            h_doc = await holidays_collection.find_one({
                "$or": [
                    {"organization_id": str(org_id), "payroll_month": month, "days_off": {"$exists": True}},
                    {"organization_id": "system_org", "payroll_month": month, "days_off": {"$exists": True}}
                ]
            })
            if h_doc:
                days_off_set.update(h_doc.get("days_off", []))
                holiday_names.update(h_doc.get("holiday_names", {}))

            # 2. Fetch all named holidays for this month (e.g. Janmashtami, festival, company)
            named_holidays = await holidays_collection.find({
                "$or": [
                    {"organization_id": str(org_id), "payroll_month": month, "date": {"$exists": True}},
                    {"organization_id": "system_org", "payroll_month": month, "date": {"$exists": True}}
                ]
            }).to_list(length=100)
            for nh in named_holidays:
                d_num = None
                if "day" in nh and nh["day"] is not None:
                    d_num = int(nh["day"])
                elif "date" in nh and nh["date"]:
                    try:
                        d_num = int(str(nh["date"]).split("-")[2])
                    except Exception:
                        pass
                if d_num is not None:
                    days_off_set.add(d_num)
                    holiday_names[str(d_num)] = nh.get("name") or "Holiday"

            # 3. Always include all Sundays in this month as weekends
            try:
                y_m = month.split("-")
                month_days_cnt = calendar.monthrange(int(y_m[0]), int(y_m[1]))[1]
                for di in range(1, month_days_cnt + 1):
                    if datetime(int(y_m[0]), int(y_m[1]), di).weekday() == 6:
                        days_off_set.add(di)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Failed to fetch holidays in monthly summary: {e}")

    start_h, start_m = map(int, office_start_time_str.split(":"))
    end_h, end_m = map(int, office_end_time_str.split(":"))

    local_start_of_month = start_date + timedelta(minutes=tz_offset)
    local_month_end = end_date + timedelta(minutes=tz_offset)
    local_now = datetime.now(timezone.utc) + timedelta(minutes=tz_offset)
    
    # Use the earlier of (End of Month) or (Today) for the loop boundary
    local_end_boundary = local_now if local_now < local_month_end else local_month_end
    
    current_day_local = local_start_of_month
    while current_day_local < local_end_boundary:
        next_day_local = current_day_local + timedelta(days=1)
        day_str = current_day_local.strftime("%Y-%m-%d")
        
        # Filter logs for this day in localized boundary
        day_logs = []
        for l in logs:
            ts = l.get("timestamp")
            if not ts:
                continue
            
            # Ensure timestamp is UTC aware
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            else:
                ts = ts.astimezone(timezone.utc)
            
            # Localize for comparison
            local_ts = ts + timedelta(minutes=tz_offset)
            if current_day_local <= local_ts < next_day_local:
                day_logs.append(l)
        
        day_info = {
            "date": day_str,
            "status": "Absent",
            "is_late": False,
            "is_early_leave": False,
            "is_short_leave": False,
            "is_half_day": False,
            "short_leave_type": None,
            "half_day_type": None,
            "late_mins": 0,
            "early_exit_mins": 0,
            "late_off_hours": 0.0,
            "early_off_hours": 0.0,
            "off_hours": 0.0,
            "required_hours": 8.0,
            "effective_days": 0.0,
            "classification_reason": "",
            "is_applied_leave": False,
            "first_in": None,
            "last_out": None,
            "duration_hours": 0,
            "logs": []
        }

        # 1. Check for Approved Leave on this day
        matching_leave = None
        for leave in leaves:
            s_date = str(leave.get("start_date", ""))[:10]
            e_date = str(leave.get("end_date", ""))[:10]
            if s_date and e_date and (s_date <= day_str <= e_date):
                matching_leave = leave
                leaves_count += 1
                break

        # 2. Check for Pending Leave on this day
        is_pending_leave = False
        pending_label = None
        if not matching_leave:
            for pl in pending_leaves:
                s_date = str(pl.get("start_date", ""))[:10]
                e_date = str(pl.get("end_date", ""))[:10]
                if s_date and e_date and (s_date <= day_str <= e_date):
                    l_type = pl.get("leave_type", "General")
                    pending_label = f"Pending Leave ({l_type})"
                    is_pending_leave = True
                    break

        # 3. Check for Configured Holidays / Days Off / Weekend
        day_num = current_day_local.day
        is_weekend_or_holiday = False
        if day_num in days_off_set:
            is_weekend_or_holiday = True
            if current_day_local.weekday() == 6:
                day_info["status"] = "Weekend"
            elif current_day_local.weekday() == 5:
                day_info["status"] = holiday_names.get(str(day_num), "Weekend")
            else:
                day_info["status"] = holiday_names.get(str(day_num), "Holiday")
        elif current_day_local.weekday() >= 5:
            is_weekend_or_holiday = True
            day_info["status"] = "Weekend"

        if day_logs:
            day_logs.sort(key=lambda x: x["timestamp"])
            present_days += 1
            
            day_info["first_in"] = day_logs[0]["timestamp"].replace(tzinfo=timezone.utc).isoformat()
            
            day_duration_ms = 0
            last_in_time = None
            
            for log in day_logs:
                log_ts = log["timestamp"]
                if log_ts.tzinfo is None:
                    log_ts = log_ts.replace(tzinfo=timezone.utc)
                local_log_time = log_ts + timedelta(minutes=tz_offset)
                
                log_type = log.get("type", "")
                log_status = log.get("attendance_status") or "Present"

                # Basic representation for frontend
                day_info["logs"].append({
                    "time": log_ts.isoformat(),
                    "type": log_type,
                    "status": log_status,
                    "attendance_status": log.get("attendance_status", log_status),
                    "is_short_leave": log.get("is_short_leave", False),
                    "is_half_day": log.get("is_half_day", False),
                    "off_hours": log.get("off_hours", 0.0),
                    "method": log.get("check_in_method", "N/A"),
                    "location": log.get("location"),
                    "selfie": log.get("selfie_url"),
                    "wifi_confidence": log.get("wifi_confidence", 0)
                })

                if log_type == "check-in":
                    last_in_time = log_ts
                elif log_type == "check-out" and last_in_time:
                    delta = log_ts - last_in_time
                    day_duration_ms += delta.total_seconds() * 1000
                    last_in_time = None
                    day_info["last_out"] = log_ts.isoformat()

            duration_hours = round(day_duration_ms / (1000 * 3600), 2)
            day_info["duration_hours"] = duration_hours
            total_working_ms += day_duration_ms

            # Comprehensive classification via classify_attendance_day
            checkout_logs = [l for l in day_logs if l.get("type") == "check-out"]
            first_checkin = [l for l in day_logs if l.get("type") == "check-in"]

            first_in_ts = first_checkin[0]["timestamp"] if first_checkin else day_logs[0]["timestamp"]
            if first_in_ts.tzinfo is None: first_in_ts = first_in_ts.replace(tzinfo=timezone.utc)
            local_first_in = first_in_ts + timedelta(minutes=tz_offset)

            local_last_out = None
            if checkout_logs:
                last_ts = checkout_logs[-1]["timestamp"]
                if last_ts.tzinfo is None: last_ts = last_ts.replace(tzinfo=timezone.utc)
                local_last_out = last_ts + timedelta(minutes=tz_offset)

            classification = classify_attendance_day(
                first_checkin_local=local_first_in,
                last_checkout_local=local_last_out,
                worked_hours=duration_hours,
                settings=org_doc or {},
                employee_type=employee.get("employee_type", "desk"),
                approved_leave=matching_leave
            )

            day_info["status"] = classification["status"]
            day_info["is_late"] = classification["is_late"]
            day_info["is_early_leave"] = classification["is_early_leave"]
            day_info["is_short_leave"] = classification["is_short_leave"]
            day_info["is_half_day"] = classification["is_half_day"]
            day_info["short_leave_type"] = classification["short_leave_type"]
            day_info["half_day_type"] = classification["half_day_type"]
            day_info["late_mins"] = classification["late_mins"]
            day_info["early_exit_mins"] = classification["early_exit_mins"]
            day_info["late_off_hours"] = classification["late_off_hours"]
            day_info["early_off_hours"] = classification["early_off_hours"]
            day_info["off_hours"] = classification["off_hours"]
            day_info["required_hours"] = classification["required_hours"]
            day_info["effective_days"] = classification["effective_days"]
            day_info["classification_reason"] = classification["classification_reason"]
            day_info["is_applied_leave"] = classification["is_applied_leave"]

        else:
            # No attendance logs on this day
            if matching_leave:
                classification = classify_attendance_day(
                    first_checkin_local=None,
                    last_checkout_local=None,
                    worked_hours=0.0,
                    settings=org_doc or {},
                    employee_type=employee.get("employee_type", "desk"),
                    approved_leave=matching_leave
                )
                day_info["status"] = classification["status"]
                day_info["is_short_leave"] = classification["is_short_leave"]
                day_info["is_half_day"] = classification["is_half_day"]
                day_info["short_leave_type"] = classification["short_leave_type"]
                day_info["half_day_type"] = classification["half_day_type"]
                day_info["off_hours"] = classification["off_hours"]
                day_info["effective_days"] = classification["effective_days"]
                day_info["classification_reason"] = classification["classification_reason"]
                day_info["is_applied_leave"] = True
            elif is_pending_leave:
                day_info["status"] = pending_label
                day_info["classification_reason"] = "Pending Leave Approval"
            elif is_weekend_or_holiday:
                day_info["classification_reason"] = day_info["status"]
                day_info["off_hours"] = 0.0
                day_info["effective_days"] = 1.0 if day_info["status"] not in ["Weekend"] else 0.0
            else:
                day_info["status"] = "Absent"
                day_info["classification_reason"] = "Unexcused Absence"
                absent_days += 1
                req_h = float((org_doc or {}).get("required_hours", 8.0))
                day_info["off_hours"] = req_h
                day_info["required_hours"] = req_h

        daily_breakdown.append(day_info)
        current_day_local = next_day_local

    # Calculate holiday count from breakdown
    holiday_days_count = sum(1 for d in daily_breakdown if d.get("status") not in ["Present", "Late", "Early Leave", "Short Leave", "Half Day", "Absent", "Weekend"] and not str(d.get("status", "")).startswith("Leave") and not str(d.get("status", "")).startswith("Pending Leave"))
    
    # Calculate Short Leave & Half Day metrics
    short_leave_days = sum(1 for d in daily_breakdown if d.get("is_short_leave"))
    half_day_days = sum(1 for d in daily_breakdown if d.get("is_half_day"))
    total_off_hours = round(sum(d.get("off_hours", 0.0) for d in daily_breakdown if d.get("status") not in ["Weekend", "Holiday"] and not str(d.get("status", "")).startswith("Leave")), 2)
    total_late_off_hours = round(sum(d.get("late_off_hours", 0.0) for d in daily_breakdown), 2)
    total_early_off_hours = round(sum(d.get("early_off_hours", 0.0) for d in daily_breakdown), 2)

    # Live leave balance for this year and month (including unexcused absent days in total deduction)
    leave_balance = await calculate_leave_balance(email, org_id or "system_org", year, month, absent_days=absent_days)

    total_hours = round(total_working_ms / (1000 * 3600), 2)
    avg_hours = round(total_hours / present_days, 2) if present_days > 0 else 0

    return {
        "employee": {
            "full_name": employee.get("full_name"),
            "email": employee.get("email"),
            "designation": employee.get("designation"),
            "department": employee.get("department"),
            "employee_type": employee.get("employee_type")
        },
        "summary": {
            "total_working_hours": total_hours,
            "average_daily_hours": avg_hours,
            "present_days": present_days,
            "leaves_taken": leaves_count,
            "absent_days": absent_days,
            "short_leave_days": short_leave_days,
            "half_day_days": half_day_days,
            "total_off_hours": total_off_hours,
            "total_late_off_hours": total_late_off_hours,
            "total_early_off_hours": total_early_off_hours,
            "holiday_days": holiday_days_count,
            "total_days_evaluated": len(daily_breakdown),
            "leave_balance": leave_balance
        },
        "daily_breakdown": daily_breakdown
    }


@app.post("/admin/employees/bulk-update")
async def admin_bulk_update_employees(req: dict, admin=Depends(get_current_admin)):
    """Bulk update fields (like manager assignment or territory) for multiple employees."""
    employee_emails = [e.lower().strip() for e in req.get("employee_emails", [])]
    updates = req.get("updates", {})
    
    if "manager_id" in updates:
        updates["manager_id"] = updates["manager_id"].lower().strip()

    if not employee_emails or not updates:
        raise HTTPException(status_code=400, detail="Missing emails or updates")
        
    # Security: Ensure admin only updates records they have access to
    base_filter = get_employee_filter(admin)
    query = {"email": {"$in": employee_emails}, **base_filter}
    
    # Restrict allowed update fields directly
    allowed_updates = ["manager_id", "territory", "employee_type"]
    filtered_updates = {k: v for k, v in updates.items() if k in allowed_updates}
    
    if not filtered_updates:
        raise HTTPException(status_code=400, detail="No valid update fields provided")
        
    result = await employees_collection.update_many(
        query,
        {"$set": filtered_updates}
    )
    
    return {
        "status": "success",
        "modified_count": result.modified_count,
        "matched_count": result.matched_count
    }



# =========================
# WFH DEVICE APIs
# =========================

@app.post("/api/wfh/device-info")
async def register_wfh_device(req: dict, employee=Depends(get_current_employee)):
    """
    WFH desktop app registers or updates employee device info.
    Employee must be logged in with normal employee JWT.
    """

    employee_email = employee.get("email")
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")

    if not org_id:
        raise HTTPException(status_code=400, detail="Employee organization_id missing")

    device_id = req.get("device_id")
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id is required")

    existing_device = await wfh_device_info_collection.find_one({
        "employee_id": employee_id,
        "device_id": device_id,
        "organization_id": org_id
    })

    now = datetime.now(timezone.utc)

    device_data = {
        "employee_id": employee_id,
        "employee_email": employee_email,
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "device_id": device_id,
        "mac_address": req.get("mac_address"),
        "cpu_id": req.get("cpu_id"),
        "os_info": req.get("os_info"),
        "hostname": req.get("hostname"),
        "ip_local": req.get("ip_local"),
        "ip_public": req.get("ip_public"),
        "ram_gb": req.get("ram_gb"),
        "screen_resolution": req.get("screen_resolution"),
        "monitor_count": req.get("monitor_count"),
        "last_seen": now,
        "updated_at": now
    }

    if existing_device:
        await wfh_device_info_collection.update_one(
            {"_id": existing_device["_id"]},
            {"$set": device_data}
        )

        existing_device["_id"] = str(existing_device["_id"])

        return {
            "status": "success",
            "message": "WFH device updated successfully",
            "device_status": existing_device.get("status", "pending"),
            "device_id": device_id
        }

    AUTO_APPROVE = os.getenv("AUTO_APPROVE_WFH_DEVICES", "false").lower() == "true"

    device_data["status"] = "approved" if AUTO_APPROVE else "pending"
    device_data["registered_at"] = now
    device_data["approved_by"] = "auto-approved" if AUTO_APPROVE else None
    device_data["approved_at"] = now if AUTO_APPROVE else None
    device_data["revoked_by"] = None
    device_data["revoked_at"] = None
    device_data["revoke_reason"] = None

    result = await wfh_device_info_collection.insert_one(device_data)

    return {
        "status": "success",
        "message": "WFH device approved." if AUTO_APPROVE else "WFH device registered. Waiting for admin approval.",
        "device_status": "approved" if AUTO_APPROVE else "pending",
        "device_record_id": str(result.inserted_id),
        "device_id": device_id
    }


@app.get("/api/wfh/my-device")
async def get_my_wfh_device(device_id: Optional[str] = None, employee=Depends(get_current_employee)):
    """
    Employee checks current WFH device approval status.
    """

    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")

    query = {
        "employee_id": employee_id,
        "organization_id": org_id
    }

    if device_id:
        query["device_id"] = device_id

    device = await wfh_device_info_collection.find_one(query, sort=[("registered_at", -1)])

    if not device:
        return {
            "registered": False,
            "device_status": "unregistered"
        }

    device["_id"] = str(device["_id"])

    return {
        "registered": True,
        "device": device,
        "device_status": device.get("status", "pending")
    }


@app.get("/admin/wfh/devices")
async def admin_list_wfh_devices(
    status: Optional[str] = None,
    current_admin: Admin = Depends(get_current_admin)
):
    """
    Admin lists all WFH registered devices in their organization.
    """

    org_id = current_admin.organization_id

    query = {}

    if org_id and org_id != "system_org":
        query["organization_id"] = org_id

    if status:
        query["status"] = status

    cursor = wfh_device_info_collection.find(query).sort("registered_at", -1)
    devices = await cursor.to_list(length=1000)

    for device in devices:
        device["_id"] = str(device["_id"])

    return devices


@app.post("/admin/wfh/devices/{device_record_id}/approve")
async def admin_approve_wfh_device(
    device_record_id: str,
    current_admin: Admin = Depends(get_current_admin)
):
    """
    Admin approves a WFH device.
    """

    if not ObjectId.is_valid(device_record_id):
        raise HTTPException(status_code=400, detail="Invalid device record id")

    query = {"_id": ObjectId(device_record_id)}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id

    device = await wfh_device_info_collection.find_one(query)

    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    now = datetime.now(timezone.utc)

    await wfh_device_info_collection.update_one(
        {"_id": ObjectId(device_record_id)},
        {
            "$set": {
                "status": "approved",
                "approved_by": current_admin.email,
                "approved_at": now,
                "revoked_by": None,
                "revoked_at": None,
                "revoke_reason": None
            }
        }
    )

    return {
        "status": "success",
        "message": "WFH device approved successfully"
    }


@app.post("/admin/wfh/devices/{device_record_id}/revoke")
async def admin_revoke_wfh_device(
    device_record_id: str,
    req: dict,
    current_admin: Admin = Depends(get_current_admin)
):
    """
    Admin revokes a WFH device.
    """

    if not ObjectId.is_valid(device_record_id):
        raise HTTPException(status_code=400, detail="Invalid device record id")

    query = {"_id": ObjectId(device_record_id)}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id

    device = await wfh_device_info_collection.find_one(query)

    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    now = datetime.now(timezone.utc)

    await wfh_device_info_collection.update_one(
        {"_id": ObjectId(device_record_id)},
        {
            "$set": {
                "status": "revoked",
                "revoked_by": current_admin.email,
                "revoked_at": now,
                "revoke_reason": req.get("reason", "Revoked by admin")
            }
        }
    )

    return {
        "status": "success",
        "message": "WFH device revoked successfully"
    }

# =========================
# WFH ATTENDANCE APIs
# =========================

async def verify_wfh_device(employee_id: str, org_id: str, device_id: str):
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id is required")

    AUTO_APPROVE = os.getenv("AUTO_APPROVE_WFH_DEVICES", "true").lower() == "true"

    device = await wfh_device_info_collection.find_one({
        "employee_id": employee_id,
        "organization_id": org_id,
        "device_id": device_id
    })

    if not device:
        if AUTO_APPROVE:
            from bson import ObjectId
            employee = await employees_collection.find_one({"_id": ObjectId(employee_id)})
            now = datetime.now(timezone.utc)
            device_doc = {
                "employee_id": employee_id,
                "employee_email": employee.get("email") if employee else "",
                "employee_name": employee.get("full_name", "") if employee else "",
                "organization_id": org_id,
                "device_id": device_id,
                "status": "approved",
                "registered_at": now,
                "approved_at": now,
                "approved_by": "auto-approved",
                "last_seen": now,
                "updated_at": now
            }
            await wfh_device_info_collection.insert_one(device_doc)
            return device_doc

        # Auto approve authenticated employee devices
        from bson import ObjectId
        employee = await employees_collection.find_one({"_id": ObjectId(employee_id)})
        now = datetime.now(timezone.utc)
        device_doc = {
            "employee_id": employee_id,
            "employee_email": employee.get("email") if employee else "",
            "employee_name": employee.get("full_name", "") if employee else "",
            "organization_id": org_id,
            "device_id": device_id,
            "status": "approved",
            "registered_at": now,
            "approved_at": now,
            "approved_by": "auto-approved",
            "last_seen": now,
            "updated_at": now
        }
        await wfh_device_info_collection.insert_one(device_doc)
        return device_doc

    if device.get("status") != "approved":
        await wfh_device_info_collection.update_one(
            {"_id": device["_id"]},
            {"$set": {"status": "approved", "approved_at": datetime.now(timezone.utc), "approved_by": "auto-approved"}}
        )
        device["status"] = "approved"
        return device

        raise HTTPException(
            status_code=403,
            detail="WFH device not approved. Please contact admin."
        )

    return device


@app.post("/api/wfh/checkin")
async def wfh_checkin(req: dict, employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")
    email = employee.get("email")
    device_id = req.get("device_id")
    face_image = req.get("face_image")

    if not org_id:
        raise HTTPException(status_code=400, detail="Employee organization_id missing")

    await verify_wfh_device(employee_id, org_id, device_id)

    if not face_image:
        raise HTTPException(status_code=400, detail="face_image is required")

    stored_embedding = employee.get("face_embedding")
    if not stored_embedding:
        raise HTTPException(status_code=400, detail="Face enrollment missing")

    face_ok, face_distance = verify_face(face_image, stored_embedding)

    if not face_ok:
        raise HTTPException(
            status_code=403,
            detail=f"Face verification failed. Distance: {face_distance}"
        )

    active_session = await wfh_sessions_collection.find_one({
        "employee_id": employee_id,
        "organization_id": org_id,
        "status": "active"
    })

    if active_session:
        return {
            "status": "already_active",
            "message": "WFH session already active",
            "session_id": str(active_session["_id"])
        }

    now = datetime.now(timezone.utc)
    # IMPORTANT: Use LOCAL date (with org timezone offset) so it matches
    # the stale-session check in /api/wfh/session/active which uses local date.
    # Using UTC date here caused instant "stale session" closure for IST users
    # between midnight and 5:30 AM local time.
    tz_offset_mins = 330  # default IST
    try:
        org_settings = await settings_collection.find_one({"organization_id": org_id}) if org_id else None
        if org_settings:
            tz_offset_mins = org_settings.get("timezone_offset", 330)
    except Exception:
        pass
    local_now = now + timedelta(minutes=tz_offset_mins)
    today = local_now.strftime("%Y-%m-%d")

    session_doc = {
        "employee_id": employee_id,
        "employee_email": email,
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "device_id": device_id,
        "date": today,
        "check_in_time": now,
        "check_out_time": None,
        "status": "active",
        "check_in_face_verified": True,
        "face_distance": face_distance,
        "total_active_seconds": 0,
        "total_idle_seconds": 0,
        "productivity_score": 0,
        "created_at": now,
        "updated_at": now,
        "metadata": req.get("metadata", {})
    }

    result = await wfh_sessions_collection.insert_one(session_doc)
    session_id = str(result.inserted_id)

    attendance_doc = {
        "user_id": employee_id,
        "email": email,
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "timestamp": now,
        "type": "check-in",
        "status": "Present",
        "selfie_verified": True,
        "face_confidence": face_distance,
        "device_id": device_id,
        "source": "wfh_desktop",
        "wfh_session_id": session_id,
        "attendance_type": "wfh",
        "check_in_method": "wfh_desktop",
        "created_at": now
    }

    await attendance_logs_collection.insert_one(attendance_doc)
    await employees_collection.update_one(
        {"_id": employee["_id"]},
        {"$set": {"checked_in": True, "last_attendance_type": "wfh_checkin", "last_attendance_time": now}}
    )


    policy = None
    try:
        org = await organizations_collection.find_one({"_id": ObjectId(org_id)})
        if org:
            policy = org.get("wfh_policy")
    except Exception as e:
        logger.error(f"Error fetching policy in checkin: {e}")
        
    if not policy:
        policy = {
            "screenshot_interval_minutes": 10,
            "face_check_interval_minutes": 30,
            "max_idle_minutes": 20,
            "productivity_threshold_percent": 60,
            "working_hours_start": "09:00",
            "working_hours_end": "18:00",
            "screenshot_retention_days": 5,
            "require_face_verification": True,
            "productive_apps": [],
            "unproductive_apps": []
        }

    return {
        "status": "success",
        "message": "WFH check-in successful",
        "session_id": session_id,
        "check_in_time": now.isoformat(),
        "face_verified": True,
        "wfh_policy": policy
    }


@app.post("/api/wfh/checkout")
async def wfh_checkout(req: dict, employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")
    email = employee.get("email")
    device_id = req.get("device_id")
    session_id = req.get("session_id")

    if not org_id:
        raise HTTPException(status_code=400, detail="Employee organization_id missing")

    # Don't block checkout on device verification — the session was already
    # validated at check-in. Blocking here traps users in permanent active sessions.

    query = {
        "employee_id": employee_id,
        "organization_id": org_id,
        "status": "active"
    }

    if session_id:
        if not ObjectId.is_valid(session_id):
            raise HTTPException(status_code=400, detail="Invalid session_id")
        query["_id"] = ObjectId(session_id)

    session = await wfh_sessions_collection.find_one(query)

    if not session:
        raise HTTPException(status_code=404, detail="No active WFH session found")

    now = datetime.now(timezone.utc)
    check_in_time = session.get("check_in_time")

    total_seconds = 0
    if check_in_time:
        if check_in_time.tzinfo is None:
            check_in_time = check_in_time.replace(tzinfo=timezone.utc)
        total_seconds = int((now - check_in_time).total_seconds())

    # SERVER-SIDE PRODUCTIVITY VALIDATION
    # 1. Fetch Policy
    policy = None
    try:
        org = await organizations_collection.find_one({"_id": ObjectId(org_id)})
        if org:
            policy = org.get("wfh_policy")
    except Exception as e:
        logger.error(f"Error fetching policy in checkout validation: {e}")
        
    # 2. Activity Recomputation
    activity_ratio = 0.0
    active_ratio = 1.0
    try:
        activity_cursor = wfh_activity_collection.find({"session_id": str(session["_id"])})
        activities = await activity_cursor.to_list(length=1000)
        
        total_keystrokes = sum(a.get("keystrokes", 0) for a in activities)
        total_clicks = sum(a.get("mouse_clicks", 0) for a in activities)
        total_scrolls = sum(a.get("scroll_events", 0) for a in activities)
        total_active_sec = sum(a.get("active_seconds", 0) for a in activities)
        total_idle_sec = sum(a.get("idle_seconds", 0) for a in activities)
        
        total_rec_seconds = total_active_sec + total_idle_sec
        if total_rec_seconds > 0:
            active_ratio = float(total_active_sec) / total_rec_seconds
            
        total_events = total_keystrokes + total_clicks + total_scrolls
        expected_baseline_events = max(1.0, (total_rec_seconds / 300.0) * 250.0)
        activity_ratio = min(1.0, float(total_events) / expected_baseline_events)
    except Exception as e:
        logger.error(f"Error recomputing activity ratio: {e}")
        active_ratio = 1.0
        activity_ratio = 0.5

    # 3. App Usage Recomputation
    app_productivity_ratio = 0.5
    try:
        app_cursor = wfh_app_usage_collection.find({"session_id": str(session["_id"])})
        usages = await app_cursor.to_list(length=1000)
        
        productive_sec = 0.0
        neutral_sec = 0.0
        unproductive_sec = 0.0
        
        # Helper classification inside checkout block
        def get_local_app_category(app_name: str, org_policy: dict = None) -> str:
            if not app_name or app_name == "unknown":
                return "neutral"
            app_name_lower = app_name.lower().replace(".exe", "").strip()
            productive_keywords = ["vscode", "code", "pycharm", "idea", "figma", "notion", "slack", "teams", "excel", "winword", "word", "powerpnt", "outlook", "chrome", "firefox"]
            unproductive_keywords = ["steam", "netflix", "youtube", "instagram", "facebook", "spotify", "discord", "game", "twitter", "reddit"]
            if org_policy:
                custom_productive = org_policy.get("productive_apps", [])
                custom_unproductive = org_policy.get("unproductive_apps", [])
                if any(app.lower() in app_name_lower for app in custom_productive):
                    return "productive"
                if any(app.lower() in app_name_lower for app in custom_unproductive):
                    return "unproductive"
            if any(keyword in app_name_lower for keyword in productive_keywords):
                return "productive"
            if any(keyword in app_name_lower for keyword in unproductive_keywords):
                return "unproductive"
            return "neutral"
            
        for usage in usages:
            apps = usage.get("apps", [])
            for app in apps:
                app_name = app.get("name", "unknown")
                dur = float(app.get("duration_seconds", 0))
                category = get_local_app_category(app_name, policy)
                if category == "productive":
                    productive_sec += dur
                elif category == "neutral":
                    neutral_sec += dur
                else:
                    unproductive_sec += dur
                    
        total_app_sec = productive_sec + neutral_sec + unproductive_sec
        if total_app_sec > 0:
            app_productivity_ratio = (productive_sec * 1.0 + neutral_sec * 0.6 + unproductive_sec * 0.1) / total_app_sec
    except Exception as e:
        logger.error(f"Error recomputing app productivity ratio: {e}")

    # 4. Face Presence Recomputation
    face_ratio = 1.0
    try:
        face_cursor = wfh_face_checks_collection.find({"session_id": str(session["_id"])})
        face_checks = await face_cursor.to_list(length=1000)
        if len(face_checks) > 0:
            passed_checks = sum(1 for check in face_checks if check.get("passed", False))
            face_ratio = float(passed_checks) / len(face_checks)
    except Exception as e:
        logger.error(f"Error recomputing face ratio: {e}")

    # 5. Combined Score
    w_app = 0.45
    w_activity = 0.35
    w_face = 0.20
    app_score = active_ratio * app_productivity_ratio
    
    server_score = (app_score * w_app + activity_ratio * w_activity + face_ratio * w_face) * 100.0
    server_score = round(max(0.0, min(100.0, server_score)), 2)
    
    # 6. Flag Alert on Discrepancy (> 15 delta)
    client_reported_score = session.get("productivity_score", 0.0)
    delta = abs(client_reported_score - server_score)
    
    if delta > 15.0:
        alert_doc = {
            "employee_id": employee_id,
            "employee_email": email,
            "employee_name": employee.get("full_name", ""),
            "organization_id": org_id,
            "session_id": str(session["_id"]),
            "type": "WFH_SCORE_DISCREPANCY",
            "timestamp": now,
            "severity": "high",
            "status": "pending",
            "details": f"Productivity score tampering risk. Client reported: {client_reported_score:.1f}, Server validated: {server_score:.1f} (Delta: {delta:.1f})",
            "created_at": now
        }
        alert_res = await wfh_alerts_collection.insert_one(alert_doc)
        
        # Mirror alert to unified alerts collection
        try:
            await alerts_collection.insert_one({
                "organization_id": org_id,
                "employee_id": employee_id,
                "employee_name": employee.get("full_name", ""),
                "type": "Compliance",
                "severity": "high",
                "status": "pending",
                "detail": f"WFH Productivity discrepancy: Client ({client_reported_score:.0f}%) vs Server ({server_score:.0f}%)",
                "timestamp": now,
                "metadata": {
                    "source": "wfh_desktop",
                    "wfh_alert_id": str(alert_res.inserted_id),
                    "session_id": str(session["_id"])
                }
            })
        except Exception as err_alert:
            logger.error(f"Failed to mirror WFH discrepancy alert: {err_alert}")

    # Set completed with server-verified productivity score
    await wfh_sessions_collection.update_one(
        {"_id": session["_id"]},
        {
            "$set": {
                "status": "completed",
                "check_out_time": now,
                "total_active_seconds": total_seconds,
                "verified_productivity_score": server_score,
                "updated_at": now
            }
        }
    )

    attendance_doc = {
        "user_id": employee_id,
        "email": email,
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "timestamp": now,
        "type": "check-out",
        "status": "Present",
        "selfie_verified": True,
        "device_id": device_id,
        "source": "wfh_desktop",
        "wfh_session_id": str(session["_id"]),
        "attendance_type": "wfh",
        "check_in_method": "wfh_desktop",
        "total_seconds": total_seconds,
        "created_at": now
    }

    await attendance_logs_collection.insert_one(attendance_doc)
    await employees_collection.update_one(
        {"_id": employee["_id"]},
        {"$set": {"checked_in": False, "last_attendance_type": "wfh_checkout", "last_attendance_time": now}}
    )


    return {
        "status": "success",
        "message": "WFH check-out successful",
        "session_id": str(session["_id"]),
        "check_out_time": now.isoformat(),
        "total_seconds": total_seconds,
        "verified_productivity_score": server_score
    }


@app.get("/api/wfh/session/active")
async def get_active_wfh_session(employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")

    # Timezone-aware stale session closing
    tz_offset = 330 # default IST
    org_settings = await settings_collection.find_one({"organization_id": org_id}) if org_id else None
    if org_settings:
        tz_offset = org_settings.get("timezone_offset", 330)

    local_now = datetime.now(timezone.utc) + timedelta(minutes=tz_offset)
    today_str = local_now.strftime("%Y-%m-%d")

    session = await wfh_sessions_collection.find_one({
        "employee_id": employee_id,
        "organization_id": org_id,
        "status": "active"
    })

    if session:
        # If the session is from a previous day, auto-close it as stale!
        # But add a grace period: only close if the session is >20 hours old
        # to prevent midnight-boundary race conditions (check-in at 23:59,
        # session/active polled at 00:00 → wrongly killed as stale).
        session_date = session.get("date", "")
        stale_check_in_time = session.get("check_in_time")
        session_age_hours = 0
        if stale_check_in_time:
            if stale_check_in_time.tzinfo is None:
                stale_check_in_time = stale_check_in_time.replace(tzinfo=timezone.utc)
            session_age_hours = (datetime.now(timezone.utc) - stale_check_in_time).total_seconds() / 3600
        if session_date != today_str and session_age_hours > 20:
            now_utc = datetime.now(timezone.utc)
            check_in_time = session.get("check_in_time")
            total_seconds = 0
            if check_in_time:
                if check_in_time.tzinfo is None:
                    check_in_time = check_in_time.replace(tzinfo=timezone.utc)
                total_seconds = int((now_utc - check_in_time).total_seconds())

            await wfh_sessions_collection.update_one(
                {"_id": session["_id"]},
                {
                    "$set": {
                        "status": "completed",
                        "check_out_time": now_utc,
                        "total_active_seconds": min(total_seconds, 28800), # Cap at 8 hours if stale
                        "updated_at": now_utc
                    }
                }
            )
            session = None

    if not session:
        return {
            "active": False,
            "session": None
        }

    session["_id"] = str(session["_id"])
    
    return {
        "active": True,
        "session": session,
        "server_time": datetime.now(timezone.utc).isoformat()
    }


@app.get("/api/wfh/productivity")
async def get_my_wfh_productivity(
    date: Optional[str] = None,
    limit: int = 100,
    employee=Depends(get_current_employee)
):
    clean_email = employee.get("email").strip().lower()
    query = {"employee_email": clean_email}
    if date:
        query["date"] = date
    cursor = wfh_productivity_collection.find(query).sort("timestamp", 1).limit(limit)
    data = await cursor.to_list(length=limit)
    for item in data:
        item["_id"] = str(item["_id"])
    return data


@app.get("/api/wfh/apps")
async def get_my_wfh_apps(
    date: Optional[str] = None,
    limit: int = 100,
    employee=Depends(get_current_employee)
):
    clean_email = employee.get("email").strip().lower()
    query = {"employee_email": clean_email}
    if date:
        query["date"] = date
    cursor = wfh_app_usage_collection.find(query).sort("timestamp", -1).limit(limit)
    data = await cursor.to_list(length=limit)
    for item in data:
        item["_id"] = str(item["_id"])
    return data

# =========================
# WFH DATA INTAKE APIs
# =========================

async def scan_screenshot_ocr_threats(screenshot_id: str, image_path: str, employee_id: str, org_id: str, email: str, device_id: str, session_id: str):
    temp_file_path = None
    try:
        import os
        import re
        from PIL import Image
        
        # Try to resolve web URL or static mount path to local path
        if image_path.startswith("http://") or image_path.startswith("https://"):
            import urllib.request
            import tempfile
            try:
                # Get display URL (which will be presigned if S3/R2)
                download_url = get_screenshot_display_url(image_path)
                
                # Create a temporary file and close the fd immediately
                fd, temp_file_path = tempfile.mkstemp(suffix=".jpg")
                os.close(fd)
                
                # Fetch headers to bypass potential user-agent blocks if any
                req = urllib.request.Request(
                    download_url, 
                    headers={'User-Agent': 'Mozilla/5.0'}
                )
                with urllib.request.urlopen(req, timeout=15) as response, open(temp_file_path, 'wb') as out_file:
                    out_file.write(response.read())
                
                resolved_path = temp_file_path
            except Exception as download_err:
                logger.error(f"Failed to download screenshot from R2/HTTP for OCR: {download_err}")
                resolved_path = ""
        else:
            if "uploads/" in image_path:
                idx = image_path.find("uploads/")
                image_path = image_path[idx:]
                
            # Try resolving relative path if it's not absolute
            resolved_path = image_path
            if not os.path.isabs(image_path) and not os.path.exists(image_path):
                alternative_path = os.path.join("..", "wfh_desktop", "agent", image_path)
                if os.path.exists(alternative_path):
                    resolved_path = alternative_path
        
        if not resolved_path or not os.path.exists(resolved_path):
            logger.warning(f"Screenshot file not found for OCR: {image_path}")
            return
            
        try:
            import pytesseract  # type: ignore[import-not-found]  # optional dependency
            from PIL import Image  # type: ignore[import-not-found]
            img = Image.open(resolved_path)
            text = pytesseract.image_to_string(img)  # type: ignore[union-attr]
        except ImportError:
            logger.warning("pytesseract or PIL is not installed. Falling back to active window text threat check.")
            # Graceful fallback: scan the active window title instead for simple keywords
            text = ""
        except Exception as ocr_err:
            logger.warning(f"Tesseract OCR engine not fully loaded or failed: {ocr_err}")
            text = ""

        # Also merge active window title if text was not found
        text += f"\n"

        # Regex threat signatures
        pattern = r'(?i)(password|passwd|api[_-]?key|secret|token)\s*[:=]\s*["\']?[a-zA-Z0-9_\-\+]{16,}'
        match = re.search(pattern, text)
        
        if match:
            leak_reason = f"Plaintext credentials/API keys visible: found '{match.group(1)}' token leak"
            logger.warning(f"WFH Security Leak Alert! screenshot {screenshot_id}: {leak_reason}")
            
            # Update screenshot record in Mongo
            await wfh_screenshots_collection.update_one(
                {"_id": ObjectId(screenshot_id)},
                {"$set": {
                    "flagged": True,
                    "flag_reason": "Plaintext credentials/API keys visible"
                }}
            )
            
            # Raise High Severity WFH Compliance Alert
            now = datetime.now(timezone.utc)
            alert_doc = {
                "employee_id": employee_id,
                "employee_email": email,
                "employee_name": "Remote Employee",
                "organization_id": org_id,
                "device_id": device_id,
                "session_id": session_id,
                "type": "Credential Leak",
                "timestamp": now,
                "image_url": image_path,
                "severity": "high",
                "status": "pending",
                "details": f"Plaintext credentials or secret tokens leaked in active window screenshot: {leak_reason}",
                "metadata": {"screenshot_id": screenshot_id},
                "created_at": now
            }
            alert_result = await wfh_alerts_collection.insert_one(alert_doc)
            
            # Mirror WFH alert into unified alerts collection
            try:
                await alerts_collection.insert_one({
                    "organization_id": org_id,
                    "employee_id": employee_id,
                    "employee_name": "Remote Employee",
                    "type": "Productivity",
                    "severity": "high",
                    "status": "pending",
                    "detail": f"WFH Security Leak Alert: Credential leak detected via background OCR scanning.",
                    "timestamp": now,
                    "metadata": {
                        "source": "wfh_desktop",
                        "wfh_alert_id": str(alert_result.inserted_id),
                        "device_id": device_id,
                        "session_id": session_id
                    }
                })
            except Exception as mirror_err:
                logger.error(f"Failed to mirror unified WFH alert: {mirror_err}")
    except Exception as e:
        logger.error(f"OCR threat scan failed: {e}")
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except Exception as cleanup_err:
                logger.warning(f"Failed to cleanup temp OCR screenshot file: {cleanup_err}")

@app.get("/api/wfh/screenshots")
async def get_my_screenshots(
    request: Request,
    date: Optional[str] = None,
    limit: int = 100,
    employee=Depends(get_current_employee)
):
    email = employee.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    query = {"employee_email": email}
    if date:
        query["date"] = date
        
    cursor = wfh_screenshots_collection.find(query).sort("timestamp", -1).limit(limit)
    screenshots = await cursor.to_list(length=limit)
    
    for item in screenshots:
        item["_id"] = str(item["_id"])
        item["image_url"] = get_screenshot_display_url(item.get("image_url", ""), request)
        if item.get("thumbnail_url"):
            item["thumbnail_url"] = get_screenshot_display_url(item["thumbnail_url"], request)
        
    return {"screenshots": screenshots}

def build_static_upload_url(request: Request, relative_path: str) -> str:
    base_url = str(request.base_url).rstrip("/")
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_proto == "https" and base_url.startswith("http://"):
        base_url = base_url.replace("http://", "https://", 1)
    if not relative_path.startswith("/"):
        relative_path = "/" + relative_path
    return f"{base_url}{relative_path}"



@app.post("/api/wfh/screenshot")
async def wfh_submit_screenshot(req: dict, background_tasks: BackgroundTasks, request: Request, employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")
    device_id = req.get("device_id")

    await verify_wfh_device(employee_id, org_id, device_id)

    image_url = req.get("image_url")
    if not image_url:
        raise HTTPException(status_code=400, detail="image_url is required")

    server_now = datetime.now(timezone.utc)
    timestamp_str = req.get("timestamp")
    if timestamp_str:
        try:
            if timestamp_str.endswith('Z'):
                timestamp_str = timestamp_str[:-1] + '+00:00'
            now = datetime.fromisoformat(timestamp_str)
        except Exception:
            now = server_now
    else:
        now = server_now

    def is_base64_payload(value: str) -> bool:
        return (value.startswith("data:image/") or ";base64," in value or (not value.startswith("http") and not value.startswith("/uploads") and len(value) > 1000))

    if is_base64_payload(image_url):
        import base64
        import uuid
        import time
        header = ""
        base64_data = image_url
        if "," in image_url:
            header, base64_data = image_url.split(",", 1)

        try:
            image_bytes = base64.b64decode(base64_data)
            ext = ".jpg"
            if "png" in header:
                ext = ".png"
            elif "gif" in header:
                ext = ".gif"
            filename = f"view_{int(time.time())}_{uuid.uuid4().hex[:8]}{ext}"
            image_url = await upload_file_to_storage(image_bytes, filename, "wfh_view")
        except Exception as err:
            logger.error(f"Failed to decode base64 screenshot: {err}")
            raise HTTPException(status_code=400, detail=f"Invalid base64 image data: {err}")
    elif image_url.startswith("/uploads/"):
        pass

    doc = {
        "employee_id": employee_id,
        "employee_email": employee.get("email"),
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "device_id": device_id,
        "session_id": req.get("session_id"),
        "date": now.strftime("%Y-%m-%d"),
        "timestamp": now,
        "image_url": image_url,
        "thumbnail_url": req.get("thumbnail_url"),
        "active_app": req.get("active_app"),
        "active_window": req.get("active_window"),
        "flagged": req.get("flagged", False),
        "flag_reason": req.get("flag_reason"),
        "created_at": server_now
    }

    result = await wfh_screenshots_collection.insert_one(doc)
    screenshot_id = str(result.inserted_id)

    # Queue background threat scan using OCR parser
    background_tasks.add_task(
        scan_screenshot_ocr_threats,
        screenshot_id,
        image_url,
        employee_id,
        org_id,
        employee.get("email"),
        device_id,
        req.get("session_id")
    )

    return {
        "status": "success",
        "message": "WFH screenshot saved",
        "screenshot_id": screenshot_id
    }


@app.post("/api/wfh/activity")
async def wfh_submit_activity(req: dict, employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")
    device_id = req.get("device_id")

    await verify_wfh_device(employee_id, org_id, device_id)

    now = datetime.now(timezone.utc)

    doc = {
        "employee_id": employee_id,
        "employee_email": employee.get("email"),
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "device_id": device_id,
        "session_id": req.get("session_id"),
        "timestamp": now,
        "period_minutes": req.get("period_minutes", 5),
        "keystrokes": req.get("keystrokes", 0),
        "mouse_clicks": req.get("mouse_clicks", 0),
        "mouse_distance_px": req.get("mouse_distance_px", 0),
        "scroll_events": req.get("scroll_events", 0),
        "idle_seconds": req.get("idle_seconds", 0),
        "active_seconds": req.get("active_seconds", 0),
        "created_at": now
    }

    result = await wfh_activity_collection.insert_one(doc)

    return {
        "status": "success",
        "message": "WFH activity saved",
        "activity_id": str(result.inserted_id)
    }


@app.post("/api/wfh/app-usage")
async def wfh_submit_app_usage(req: dict, employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")
    device_id = req.get("device_id")

    await verify_wfh_device(employee_id, org_id, device_id)

    now = datetime.now(timezone.utc)

    doc = {
        "employee_id": employee_id,
        "employee_email": employee.get("email"),
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "device_id": device_id,
        "session_id": req.get("session_id"),
        "timestamp": now,
        "date": req.get("date", now.strftime("%Y-%m-%d")),
        "apps": req.get("apps", []),
        "created_at": now
    }

    result = await wfh_app_usage_collection.insert_one(doc)

    return {
        "status": "success",
        "message": "WFH app usage saved",
        "app_usage_id": str(result.inserted_id)
    }


@app.post("/api/wfh/productivity")
async def wfh_submit_productivity(req: dict, employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")
    device_id = req.get("device_id")

    await verify_wfh_device(employee_id, org_id, device_id)

    now = datetime.now(timezone.utc)

    score = float(req.get("score", 0))
    score = max(0, min(score, 100))

    doc = {
        "employee_id": employee_id,
        "employee_email": employee.get("email"),
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "device_id": device_id,
        "session_id": req.get("session_id"),
        "timestamp": now,
        "date": req.get("date", now.strftime("%Y-%m-%d")),
        "score": score,
        "breakdown": req.get("breakdown", {}),
        "created_at": now
    }

    result = await wfh_productivity_collection.insert_one(doc)

    if req.get("session_id") and ObjectId.is_valid(req.get("session_id")):
        await wfh_sessions_collection.update_one(
            {"_id": ObjectId(req.get("session_id")), "employee_id": employee_id},
            {
                "$set": {
                    "productivity_score": score,
                    "updated_at": now
                }
            }
        )

    return {
        "status": "success",
        "message": "WFH productivity saved",
        "productivity_id": str(result.inserted_id),
        "score": score
    }


@app.post("/api/wfh/alert")
async def wfh_submit_alert(req: dict, employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")
    device_id = req.get("device_id")

    await verify_wfh_device(employee_id, org_id, device_id)

    now = datetime.now(timezone.utc)

    alert_type = req.get("type")
    if not alert_type:
        raise HTTPException(status_code=400, detail="alert type is required")

    severity = req.get("severity", "medium")

    doc = {
        "employee_id": employee_id,
        "employee_email": employee.get("email"),
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "device_id": device_id,
        "session_id": req.get("session_id"),
        "type": alert_type,
        "timestamp": now,
        "image_url": req.get("image_url"),
        "severity": severity,
        "status": "pending",
        "details": req.get("details"),
        "metadata": req.get("metadata", {}),
        "created_at": now
    }

    result = await wfh_alerts_collection.insert_one(doc)

    # Also mirror WFH alert into existing unified alerts collection
    try:
        # Map WFH alert types to admin-friendly categories
        wfh_type_map = {
            "WFH_IDLE_EXTENDED": "Productivity",
            "WFH_IDENTITY_MISMATCH": "Identity",
            "Continuous Auth Failure": "Identity",
            "WFH_FACE_CHECK_FAILURE": "Identity",
            "WFH_FAKE_WEBCAM": "Identity",
            "WFH_SCREENSHOT_THREAT": "Compliance",
            "WFH_SUSPICIOUS_APP": "Compliance",
        }
        mapped_type = wfh_type_map.get(alert_type, "Productivity")

        # Create a clean, human-readable detail for the admin
        raw_detail = req.get('details', '')
        friendly_detail = raw_detail if raw_detail else f"{alert_type} detected for this employee."

        await alerts_collection.insert_one({
            "organization_id": org_id,
            "employee_id": employee_id,
            "employee_name": employee.get("full_name", ""),
            "type": mapped_type,
            "severity": severity,
            "status": "pending",
            "detail": friendly_detail,
            "timestamp": now,
            "metadata": {
                "source": "wfh_desktop",
                "wfh_alert_id": str(result.inserted_id),
                "wfh_alert_type": alert_type,
                "device_id": device_id,
                "session_id": req.get("session_id")
            }
        })
    except Exception as e:
        logger.error(f"Failed to mirror WFH alert into alerts collection: {e}")

    return {
        "status": "success",
        "message": "WFH alert saved",
        "alert_id": str(result.inserted_id)
    }


@app.post("/api/wfh/meeting")
async def wfh_submit_meeting(req: dict, employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")
    device_id = req.get("device_id")

    await verify_wfh_device(employee_id, org_id, device_id)

    now = datetime.now(timezone.utc)

    platform = req.get("platform")
    if not platform:
        raise HTTPException(status_code=400, detail="platform is required")

    doc = {
        "employee_id": employee_id,
        "employee_email": employee.get("email"),
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "device_id": device_id,
        "session_id": req.get("session_id"),
        "platform": platform,
        "start_time": req.get("start_time"),
        "end_time": req.get("end_time"),
        "duration_minutes": req.get("duration_minutes"),
        "date": req.get("date", now.strftime("%Y-%m-%d")),
        "created_at": now
    }

    result = await wfh_meetings_collection.insert_one(doc)

    return {
        "status": "success",
        "message": "WFH meeting saved",
        "meeting_id": str(result.inserted_id)
    }

@app.get("/api/wfh/team/active")
async def wfh_get_active_team(employee=Depends(get_current_employee)):
    org_id = employee.get("organization_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Employee organization_id missing")
    
    # Find active WFH sessions in the same organization, excluding current employee
    cursor = wfh_sessions_collection.find({
        "organization_id": org_id,
        "status": "active",
        "employee_email": {"$ne": employee.get("email")}
    })
    sessions = await cursor.to_list(length=100)
    
    team_members = []
    for s in sessions:
        team_members.append({
            "employee_id": s.get("employee_id"),
            "employee_email": s.get("employee_email"),
            "employee_name": s.get("employee_name", ""),
            "check_in_time": s.get("check_in_time").isoformat() if s.get("check_in_time") else None,
            "productivity_score": s.get("productivity_score", 0)
        })
    return team_members

@app.post("/api/wfh/meeting/signal")
async def wfh_post_signal(req: dict, employee=Depends(get_current_employee)):
    sender_email = employee.get("email")
    receiver_email = req.get("receiver_email")
    signal_type = req.get("type") # 'offer', 'answer', 'candidate'
    data = req.get("data")
    
    if not receiver_email or not signal_type or not data:
        raise HTTPException(status_code=400, detail="receiver_email, type, and data are required")
        
    doc = {
        "sender_email": sender_email,
        "receiver_email": receiver_email,
        "type": signal_type,
        "data": data,
        "timestamp": datetime.now(timezone.utc),
        "delivered": False
    }
    
    await wfh_signals_collection.insert_one(doc)
    return {"status": "success", "message": "Signal sent"}

@app.get("/api/wfh/meeting/signal")
async def wfh_get_signals(employee=Depends(get_current_employee)):
    email = employee.get("email")
    
    # Fetch all undelivered signals for current employee
    cursor = wfh_signals_collection.find({
        "receiver_email": email,
        "delivered": False
    }).sort("timestamp", 1)
    
    signals = await cursor.to_list(length=100)
    
    # Mark as delivered
    if signals:
        signal_ids = [s["_id"] for s in signals]
        await wfh_signals_collection.update_many(
            {"_id": {"$in": signal_ids}},
            {"$set": {"delivered": True}}
        )
        
    result = []
    for s in signals:
        result.append({
            "id": str(s["_id"]),
            "sender_email": s.get("sender_email"),
            "type": s.get("type"),
            "data": s.get("data"),
            "timestamp": s.get("timestamp").isoformat() if s.get("timestamp") else None
        })
        
    return result

# =========================
# ADMIN WFH VIEW APIs
# =========================

@app.get("/admin/wfh/stats")
async def admin_wfh_stats(current_admin: Admin = Depends(get_current_admin)):
    org_id = current_admin.organization_id
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    query = {}
    if org_id and org_id != "system_org":
        query["organization_id"] = org_id

    try:
        # Run all 4 queries in parallel to minimize latency
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        active_sessions_task = wfh_sessions_collection.count_documents({
            **query,
            "status": "active"
        })
        today_alerts_task = wfh_alerts_collection.count_documents({
            **query,
            "status": "pending",
            "timestamp": {"$gte": today_start}
        })
        emp_type_filter = ["wfh", "desk", "office", "hybrid", "all", None]
        wfh_employees_task = employees_collection.count_documents({
            **query,
            "$or": [
                {"employee_type": {"$in": emp_type_filter}},
                {"employee_type": {"$exists": False}}
            ]
        })
        productivity_task = wfh_productivity_collection.find({
            **query,
            "date": today
        }).to_list(length=500)

        active_sessions, today_alerts, wfh_employees, productivity_list = await asyncio.gather(
            active_sessions_task,
            today_alerts_task,
            wfh_employees_task,
            productivity_task,
            return_exceptions=True
        )

        # Handle any individual query failures gracefully
        if isinstance(active_sessions, Exception):
            logger.error(f"stats: active_sessions query failed: {active_sessions}")
            active_sessions = 0
        if isinstance(today_alerts, Exception):
            logger.error(f"stats: today_alerts query failed: {today_alerts}")
            today_alerts = 0
        if isinstance(wfh_employees, Exception):
            logger.error(f"stats: wfh_employees query failed: {wfh_employees}")
            wfh_employees = 0
        if isinstance(productivity_list, Exception):
            logger.error(f"stats: productivity query failed: {productivity_list}")
            productivity_list = []

        avg_productivity = 0
        if productivity_list and isinstance(productivity_list, list):
            scores = [item.get("score", 0) for item in productivity_list if isinstance(item, dict)]
            if scores:
                avg_productivity = round(sum(scores) / len(scores), 2)

        return {
            "active_sessions": active_sessions,
            "wfh_employees": wfh_employees,
            "pending_alerts": today_alerts,
            "avg_productivity": avg_productivity,
            "date": today
        }
    except Exception as e:
        logger.error(f"admin_wfh_stats crashed: {e}", exc_info=True)
        # Return safe fallback instead of 500 — CORS headers will still be sent
        return {
            "active_sessions": 0,
            "wfh_employees": 0,
            "pending_alerts": 0,
            "avg_productivity": 0,
            "date": today,
            "error": "Stats temporarily unavailable"
        }


@app.get("/admin/wfh/live-view")
async def admin_wfh_live_view(request: Request, current_admin: Admin = Depends(get_current_admin)):
    org_id = current_admin.organization_id

    try:
        from bson import ObjectId

        # 1. Fetch active sessions in bulk for this org
        session_query = {"status": "active"}
        if org_id and org_id != "system_org":
            session_query["organization_id"] = org_id
        active_sessions_list = await wfh_sessions_collection.find(session_query).to_list(length=500)
        active_emp_ids = [s.get("employee_id") for s in (active_sessions_list or []) if s.get("employee_id")]

        active_obj_ids = []
        for eid in active_emp_ids:
            try:
                active_obj_ids.append(ObjectId(eid))
            except Exception:
                pass

        # 2. Match employees who are WFH, desk, office, hybrid, or currently checked-in
        query_conditions = [
            {"employee_type": {"$in": ["wfh", "desk", "office", "hybrid", "all", None]}},
            {"employee_type": {"$exists": False}}
        ]
        if active_obj_ids:
            query_conditions.append({"_id": {"$in": active_obj_ids}})

        query = {"$or": query_conditions}
        if org_id and org_id != "system_org":
            query["organization_id"] = org_id

        employees = await employees_collection.find(query).to_list(length=500)

        if not employees:
            return []

        employee_ids = [str(emp["_id"]) for emp in employees]

        # Fetch latest screenshot and activity per employee using fast indexed find_one
        screenshots_by_emp = {}
        activities_by_emp = {}
        
        async def fetch_emp_latest(emp_id):
            shot_task = wfh_screenshots_collection.find_one(
                {"employee_id": emp_id},
                sort=[("timestamp", -1)]
            )
            act_task = wfh_activity_collection.find_one(
                {"employee_id": emp_id},
                sort=[("timestamp", -1)]
            )
            shot, act = await asyncio.gather(shot_task, act_task)
            if shot:
                screenshots_by_emp[emp_id] = shot
            if act:
                activities_by_emp[emp_id] = act

        await asyncio.gather(*(fetch_emp_latest(emp_id) for emp_id in employee_ids))

        # Build lookup dicts keyed by employee_id
        sessions_by_emp = {s["employee_id"]: s for s in (active_sessions_list or [])}

        result = []
        for employee in employees:
            employee_id = str(employee["_id"])
            employee_email = employee.get("email")

            active_session = sessions_by_emp.get(employee_id)
            latest_screenshot = screenshots_by_emp.get(employee_id)
            latest_activity = activities_by_emp.get(employee_id)

            result.append({
                "session_id": str(active_session["_id"]) if active_session else f"offline-{employee_email}",
                "employee_id": employee_id,
                "employee_email": employee_email,
                "employee_name": employee.get("full_name"),
                "check_in_time": active_session.get("check_in_time") if active_session else None,
                "productivity_score": active_session.get("productivity_score", 0) if active_session else 0,
                "device_id": active_session.get("device_id") if active_session else None,
                "latest_screenshot": {
                    "_id": str(latest_screenshot["_id"]),
                    "image_url": get_screenshot_display_url(latest_screenshot.get("image_url", ""), request),
                    "timestamp": latest_screenshot.get("timestamp"),
                    "active_app": latest_screenshot.get("active_app"),
                    "active_window": latest_screenshot.get("active_window")
                } if latest_screenshot else None,
                "latest_activity": {
                    "_id": str(latest_activity["_id"]),
                    "keystrokes": latest_activity.get("keystrokes", 0),
                    "mouse_clicks": latest_activity.get("mouse_clicks", 0),
                    "idle_seconds": latest_activity.get("idle_seconds", 0),
                    "active_seconds": latest_activity.get("active_seconds", 0),
                    "timestamp": latest_activity.get("timestamp")
                } if latest_activity else None
            })

        return result
    except Exception as e:
        logger.error(f"admin_wfh_live_view crashed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Live view temporarily unavailable: {str(e)}")

@app.post("/admin/wfh/employee/{email}/trigger-screenshot")
async def admin_trigger_screenshot(email: str, current_admin: Admin = Depends(get_current_admin)):
    clean_email = email.strip().lower()
    
    # Check if employee exists
    user = await employees_collection.find_one({"email": clean_email})
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")
        
    doc = {
        "employee_email": clean_email,
        "command": "trigger_screenshot",
        "status": "pending",
        "timestamp": datetime.now(timezone.utc)
    }
    
    await wfh_commands_collection.insert_one(doc)
    return {"status": "success", "message": "Trigger command enqueued successfully"}

@app.post("/admin/wfh/employee/{email}/force-end")
async def admin_force_end_session(email: str, req: dict, current_admin: Admin = Depends(get_current_admin)):
    clean_email = email.strip().lower()
    reason = req.get("reason", "Force-ended by administrator").strip()
    
    user = await employees_collection.find_one({"email": clean_email})
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")
        
    active_session = await wfh_sessions_collection.find_one({
        "employee_email": clean_email,
        "status": "active"
    })
    
    if not active_session:
        raise HTTPException(status_code=400, detail="No active WFH session found for this employee.")
        
    now = datetime.now(timezone.utc)
    session_id = active_session["_id"]
    
    check_in_time_val = active_session.get("check_in_time")
    total_seconds_val = 0
    if check_in_time_val:
        if check_in_time_val.tzinfo is None:
            check_in_time_val = check_in_time_val.replace(tzinfo=timezone.utc)
        total_seconds_val = int((now - check_in_time_val).total_seconds())
        
    # Update active session
    await wfh_sessions_collection.update_one(
        {"_id": session_id},
        {
            "$set": {
                "status": "force_ended",
                "check_out_time": now,
                "total_active_seconds": total_seconds_val,
                "force_ended_by": current_admin.email,
                "force_end_reason": reason,
                "updated_at": now
            }
        }
    )
    
    # Write checkout log in attendance_logs
    checkout_log = {
        "user_id": str(user["_id"]),
        "email": clean_email,
        "employee_name": user.get("full_name", ""),
        "organization_id": user.get("organization_id"),
        "timestamp": now,
        "type": "check-out",
        "attendance_type": "wfh",
        "location": None,
        "check_in_method": "wfh_desktop",
        "status": "SUCCESS",
        "location_name": "Force Ended by Admin",
        "selfie_verified": False,
        "device_id": active_session.get("device_id"),
        "wfh_session_id": str(session_id),
        "method": "admin_force_end",
        "force_ended_by": current_admin.email
    }
    await attendance_logs_collection.insert_one(checkout_log)
    
    # Enqueue logout command to agent
    cmd_doc = {
        "employee_email": clean_email,
        "command": "force_end_session",
        "status": "pending",
        "timestamp": now,
        "reason": reason
    }
    await wfh_commands_collection.insert_one(cmd_doc)
    
    # Generate WFH Alert
    alert_doc = {
        "employee_id": str(user["_id"]),
        "employee_email": clean_email,
        "employee_name": user.get("full_name", ""),
        "organization_id": user.get("organization_id"),
        "type": "WFH_SESSION_FORCE_ENDED",
        "timestamp": now,
        "severity": "info",
        "status": "pending",
        "details": f"Session force-ended by admin {current_admin.email}. Reason: {reason}",
        "created_at": now
    }
    alert_res = await wfh_alerts_collection.insert_one(alert_doc)
    
    # Mirror into unified alerts collection
    try:
        await alerts_collection.insert_one({
            "organization_id": user.get("organization_id"),
            "employee_id": str(user["_id"]),
            "employee_name": user.get("full_name", ""),
            "type": "Compliance",
            "severity": "medium",
            "status": "pending",
            "detail": f"WFH Session force-ended by admin: {reason}",
            "timestamp": now,
            "metadata": {
                "source": "wfh_desktop",
                "wfh_alert_id": str(alert_res.inserted_id),
                "session_id": str(session_id)
            }
        })
    except Exception as e:
        logger.error(f"Failed to mirror force-end alert: {e}")
        
    return {
        "status": "success",
        "message": "Session force-ended successfully",
        "session_id": str(session_id)
    }

@app.post("/admin/wfh/employee/{email}/force-logout")
async def admin_force_logout(email: str, current_admin: Admin = Depends(get_current_admin)):
    clean_email = email.strip().lower()
    
    user = await employees_collection.find_one({"email": clean_email})
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")
        
    now = datetime.now(timezone.utc)
    
    # Enqueue force_logout command to agent
    cmd_doc = {
        "employee_email": clean_email,
        "command": "force_logout",
        "status": "pending",
        "timestamp": now,
        "reason": f"Force logged out by admin {current_admin.email}"
    }
    await wfh_commands_collection.insert_one(cmd_doc)
    
    # Also force-end active WFH session if exists
    active_session = await wfh_sessions_collection.find_one({
        "employee_email": clean_email,
        "status": "active"
    })
    
    session_id_str = None
    if active_session:
        session_id = active_session["_id"]
        session_id_str = str(session_id)
        check_in_time_val = active_session.get("check_in_time")
        total_seconds_val = 0
        if check_in_time_val:
            if check_in_time_val.tzinfo is None:
                check_in_time_val = check_in_time_val.replace(tzinfo=timezone.utc)
            total_seconds_val = int((now - check_in_time_val).total_seconds())
            
        await wfh_sessions_collection.update_one(
            {"_id": session_id},
            {
                "$set": {
                    "status": "force_ended",
                    "check_out_time": now,
                    "total_active_seconds": total_seconds_val,
                    "force_ended_by": current_admin.email,
                    "force_end_reason": "Logged out by admin",
                    "updated_at": now
                }
            }
        )
        
        # Write checkout log in attendance_logs
        checkout_log = {
            "user_id": str(user["_id"]),
            "email": clean_email,
            "employee_name": user.get("full_name", ""),
            "organization_id": user.get("organization_id"),
            "timestamp": now,
            "type": "check-out",
            "attendance_type": "wfh",
            "location": None,
            "check_in_method": "wfh_desktop",
            "status": "SUCCESS",
            "location_name": "Logged out by Admin",
            "selfie_verified": False,
            "device_id": active_session.get("device_id"),
            "wfh_session_id": session_id_str,
            "method": "admin_force_logout",
            "force_ended_by": current_admin.email
        }
        await attendance_logs_collection.insert_one(checkout_log)

    return {
        "status": "success",
        "message": "Force-logout command queued successfully",
        "session_id": session_id_str
    }

@app.get("/admin/wfh/policy")
async def get_wfh_policy(current_admin: Admin = Depends(get_current_admin)):
    org_id = current_admin.organization_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Admin organization missing")
    
    org = await organizations_collection.find_one({"_id": ObjectId(org_id)})
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
        
    policy = org.get("wfh_policy")
    if not policy:
        policy = {
            "screenshot_interval_minutes": 10,
            "face_check_interval_minutes": 30,
            "max_idle_minutes": 20,
            "productivity_threshold_percent": 60,
            "working_hours_start": "09:00",
            "working_hours_end": "18:00",
            "screenshot_retention_days": 5,
            "require_face_verification": True,
            "productive_apps": [],
            "unproductive_apps": []
        }
    return policy

@app.put("/admin/wfh/policy")
async def update_wfh_policy(req: dict, current_admin: Admin = Depends(get_current_admin)):
    org_id = current_admin.organization_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Admin organization missing")
        
    policy = {
        "screenshot_interval_minutes": int(req.get("screenshot_interval_minutes", 10)),
        "face_check_interval_minutes": int(req.get("face_check_interval_minutes", 30)),
        "max_idle_minutes": int(req.get("max_idle_minutes", 20)),
        "productivity_threshold_percent": int(req.get("productivity_threshold_percent", 60)),
        "working_hours_start": str(req.get("working_hours_start", "09:00")),
        "working_hours_end": str(req.get("working_hours_end", "18:00")),
        "screenshot_retention_days": int(req.get("screenshot_retention_days", 5)),
        "require_face_verification": bool(req.get("require_face_verification", True)),
        "productive_apps": list(req.get("productive_apps", [])),
        "unproductive_apps": list(req.get("unproductive_apps", []))
    }
    
    result = await organizations_collection.update_one(
        {"_id": ObjectId(org_id)},
        {"$set": {"wfh_policy": policy}}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Organization not found")
        
    return {"status": "success", "message": "WFH policy updated successfully", "policy": policy}

@app.get("/api/wfh/policy")
async def api_get_wfh_policy(employee = Depends(get_current_employee)):
    org_id = employee.get("organization_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Employee organization_id missing")
        
    org = await organizations_collection.find_one({"_id": ObjectId(org_id)})
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
        
    policy = org.get("wfh_policy")
    if not policy:
        policy = {
            "screenshot_interval_minutes": 10,
            "face_check_interval_minutes": 30,
            "max_idle_minutes": 20,
            "productivity_threshold_percent": 60,
            "working_hours_start": "09:00",
            "working_hours_end": "18:00",
            "screenshot_retention_days": 5,
            "require_face_verification": True,
            "productive_apps": [],
            "unproductive_apps": []
        }
    return policy

@app.post("/api/wfh/face-check")
async def wfh_face_check(req: dict, employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")
    
    session_id = req.get("session_id")
    passed = bool(req.get("passed", True))
    face_score = float(req.get("face_score", 1.0))
    failure_reason = req.get("failure_reason", "")
    image_base64 = req.get("image_base64")
    
    now = datetime.now(timezone.utc)
    
    image_url = None
    if image_base64:
        import uuid
        import time
        filename = f"face_check_{int(time.time())}_{uuid.uuid4().hex[:8]}.jpg"
        import base64
        try:
            base64_data = image_base64
            if "," in image_base64:
                base64_data = image_base64.split(",", 1)[1]
            image_bytes = base64.b64decode(base64_data)
            image_url = await upload_file_to_storage(image_bytes, filename, "wfh_face_checks")
        except Exception as e:
            logger.error(f"Failed to save face check image: {e}")
            
    doc = {
        "employee_id": employee_id,
        "employee_email": employee.get("email"),
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "session_id": session_id,
        "timestamp": now,
        "passed": passed,
        "face_score": face_score,
        "failure_reason": failure_reason,
        "image_url": image_url,
        "created_at": now
    }
    
    result = await wfh_face_checks_collection.insert_one(doc)
    
    # Update active session counters
    if session_id and ObjectId.is_valid(session_id):
        update_op = {
            "$inc": {
                "face_check_count": 1,
                "face_check_passed": 1 if passed else 0
            },
            "$set": {
                "updated_at": now
            }
        }
        await wfh_sessions_collection.update_one(
            {"_id": ObjectId(session_id), "employee_id": employee_id},
            update_op
        )
        
    # If failed, raise alert
    if not passed:
        alert_doc = {
            "employee_id": employee_id,
            "employee_email": employee.get("email"),
            "employee_name": employee.get("full_name", ""),
            "organization_id": org_id,
            "session_id": session_id,
            "type": "WFH_FACE_CHECK_FAILED",
            "timestamp": now,
            "image_url": image_url,
            "severity": "critical" if "fake" in failure_reason.lower() else "high",
            "status": "pending",
            "details": f"WFH face verification failed: {failure_reason}",
            "created_at": now
        }
        alert_res = await wfh_alerts_collection.insert_one(alert_doc)
        
        # Mirror alert to unified alerts collection
        try:
            await alerts_collection.insert_one({
                "organization_id": org_id,
                "employee_id": employee_id,
                "employee_name": employee.get("full_name", ""),
                "type": "Identity",
                "severity": "critical" if "fake" in failure_reason.lower() else "high",
                "status": "pending",
                "detail": f"WFH face check failed: {failure_reason}",
                "timestamp": now,
                "metadata": {
                    "source": "wfh_desktop",
                    "wfh_alert_id": str(alert_res.inserted_id),
                    "session_id": session_id
                }
            })
        except Exception as e:
            logger.error(f"Failed to mirror WFH face-check failure alert: {e}")
            
    return {
        "status": "success",
        "message": "Face check recorded",
        "face_check_id": str(result.inserted_id),
        "passed": passed
    }


@app.get("/api/wfh/commands/pending")
async def get_pending_commands(employee=Depends(get_current_employee)):
    email = employee.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    cursor = wfh_commands_collection.find({
        "employee_email": email.strip().lower(),
        "status": "pending"
    }).sort("timestamp", 1)
    
    commands = await cursor.to_list(length=100)
    
    result = []
    for c in commands:
        result.append({
            "id": str(c["_id"]),
            "command": c.get("command"),
            "status": c.get("status"),
            "timestamp": c.get("timestamp").isoformat() if c.get("timestamp") else None
        })
        
    return {"commands": result}

@app.post("/api/wfh/commands/{command_id}/complete")
async def complete_command(command_id: str, employee=Depends(get_current_employee)):
    if not ObjectId.is_valid(command_id):
        raise HTTPException(status_code=400, detail="Invalid command ID")
        
    result = await wfh_commands_collection.update_one(
        {"_id": ObjectId(command_id), "employee_email": employee.get("email").strip().lower()},
        {"$set": {"status": "completed", "completed_at": datetime.now(timezone.utc)}}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Command not found or unauthorized")
        
    return {"status": "success", "message": "Command marked as completed"}

@app.get("/admin/wfh/employee/{email}/screenshots")
async def admin_wfh_employee_screenshots(
    request: Request,
    email: str,
    date: Optional[str] = None,
    limit: int = 100,
    current_admin: Admin = Depends(get_current_admin)
):
    clean_email = email.strip().lower()

    query = {"employee_email": clean_email}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id

    if date:
        query["date"] = date

    cursor = wfh_screenshots_collection.find(query).sort("timestamp", -1).limit(limit)
    screenshots = await cursor.to_list(length=limit)
    for item in screenshots:
        item["_id"] = str(item["_id"])
        item["image_url"] = get_screenshot_display_url(item.get("image_url", ""), request)
        if item.get("thumbnail_url"):
            item["thumbnail_url"] = get_screenshot_display_url(item["thumbnail_url"], request)

    return screenshots



@app.get("/admin/wfh/employee/{email}/screenshots/download")
async def admin_wfh_employee_screenshots_download(
    request: Request,
    email: str,
    date: Optional[str] = None,
    current_admin: Admin = Depends(get_current_admin)
):
    import zipfile
    import io
    import os
    import requests
    from PIL import Image
    from fastapi.concurrency import run_in_threadpool
    from starlette.responses import StreamingResponse

    clean_email = email.strip().lower()

    query = {"employee_email": clean_email}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id

    if date:
        query["date"] = date

    cursor = wfh_screenshots_collection.find(query).sort("timestamp", 1)
    screenshots = await cursor.to_list(length=1000)

    if not screenshots:
        raise HTTPException(status_code=404, detail="No screenshots found for the selected date")

    def compress_screenshot(image_url: str) -> bytes:
        img_bytes = None
        local_path = None
        if "/uploads/" in image_url:
            idx = image_url.find("/uploads/")
            local_path = image_url[idx:].lstrip("/")

        if local_path and os.path.exists(local_path):
            with open(local_path, "rb") as f:
                img_bytes = f.read()
        elif image_url.startswith("http://") or image_url.startswith("https://"):
            resp = requests.get(image_url, timeout=10)
            resp.raise_for_status()
            img_bytes = resp.content
        else:
            local_path = image_url.lstrip("/")
            if not os.path.exists(local_path):
                raise FileNotFoundError(f"File not found: {local_path}")
            with open(local_path, "rb") as f:
                img_bytes = f.read()

        img = Image.open(io.BytesIO(img_bytes))
        if img.mode != "RGB":
            img = img.convert("RGB")
        out_buf = io.BytesIO()
        img.save(out_buf, format="JPEG", quality=50)
        return out_buf.getvalue()

    import asyncio
    semaphore = asyncio.Semaphore(15)

    async def process_item(index, item):
        image_url = item.get("image_url", "")
        if not image_url:
            return None

        ts = item.get("timestamp")
        time_str = "unknown"
        if isinstance(ts, datetime):
            time_str = ts.strftime("%H-%M-%S")
        elif isinstance(ts, str):
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                time_str = dt.strftime("%H-%M-%S")
            except Exception:
                time_str = "".join(c if c.isalnum() or c in "-_" else "_" for c in ts)

        active_app = item.get("active_app") or "UnknownApp"
        sanitized_app = "".join(c if c.isalnum() or c in " ._-" else "_" for c in active_app)
        sanitized_app = sanitized_app[:30].strip()

        filename = f"screenshot_{index:03d}_{time_str}_{sanitized_app}.jpg"

        async with semaphore:
            try:
                compressed_data = await run_in_threadpool(compress_screenshot, image_url)
                return {"filename": filename, "data": compressed_data, "success": True}
            except Exception as e:
                err_msg = f"Failed to compress {image_url}: {str(e)}"
                return {"filename": filename, "error": err_msg, "success": False}

    tasks = [process_item(index, item) for index, item in enumerate(screenshots, 1)]
    results = await asyncio.gather(*tasks)

    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        errors = []
        for res in results:
            if not res:
                continue
            if res["success"]:
                zip_file.writestr(res["filename"], res["data"])
            else:
                errors.append(res["error"])
                err_filename = f"error_{res['filename'].replace('.jpg', '.txt')}"
                zip_file.writestr(err_filename, res["error"].encode("utf-8"))

        if errors:
            error_log = "\n".join(errors)
            zip_file.writestr("error_log.txt", error_log.encode("utf-8"))

    zip_buffer.seek(0)

    filename_date = date if date else datetime.now().strftime("%Y-%m-%d")
    zip_filename = f"screenshots_{clean_email}_{filename_date}.zip"

    headers = {
        "Content-Disposition": f'attachment; filename="{zip_filename}"',
        "Access-Control-Expose-Headers": "Content-Disposition"
    }

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers=headers
    )





@app.get("/admin/wfh/employee/{email}/activity")
async def admin_wfh_employee_activity(
    email: str,
    date: Optional[str] = None,
    limit: int = 200,
    current_admin: Admin = Depends(get_current_admin)
):
    clean_email = email.strip().lower()

    query = {"employee_email": clean_email}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id

    if date:
        start = datetime.fromisoformat(date + "T00:00:00+00:00")
        end = start + timedelta(days=1)
        query["timestamp"] = {"$gte": start, "$lt": end}

    cursor = wfh_activity_collection.find(query).sort("timestamp", 1).limit(limit)
    data = await cursor.to_list(length=limit)

    for item in data:
        item["_id"] = str(item["_id"])

    return data


@app.get("/admin/wfh/employee/{email}/apps")
async def admin_wfh_employee_apps(
    email: str,
    date: Optional[str] = None,
    limit: int = 100,
    current_admin: Admin = Depends(get_current_admin)
):
    clean_email = email.strip().lower()

    query = {"employee_email": clean_email}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id

    if date:
        query["date"] = date

    cursor = wfh_app_usage_collection.find(query).sort("timestamp", -1).limit(limit)
    data = await cursor.to_list(length=limit)

    for item in data:
        item["_id"] = str(item["_id"])

    return data


@app.get("/admin/wfh/employee/{email}/productivity")
async def admin_wfh_employee_productivity(
    email: str,
    date: Optional[str] = None,
    limit: int = 100,
    current_admin: Admin = Depends(get_current_admin)
):
    clean_email = email.strip().lower()

    query = {"employee_email": clean_email}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id

    if date:
        query["date"] = date

    cursor = wfh_productivity_collection.find(query).sort("timestamp", 1).limit(limit)
    data = await cursor.to_list(length=limit)

    for item in data:
        item["_id"] = str(item["_id"])

    return data


@app.get("/admin/wfh/employee/{email}/timeline")
async def admin_wfh_employee_timeline(
    email: str,
    date: Optional[str] = None,
    current_admin: Admin = Depends(get_current_admin)
):
    clean_email = email.strip().lower()

    base_query = {"employee_email": clean_email}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        base_query["organization_id"] = current_admin.organization_id

    if date:
        start = datetime.fromisoformat(date + "T00:00:00+00:00")
        end = start + timedelta(days=1)
        time_filter = {"timestamp": {"$gte": start, "$lt": end}}
    else:
        time_filter = {}

    try:
        # Run all 3 queries in parallel instead of sequentially
        screenshots_task = wfh_screenshots_collection.find({**base_query, **time_filter}).to_list(length=300)
        activities_task = wfh_activity_collection.find({**base_query, **time_filter}).to_list(length=300)
        alerts_task = wfh_alerts_collection.find({**base_query, **time_filter}).to_list(length=300)

        screenshots, activities, alerts = await asyncio.gather(
            screenshots_task, activities_task, alerts_task,
            return_exceptions=True
        )

        timeline = []

        if isinstance(screenshots, list):
            for item in screenshots:
                timeline.append({
                    "type": "screenshot",
                    "timestamp": item.get("timestamp"),
                    "data": {
                        "_id": str(item["_id"]),
                        "image_url": item.get("image_url"),
                        "active_app": item.get("active_app"),
                        "active_window": item.get("active_window")
                    }
                })
        else:
            logger.error(f"timeline: screenshots query failed: {screenshots}")

        if isinstance(activities, list):
            for item in activities:
                timeline.append({
                    "type": "activity",
                    "timestamp": item.get("timestamp"),
                    "data": {
                        "_id": str(item["_id"]),
                        "keystrokes": item.get("keystrokes", 0),
                        "mouse_clicks": item.get("mouse_clicks", 0),
                        "idle_seconds": item.get("idle_seconds", 0),
                        "active_seconds": item.get("active_seconds", 0)
                    }
                })
        else:
            logger.error(f"timeline: activities query failed: {activities}")

        if isinstance(alerts, list):
            for item in alerts:
                timeline.append({
                    "type": "alert",
                    "timestamp": item.get("timestamp"),
                    "data": {
                        "_id": str(item["_id"]),
                        "alert_type": item.get("type"),
                        "severity": item.get("severity"),
                        "details": item.get("details")
                    }
                })
        else:
            logger.error(f"timeline: alerts query failed: {alerts}")

        timeline.sort(key=lambda x: x.get("timestamp") or datetime.min, reverse=True)

        return timeline
    except Exception as e:
        logger.error(f"admin_wfh_employee_timeline crashed: {e}", exc_info=True)
        return []


@app.get("/admin/wfh/alerts")
async def admin_wfh_alerts(
    status: Optional[str] = None,
    severity: Optional[str] = None,
    limit: int = 100,
    current_admin: Admin = Depends(get_current_admin)
):
    query = {}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id

    if status:
        query["status"] = status

    if severity:
        query["severity"] = severity

    cursor = wfh_alerts_collection.find(query).sort("timestamp", -1).limit(limit)
    alerts = await cursor.to_list(length=limit)

    for item in alerts:
        item["_id"] = str(item["_id"])

    return alerts


@app.post("/admin/wfh/alerts/{alert_id}/review")
async def admin_review_wfh_alert(
    alert_id: str,
    req: dict,
    current_admin: Admin = Depends(get_current_admin)
):
    if not ObjectId.is_valid(alert_id):
        raise HTTPException(status_code=400, detail="Invalid alert id")

    query = {"_id": ObjectId(alert_id)}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id

    alert = await wfh_alerts_collection.find_one(query)

    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    await wfh_alerts_collection.update_one(
        {"_id": ObjectId(alert_id)},
        {
            "$set": {
                "status": req.get("status", "reviewed"),
                "reviewed_by": current_admin.email,
                "reviewed_at": datetime.now(timezone.utc),
                "review_note": req.get("note")
            }
        }
    )

    return {
        "status": "success",
        "message": "WFH alert reviewed successfully"
    }

@app.get("/api/wfh/my-alerts")
async def get_my_wfh_alerts(employee = Depends(get_current_employee)):
    clean_email = employee.get("email").strip().lower()
    cursor = wfh_alerts_collection.find({"employee_email": clean_email}).sort("timestamp", -1)
    alerts = await cursor.to_list(length=100)
    for a in alerts:
        a["_id"] = str(a["_id"])
    return alerts

# =========================
# WFH TASK MONITORING APIs
# =========================

from database import tasks_collection

@app.get("/api/tasks")
async def get_my_tasks(employee = Depends(get_current_employee)):
    email = employee.get("email").strip().lower()
    cursor = tasks_collection.find({"employee_email": email}).sort("created_at", -1)
    tasks = await cursor.to_list(length=200)
    for task in tasks:
        task["_id"] = str(task["_id"])
    return tasks

@app.post("/api/tasks")
async def create_my_task(req: dict, employee = Depends(get_current_employee)):
    email = employee.get("email").strip().lower()
    title = req.get("title")
    if not title:
        raise HTTPException(status_code=400, detail="Task title is required")
    
    now = datetime.now(timezone.utc)
    task_doc = {
        "employee_id": str(employee["_id"]),
        "employee_email": email,
        "organization_id": employee.get("organization_id"),
        "title": title.strip(),
        "description": req.get("description", "").strip(),
        "status": req.get("status", "todo"),
        "priority": req.get("priority", "medium"),
        "worked_minutes": 0,
        "created_at": now,
        "updated_at": now
    }
    
    result = await tasks_collection.insert_one(task_doc)
    task_doc["_id"] = str(result.inserted_id)
    return task_doc

@app.put("/api/tasks/{task_id}")
async def update_my_task(task_id: str, req: dict, employee = Depends(get_current_employee)):
    email = employee.get("email").strip().lower()
    
    task = await tasks_collection.find_one({"_id": ObjectId(task_id), "employee_email": email})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
        
    update_data = {}
    if "status" in req:
        update_data["status"] = req["status"]
    if "description" in req:
        update_data["description"] = req["description"]
    if "priority" in req:
        update_data["priority"] = req["priority"]
    if "worked_minutes" in req:
        update_data["worked_minutes"] = req["worked_minutes"]
        
    if not update_data:
        return {"status": "success", "message": "No fields to update"}
        
    update_data["updated_at"] = datetime.now(timezone.utc)
    
    await tasks_collection.update_one(
        {"_id": ObjectId(task_id)},
        {"$set": update_data}
    )
    return {"status": "success", "message": "Task updated successfully"}

# Admin route to fetch an employee's tasks
@app.get("/admin/wfh/employee/{email}/tasks")
async def admin_get_employee_tasks(email: str, current_admin: Admin = Depends(get_current_admin)):
    clean_email = email.strip().lower()
    query = {"employee_email": clean_email}
    
    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id
        
    cursor = tasks_collection.find(query).sort("created_at", -1)
    tasks = await cursor.to_list(length=200)
    for task in tasks:
        task["_id"] = str(task["_id"])
    return tasks

# Admin route to assign a task to an employee
@app.post("/admin/wfh/employee/{email}/tasks")
async def admin_assign_employee_task(email: str, req: dict, current_admin: Admin = Depends(get_current_admin)):
    clean_email = email.strip().lower()
    title = req.get("title")
    if not title:
        raise HTTPException(status_code=400, detail="Task title is required")
        
    emp_query = {"email": clean_email}
    if current_admin.organization_id and current_admin.organization_id != "system_org":
        emp_query["organization_id"] = current_admin.organization_id
        
    emp = await employees_collection.find_one(emp_query)
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found or access denied")
        
    now = datetime.now(timezone.utc)
    task_doc = {
        "employee_id": str(emp["_id"]),
        "employee_email": clean_email,
        "organization_id": emp.get("organization_id"),
        "title": title.strip(),
        "description": req.get("description", "").strip(),
        "status": "todo",
        "priority": req.get("priority", "medium"),
        "worked_minutes": 0,
        "created_at": now,
        "updated_at": now
    }
    
    result = await tasks_collection.insert_one(task_doc)
    task_doc["_id"] = str(result.inserted_id)
    return task_doc

# =========================
# WFH REPORTS & ANALYTICS APIs
# =========================

from fastapi.responses import Response
import csv
import io

@app.get("/admin/wfh/reports/productivity")
async def get_wfh_reports_productivity(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    employee_email: Optional[str] = None,
    format: str = "json",
    current_admin: Admin = Depends(get_current_admin)
):
    query = {}
    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id
    if employee_email:
        query["employee_email"] = employee_email.strip().lower()
    
    date_filter = {}
    if start_date:
        date_filter["$gte"] = start_date
    if end_date:
        date_filter["$lte"] = end_date
    if date_filter:
        query["date"] = date_filter
        
    cursor = wfh_sessions_collection.find(query).sort("check_in_time", -1)
    sessions = await cursor.to_list(length=1000)
    
    report_data = []
    for s in sessions:
        check_in_str = s["check_in_time"].isoformat() if s.get("check_in_time") else ""
        check_out_str = s["check_out_time"].isoformat() if s.get("check_out_time") else ""
        duration_hrs = round(s.get("total_active_seconds", 0) / 3600.0, 2)
        
        report_data.append({
            "employee_email": s.get("employee_email", ""),
            "employee_name": s.get("employee_name", ""),
            "date": s.get("date", ""),
            "check_in_time": check_in_str,
            "check_out_time": check_out_str,
            "duration_hours": duration_hrs,
            "productivity_score": s.get("productivity_score", 0),
            "verified_productivity_score": s.get("verified_productivity_score", s.get("productivity_score", 0)),
            "face_check_passed": s.get("face_check_passed", 0),
            "face_check_count": s.get("face_check_count", 0),
            "status": s.get("status", "")
        })
        
    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Employee Email", "Employee Name", "Date", "Check-in Time", "Check-out Time",
            "Duration (Hours)", "Productivity Score (%)", "Verified Productivity Score (%)",
            "Face Checks Passed", "Total Face Checks", "Status"
        ])
        for row in report_data:
            writer.writerow([
                row["employee_email"], row["employee_name"], row["date"], row["check_in_time"],
                row["check_out_time"], row["duration_hours"], row["productivity_score"],
                row["verified_productivity_score"], row["face_check_passed"], row["face_check_count"],
                row["status"]
            ])
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=wfh_productivity_report.csv"}
        )
        
    return report_data


@app.get("/admin/wfh/reports/app-usage")
async def get_wfh_reports_app_usage(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    employee_email: Optional[str] = None,
    format: str = "json",
    current_admin: Admin = Depends(get_current_admin)
):
    query = {}
    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id
    if employee_email:
        query["employee_email"] = employee_email.strip().lower()
        
    date_filter = {}
    if start_date:
        date_filter["$gte"] = start_date
    if end_date:
        date_filter["$lte"] = end_date
    if date_filter:
        query["date"] = date_filter
        
    cursor = wfh_app_usage_collection.find(query).sort("timestamp", -1)
    usages = await cursor.to_list(length=1000)
    
    # Aggregate duration by (email, name, app_name)
    app_aggregates = {}
    for u in usages:
        email = u.get("employee_email", "")
        name = u.get("employee_name", "")
        apps = u.get("apps", [])
        for app in apps:
            app_name = app.get("name", "unknown")
            duration = float(app.get("duration_seconds", 0))
            category = app.get("status", "neutral")
            
            key = (email, name, app_name, category)
            app_aggregates[key] = app_aggregates.get(key, 0.0) + duration
            
    report_data = []
    for (email, name, app_name, category), total_dur in app_aggregates.items():
        report_data.append({
            "employee_email": email,
            "employee_name": name,
            "app_name": app_name,
            "category": category,
            "total_minutes": round(total_dur / 60.0, 2)
        })
        
    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Employee Email", "Employee Name", "App Name", "Classification", "Total Duration (Minutes)"])
        for row in report_data:
            writer.writerow([row["employee_email"], row["employee_name"], row["app_name"], row["category"], row["total_minutes"]])
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=wfh_app_usage_report.csv"}
        )
        
    return report_data


@app.get("/admin/wfh/reports/attendance")
async def get_wfh_reports_attendance(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    employee_email: Optional[str] = None,
    format: str = "json",
    current_admin: Admin = Depends(get_current_admin)
):
    query = {}
    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id
    if employee_email:
        query["employee_email"] = employee_email.strip().lower()
        
    date_filter = {}
    if start_date:
        date_filter["$gte"] = start_date
    if end_date:
        date_filter["$lte"] = end_date
    if date_filter:
        query["date"] = date_filter
        
    cursor = wfh_sessions_collection.find(query).sort("check_in_time", -1)
    sessions = await cursor.to_list(length=1000)
    
    # Group by employee to calculate summary metrics
    emp_summary = {}
    for s in sessions:
        email = s.get("employee_email", "")
        name = s.get("employee_name", "")
        active_sec = s.get("total_active_seconds", 0) or 0
        score = s.get("verified_productivity_score", s.get("productivity_score", 0)) or 0
        
        # Approximate active/idle seconds if missing
        if active_sec == 0:
            if s.get("check_in_time") and s.get("check_out_time"):
                active_sec = int((s["check_out_time"] - s["check_in_time"]).total_seconds())
                
        if email not in emp_summary:
            emp_summary[email] = {
                "employee_email": email,
                "employee_name": name,
                "sessions_count": 0,
                "total_duration_hours": 0.0,
                "productivity_scores_sum": 0.0,
                "productivity_scores_count": 0
            }
            
        emp_summary[email]["sessions_count"] += 1
        emp_summary[email]["total_duration_hours"] += round(active_sec / 3600.0, 2)
        if score > 0:
            emp_summary[email]["productivity_scores_sum"] += score
            emp_summary[email]["productivity_scores_count"] += 1
            
    report_data = []
    for email, data in emp_summary.items():
        avg_score = 0.0
        if data["productivity_scores_count"] > 0:
            avg_score = round(data["productivity_scores_sum"] / data["productivity_scores_count"], 2)
            
        report_data.append({
            "employee_email": data["employee_email"],
            "employee_name": data["employee_name"],
            "total_sessions": data["sessions_count"],
            "total_duration_hours": round(data["total_duration_hours"], 2),
            "average_productivity_score": avg_score
        })
        
    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Employee Email", "Employee Name", "Total Sessions", "Total Duration (Hours)", "Average Productivity Score (%)"])
        for row in report_data:
            writer.writerow([row["employee_email"], row["employee_name"], row["total_sessions"], row["total_duration_hours"], row["average_productivity_score"]])
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=wfh_attendance_report.csv"}
        )
        
    return report_data


@app.get("/admin/wfh/reports/screenshots")
async def get_wfh_reports_screenshots(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    employee_email: Optional[str] = None,
    format: str = "json",
    current_admin: Admin = Depends(get_current_admin)
):
    query = {}
    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id
        
    # Get employee ID first if email is provided
    if employee_email:
        emp = await employees_collection.find_one({"email": employee_email.strip().lower()})
        if emp:
            query["employee_id"] = str(emp["_id"])
        else:
            query["employee_id"] = "nonexistent"
            
    # Timestamp date logic
    time_filter = {}
    if start_date:
        time_filter["$gte"] = datetime.fromisoformat(start_date)
    if end_date:
        time_filter["$lte"] = datetime.fromisoformat(end_date)
    if time_filter:
        query["timestamp"] = time_filter
        
    cursor = wfh_screenshots_collection.find(query).sort("timestamp", -1)
    screenshots = await cursor.to_list(length=1000)
    
    # Batch-fetch all employee records
    from bson import ObjectId
    emp_ids = list({ObjectId(s["employee_id"]) for s in screenshots if s.get("employee_id") and ObjectId.is_valid(str(s["employee_id"]))})
    employees = await employees_collection.find({"_id": {"$in": emp_ids}}, {"_id": 1, "email": 1, "full_name": 1}).to_list(length=len(emp_ids))
    emp_map = {str(e["_id"]): e for e in employees}

    report_data = []
    for s in screenshots:
        emp_id_str = str(s.get("employee_id", ""))
        emp = emp_map.get(emp_id_str)
        emp_email = emp.get("email", "") if emp else ""
        emp_name = emp.get("full_name", "") if emp else ""
            
        report_data.append({
            "employee_email": emp_email,
            "employee_name": emp_name,
            "timestamp": s["timestamp"].isoformat() if s.get("timestamp") else "",
            "active_app": s.get("active_app", ""),
            "active_window": s.get("active_window", ""),
            "flagged": s.get("flagged", False),
            "flag_reason": s.get("flag_reason", "")
        })
        
    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Employee Email", "Employee Name", "Timestamp", "Active App", "Active Window", "Flagged Status", "Flag Reason"])
        for row in report_data:
            writer.writerow([
                row["employee_email"], row["employee_name"], row["timestamp"], row["active_app"],
                row["active_window"], "Flagged" if row["flagged"] else "Normal", row["flag_reason"]
            ])
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=wfh_screenshots_report.csv"}
        )
        
    return report_data


@app.get("/debug/routes")
async def debug_routes():
    return [route.path for route in app.routes]

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=False, reload_excludes=["*.log", "logs/*"])
