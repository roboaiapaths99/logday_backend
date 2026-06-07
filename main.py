from fastapi import FastAPI, HTTPException, File, Depends, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from datetime import datetime, timezone, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import os
import math
import pandas as pd
import io
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
    wfh_face_checks_collection
)
from models import (
    RegisterRequest, LoginRequest, VerifyPresenceRequest, Token, LoginResponse, EmployeeProfile, UpdateFaceRequest,
    AdminLoginRequest, EmployeeUpdate, SystemSettings, Admin, AdminRole, Organization, OrganizationRegisterRequest, SubAdminCreate,
    EmployeeType, TerritoryType, AttendanceType, CheckInMethod, PlanStatus, VisitPlan, Visit, LocationPing, ExpenseClaim,
    LeaveType, LeaveStatus, DiscussionMessage, LeaveRequest, SyncBatchRequest, ChangePasswordRequest
)
from auth import (
    get_password_hash, verify_password, create_access_token, 
    get_current_admin, get_current_employee, admin_oauth2_scheme, employee_oauth2_scheme,
    SECRET_KEY, ALGORITHM
)
import uuid
from face_utils import get_face_embedding, verify_face, compare_faces
from sheets_sync import sync_to_google_sheets, sync_visit_to_google_sheets
from fastapi import BackgroundTasks

APP_ENV = os.getenv("APP_ENV", "development")

app = FastAPI(
    title="Log Day AI Attendance API",
    version="1.0.0",
    docs_url="/docs" if APP_ENV != "production" else None,
    redoc_url="/redoc" if APP_ENV != "production" else None,
)

# Configure CORS
_raw_origins = os.getenv("ALLOWED_ORIGINS", "")
if not _raw_origins:
    # Default to development origins
    _allowed_origins = [
        "http://localhost:5173",
        "http://localhost:5174",
        "http://localhost:5180",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
        "http://127.0.0.1:5180",
        "http://192.168.1.6:5173",
        "http://192.168.1.6:5174",
        "http://localhost:3000",
        "https://attendence-inofice-admin-desk-bza5cuwtz.vercel.app",
        "https://attendence-inofice-admin-desk.vercel.app",
    ]
elif _raw_origins == "*":
    _allowed_origins = ["*"]
else:
    _allowed_origins = [o.strip() for o in _raw_origins.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins + [
        "http://localhost",
        "http://10.0.2.2", # Android emulator
    ],
    # allow_origin_regex=r"https?://.*", # Very permissive for debugging, will narrow later
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)
@app.middleware("http")
async def add_cache_control_header(request: Request, call_next):
    response = await call_next(request)
    # Prevent caching of API responses to ensure real-time data accuracy
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.get("/health")
async def health_check():
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}

# --- GEOCODING PROXY ---
@app.get("/api/geocoding/reverse")
async def reverse_geocode(lat: float, lon: float):
    """
    Proxy for Nominatim reverse geocoding to bypass CORS and 429 errors.
    Uses a server-side User-Agent to comply with Nominatim's policy.
    """
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={lat}&lon={lon}"
        headers = {
            "User-Agent": "LogDayAttendanceApp/1.0 (contact: roboaiapaths99@gmail.com)"
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Geocoding proxy error: {str(e)}")
        # Fail gracefully with coordinatess if geocoding fails
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
async def http_exception_handler(request, exc):
    """Standardize HTTP exception responses for frontend mapping."""
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "status": "error",
            "code": f"HTTP_{exc.status_code}",
            "detail": exc.detail,
            "type": "standard_error"
        },
    )

@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    """Catch-all for internal server errors to avoid leaking tracebacks in production."""
    logger.exception(f"Unhandled error: {str(exc)}")
    return JSONResponse(
        status_code=500,
        content={
            "status": "error",
            "code": "INTERNAL_SERVER_ERROR",
            "detail": "An unexpected error occurred on the server." if APP_ENV == "production" else str(exc),
            "type": "system_error"
        },
    )

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

async def upload_file_to_storage(image_bytes: bytes, filename: str, folder: str = "wfh_view") -> str:
    """
    Enterprise hybrid storage: Uploads to S3/MinIO if configured, otherwise falls back to local disk.
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
            # Ensure bucket exists
            try:
                s3_client.head_bucket(Bucket=S3_BUCKET_NAME)
            except Exception:
                if S3_REGION_NAME == "us-east-1":
                    s3_client.create_bucket(Bucket=S3_BUCKET_NAME)
                else:
                    s3_client.create_bucket(
                        Bucket=S3_BUCKET_NAME,
                        CreateBucketConfiguration={"LocationConstraint": S3_REGION_NAME}
                    )
            
            s3_key = f"{folder}/{filename}"
            s3_client.put_object(
                Bucket=S3_BUCKET_NAME,
                Key=s3_key,
                Body=image_bytes,
                ContentType="image/png" if filename.endswith(".png") else "image/jpeg"
            )
            
            if S3_ENDPOINT_URL:
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
                if image_url and image_url.startswith("/uploads/"):
                    local_path = image_url.lstrip("/")
                    if os.path.exists(local_path):
                        try:
                            os.remove(local_path)
                        except Exception as delete_err:
                            logger.error(f"Failed to delete local screenshot file: {delete_err}")
                await wfh_screenshots_collection.delete_one({"_id": screen["_id"]})
                purged_count += 1
                
        logger.info(f"Finished scheduled job: purge_old_screenshots. Purged {purged_count} screenshots.")
    except Exception as e:
        logger.error(f"Error running purge_old_screenshots: {e}")


@app.on_event("startup")
async def startup_db_client():
    """Create indexes on startup (non-fatal if DB is temporarily unreachable)."""
    try:
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
    """Toggle check‑in status and return updated info."""
    # Inlined from deleted attendance_service.py
    emp = await employees_collection.find_one({"email": employee["email"]})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    new_status = not emp.get("checked_in", False)
    update_fields = {"checked_in": new_status, "last_checkin": datetime.now(timezone.utc)}
    if new_status:
        update_fields["session_id"] = str(uuid.uuid4())
    else:
        update_fields["session_id"] = None
    await employees_collection.update_one({"email": employee["email"]}, {"$set": update_fields})
    emp.update(update_fields)
    return {
        "status": "success",
        "checked_in": emp.get("checked_in", False),
        "session_id": emp.get("session_id")
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
        if target_ssid and req.wifi_ssid and req.wifi_ssid.strip().lower() != target_ssid.strip().lower():
             raise HTTPException(status_code=403, detail=f"Must be connected to Office WiFi: {target_ssid}")
        if target_bssid and req.wifi_bssid and req.wifi_bssid.strip().lower() != target_bssid.strip().lower():
             raise HTTPException(status_code=403, detail="Must be connected to Office WiFi Access Point (BSSID mismatch).")

    # 5. Determine check-in or check-out (Localized)
    today_start_utc = get_today_start(tz_offset)
    last_log = await attendance_logs_collection.find_one(
        {"user_id": str(user["_id"]), "timestamp": {"$gte": today_start_utc}},
        sort=[("timestamp", -1)]
    )
    attendance_type = "check-out" if (last_log and last_log.get("type") == "check-in") else "check-in"

    # 6. Log Attendance
    log = {
        "user_id": str(user["_id"]),
        "email": req.email,
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

    return {
        "status": "success",
        "type": attendance_type,
        "current_status": attendance_type, # Provide immediate state for UI sync
        "message": f"{attendance_type.replace('-', ' ').title()} recorded at {log['timestamp'].strftime('%I:%M %p')}",
        "time": str(log["timestamp"]),
        "distance_from_office": f"{dist_meters:.1f}m",
    }


@app.post("/smart-attendance")
async def smart_attendance(req: VerifyPresenceRequest, background_tasks: BackgroundTasks):
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
        clean_email = req.email.strip().lower()
        user = await employees_collection.find_one({"email": clean_email})
        is_admin_user = False
        
        if not user:
             # Try 1:N face search if email is unknown/auto
            new_embedding = get_face_embedding(req.image)
            if new_embedding is not None:
                employees = await employees_collection.find({}, {"_id": 1, "face_embedding": 1, "email": 1, "employee_type": 1, "organization_id": 1}).to_list(length=5000)
                for emp in employees:
                    if emp.get("face_embedding") and compare_faces(new_embedding, emp["face_embedding"]):
                        user = emp
                        break
            
            if not user:
                # Try Admin collection (Admins might be marking attendance for themselves)
                user = await admins_collection.find_one({"email": clean_email})
                if user:
                    is_admin_user = True
                    logger.info(f"Admin '{clean_email}' found in admins collection for attendance.")
                else:
                    raise HTTPException(status_code=404, detail="Identity not recognized. Please sign in or register.")

        # Identity identified.
        is_reviewer = (clean_email == "google.review@logday.app")
        is_superadmin = (user.get("role") == "superadmin") or (clean_email == os.getenv("ADMIN_EMAIL", "admin@officeflow.ai")) or is_reviewer
        
        # Fetch settings.
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
        
        last_log = await attendance_logs_collection.find_one(
            {"user_id": str(user["_id"])},
            sort=[("timestamp", -1)]
        )

        # AUTO-CLOSE STALE SESSIONS
        if last_log and last_log.get("type") == "check-in":
            last_ts = last_log.get("timestamp")
            if last_ts and last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            if last_ts and last_ts < today_start_utc:
                auto_checkout_time = last_ts.replace(hour=23, minute=59, second=59) if last_ts.hour < 23 else last_ts + timedelta(seconds=1)
                auto_checkout_log = {
                    "user_id": str(user["_id"]),
                    "type": "check-out",
                    "timestamp": auto_checkout_time,
                    "lat": last_log.get("lat", 0),
                    "long": last_log.get("long", 0),
                    "location_name": "Auto-closed (missed checkout)",
                    "wifi_bssid": "",
                    "wifi_ssid": "",
                    "method": "system_auto_close",
                    "organization_id": str(org_id) if org_id else None,
                }
                await attendance_logs_collection.insert_one(auto_checkout_log)
                last_log = auto_checkout_log

        # State Machine Validation
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
            raise HTTPException(status_code=403, detail="Security violation: Mock location detected. Attendance rejected.")

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

        # GEOFENCE BYPASS for Superadmin
        if is_superadmin:
            logger.info("Superadmin bypass: Geofence and Territory checks skipped.")
            wifi_pct = 100
            check_in_method = CheckInMethod.WIFI_GEOFENCE
        else:
            if role == "desk" or role == EmployeeType.DESK:
                if not is_at_office:
                     raise HTTPException(
                         status_code=403, 
                         detail=f"Location error. You are {office_dist:.1f}m away from office (Limit: {radius}m)."
                     )
                
                is_wifi_ok = True
                if office_wifi_ssid and (not req.wifi_ssid or req.wifi_ssid.strip().lower() != office_wifi_ssid.strip().lower()):
                    is_wifi_ok = False
                if office_wifi_bssid and (not req.wifi_bssid or req.wifi_bssid.strip().lower() != office_wifi_bssid.strip().lower()):
                    is_wifi_ok = False
                
                wifi_pct = 100 if is_wifi_ok else 0
                check_in_method = CheckInMethod.WIFI_GEOFENCE
            elif role == "wfh" or role == EmployeeType.WFH:
                # WFH Logic: Allow check-in from anywhere, bypass geofence/territory checks.
                logger.info(f"WFH Employee {user['email']} check-in/out: bypassing geofence and territory checks.")
                wifi_pct = 100
                check_in_method = CheckInMethod.GPS_TERRITORY
                
                # Check WFH device status
                if req.device_id and not is_superadmin:
                    try:
                        await verify_wfh_device(str(user["_id"]), str(org_id), req.device_id)
                    except HTTPException as e:
                        raise e
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



        # 4. Log Attendance (Consolidated)
        attendance_type = req.intended_type or "check-in" # Default or provided
        
        is_early_leave = False
        is_late = False
        late_mins = 0
        
        if attendance_type == "check-in":
            # Use org_settings which was fetched at line 833
            settings = org_settings or SystemSettings().dict()
            
            if role == "field":
                start_time_str = settings.get("field_office_start_time", "10:00")
                threshold_mins = settings.get("field_late_threshold_mins", 30)
                tz_offset = settings.get("timezone_offset", 330)
            else:
                # Desk: Dynamic from Database with defaults
                start_time_str = settings.get("office_start_time", "10:00")
                threshold_mins = settings.get("late_threshold_mins", 15)
                tz_offset = settings.get("timezone_offset", 330)
            
            try:
                log_time_utc = datetime.now(timezone.utc)
                current_time_local = log_time_utc + timedelta(minutes=tz_offset)
                
                # Limit time for today
                start_h, start_m = map(int, start_time_str.split(":"))
                limit_time = current_time_local.replace(hour=start_h, minute=start_m, second=0, microsecond=0) + timedelta(minutes=threshold_mins)
                
                if current_time_local > limit_time:
                    is_late = True
                    diff = current_time_local - current_time_local.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
                    late_mins = int(diff.total_seconds() / 60)
            except Exception as e:
                logger.error(f"Error calculating lateness: {e}")
                
        elif attendance_type == "check-out":
            # Check for early leave
            settings = org_settings or SystemSettings().dict()
            if role == "field":
                end_time_str = settings.get("field_office_end_time", "18:00")
                tz_offset = settings.get("timezone_offset", 330)
            else:
                # Desk: Dynamic from Database with defaults
                end_time_str = settings.get("office_end_time", "18:00")
                tz_offset = settings.get("timezone_offset", 330)
                
            try:
                log_time_utc = datetime.now(timezone.utc)
                current_time_local = log_time_utc + timedelta(minutes=tz_offset)
                end_h, end_m = map(int, end_time_str.split(":"))
                
                # Assume grace of 10 minutes (e.g. 17:50)
                early_limit_time = current_time_local.replace(hour=end_h, minute=end_m, second=0, microsecond=0) - timedelta(minutes=10)
                
                if current_time_local < early_limit_time:
                    is_early_leave = True
            except Exception as e:
                logger.error(f"Error calculating early checkout: {e}")

        # Determine unified attendance type
        att_type_val = AttendanceType.REMOTE_FIELD
        if role == EmployeeType.DESK or role == "desk":
            att_type_val = AttendanceType.OFFICE
        elif role == EmployeeType.WFH or role == "wfh":
            att_type_val = AttendanceType.WFH

        # Location name default
        loc_name_val = req.address or ("Office Zone" if role == EmployeeType.DESK else "Field Location")
        if role == EmployeeType.WFH or role == "wfh":
            loc_name_val = req.address or "Remote Workspace"

        wfh_session_id = None
        if role == EmployeeType.WFH or role == "wfh":
            if attendance_type == "check-in":
                # Ensure a WFH session is started
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
                # Complete the WFH session
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
            "timestamp": datetime.now(timezone.utc),
            "type": attendance_type,
            "attendance_type": att_type_val,
            "location": {"lat": req.lat, "long": req.long},
            "check_in_method": check_in_method,
            "is_late": is_late,
            "is_early_leave": is_early_leave,
            "late_mins": max(0, late_mins),
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
            "message": f"Attendance {attendance_type.replace('-', ' ').title()} Successful. Verified via {check_in_method.value.replace('_', ' ').title()}.",
            "time": log["timestamp"].isoformat(),
            "is_late": is_late,
            "is_early_leave": is_early_leave,
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
    if current_user["email"] != email:
        raise HTTPException(status_code=403, detail="Forbidden: You can only access your own logs.")

    user = await employees_collection.find_one({"email": email})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    query = {"user_id": str(user["_id"])}
    
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
    last_log = await attendance_logs_collection.find_one(
        {
            "user_id": str(user["_id"]),
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
            {"user_id": user_id_str},
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
            org_settings = await settings_collection.find_one({"organization_id": str(org_id) if ObjectId.is_valid(str(org_id)) else org_id})
            if org_settings:
                tz_offset = org_settings.get("timezone_offset", 330)

        today_start = get_today_start(tz_offset)
        week_start = today_start - timedelta(days=today_start.weekday())
        month_start = today_start.replace(day=1)

        # Get logs for calculation
        all_logs = await attendance_logs_collection.find({
            "user_id": str(user["_id"]),
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
            "user_id": str(user["_id"]),
            "timestamp": {"$gte": month_start}
        }).to_list(length=2000)
        month_hours = await calculate_hours(month_logs)

        # On-time count
        on_time_count = 0
        for log in aware_logs:
            if "in" in log.get("type", "").lower():
                user_type = user.get("employee_type", "desk")
                start_time_str = "10:00"
                threshold_mins = 15
                if user_type == "field" and org_id:
                    org_doc = await settings_collection.find_one({"organization_id": str(org_id) if ObjectId.is_valid(str(org_id)) else org_id})
                    if org_doc:
                        start_time_str = org_doc.get("field_office_start_time", "10:00")
                        threshold_mins = org_doc.get("field_late_threshold_mins", 30)
                
                try:
                    start_h, start_m = map(int, start_time_str.split(":"))
                    local_log_time = log["aware_ts"] + timedelta(minutes=tz_offset)
                    limit_time = local_log_time.replace(hour=start_h, minute=start_m, second=0, microsecond=0) + timedelta(minutes=threshold_mins)
                    if local_log_time <= limit_time:
                        on_time_count += 1
                except: pass

        # Latest history
        history_cursor = attendance_logs_collection.find(
            {"user_id": str(user["_id"])}
        ).sort("timestamp", -1).limit(5)
        history = await history_cursor.to_list(length=5)
        for h in history:
            h["_id"] = str(h["_id"])
            if "timestamp" in h and isinstance(h["timestamp"], datetime):
                h["timestamp"] = h["timestamp"].isoformat()

        # Absolute Status Sync
        latest_log = await attendance_logs_collection.find_one(
            {"user_id": str(user["_id"])},
            sort=[("timestamp", -1)]
        )
        
        current_status = "check-out"
        if latest_log:
            log_type = latest_log.get("type", "check-out")
            if log_type == "check-in":
                l_ts = latest_log["timestamp"]
                if l_ts.tzinfo is None: l_ts = l_ts.replace(tzinfo=timezone.utc)
                if l_ts < today_start:
                    current_status = "check-out" # Stale
                    logger.info(f"[Analytics] Stale session auto-closed for {user['email']}")
                else:
                    current_status = "check-in"
            else:
                current_status = log_type
        
        return {
            "email": user["email"],
            "today_hours": today_hours,
            "week_total": week_hours,
            "month_total": month_hours,
            "on_time_count": on_time_count,
            "current_status": current_status,
            "history": history,
            "office_wifi_ssid": os.getenv("OFFICE_WIFI_SSID", "")
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
    
    query = {"organization_id": current_admin.organization_id}
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
        "organization_id": current_admin.organization_id,
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
        
    # Security: Ensure we only delete admins from the same organization and who are NOT owners
    result = await admins_collection.delete_one({
        "email": email,
        "organization_id": current_admin.organization_id,
        "role": "admin" # Can only delete sub-admins, not owners
    })
    
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Sub-admin not found, or you don't have permission to delete this account.")
        
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
    
    result = await admins_collection.update_one(
        {"email": email, "organization_id": current_admin.organization_id},
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


@app.post("/admin/employees/bulk-update-type")
async def admin_bulk_update_employee_type(req: dict, current_admin: Admin = Depends(get_current_admin)):
    """Bulk update employee work type (desk/field/office)"""
    emails = [e.lower().strip() for e in req.get("employee_emails", [])]
    new_type = req.get("employee_type")
    
    if not emails or not new_type:
        raise HTTPException(status_code=400, detail="employee_emails and employee_type are required")
        
    result = await employees_collection.update_many(
        {"email": {"$in": emails}, "organization_id": current_admin.organization_id},
        {"$set": {"employee_type": new_type}}
    )
    
    return {"message": f"Successfully updated {result.modified_count} employees to {new_type}"}


@app.delete("/admin/employees/{email}")
async def admin_delete_employee(email: str, current_admin: Admin = Depends(get_current_admin)):
    """Remove an employee and their logs."""
    query = {"email": email}
    if current_admin.organization_id:
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
        org_employees = await employees_collection.find({"organization_id": current_admin.organization_id}, {"_id": 1}).to_list(None)
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
        "organization_id": current_admin.organization_id, # Bind to admin's org
        "force_password_change": True,  # Employee must set their own password on first desk login
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
def get_employee_filter(current_admin: Admin):
    """Returns a MongoDB filter based on admin role for multi-tenant segmenting."""
    org_id = current_admin.organization_id
    if not org_id:
        # Fallback for superadmin without org
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
        # Validate input
        if not q or q.strip() == "" or len(q.strip()) < 2:
            logger.info(f"[SEARCH] Query too short or empty: '{q}' (length: {len(q.strip()) if q else 0})")
            return {"results": [], "total": 0, "query": q}
        
        search_query = q.strip()
        logger.info(f"[SEARCH] Query received: '{search_query}' - Searching by name and slug")
        
        # Case-insensitive partial match on both name and slug
        regex_pattern = {"$regex": search_query, "$options": "i"}
        mongo_query = {
            "$or": [
                {"name": regex_pattern},
                {"slug": regex_pattern}
            ]
        }
        
        # Execute search
        cursor = organizations_collection.find(mongo_query).limit(10)
        orgs = await cursor.to_list(length=10)
        
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
             raise HTTPException(status_code=403, detail="Security Violation: Mock Location detected. Check-in rejected.")

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
        for att in req.attendance_logs:
            offline_id = att.get("offline_id")
            if not offline_id:
                continue
            exists = await attendance_logs_collection.find_one({"offline_id": offline_id})
            if not exists:
                att["synced_at"] = now
                # Hard link to current user to prevent tampering
                att["user_id"] = str(employee["_id"]) 
                att["organization_id"] = employee["organization_id"]
                # Convert string timestamp to datetime if needed
                if "timestamp" in att and isinstance(att["timestamp"], str):
                    try:
                        att["timestamp"] = datetime.fromisoformat(att["timestamp"].replace("Z", "+00:00"))
                    except:
                        pass
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
    for c in claims:
        c["_id"] = str(c["_id"])
        if isinstance(c.get("created_at"), datetime):
            c["created_at"] = c["created_at"].isoformat()
        # Enrich with employee name
        emp = await employees_collection.find_one({"email": c["employee_id"]})
        c["full_name"] = emp["full_name"] if emp else "Unknown"
        
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
    
    # 3. Current Attendance Status (TODAY ONLY)
    # Only check today's logs to determine status. If the last log is from
    # a previous day, treat it as a fresh day (check-out / "Start of Day").
    today_log = await attendance_logs_collection.find_one(
        {"email": employee_id, "timestamp": {"$gte": today_start}},
        sort=[("timestamp", -1)]
    )
    if today_log:
        current_status = today_log.get("type", "check-out")
    else:
        # No log today → fresh day → user needs to check in
        current_status = "check-out"
            
    return {
        "date": query_date,
        "total_visits": visits,
        "total_km": round(total_km, 2),
        "attendance_status": current_status,
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
    enriched_plans = []
    for plan in plans:
        emp = await employees_collection.find_one({"employee_id": plan["employee_id"]})
        plan["_id"] = str(plan["_id"])
        plan["full_name"] = emp["full_name"] if emp else "Unknown"
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
    
    # Get relevant employee IDs
    org_employees = await employees_collection.find(filter_query, {"_id": 1}).to_list(None)
    org_emp_ids = [str(emp["_id"]) for emp in org_employees]
    
    logs_query = {
        "timestamp": {"$gte": today_start},
        "type": "check-in",
        "user_id": {"$in": org_emp_ids}
    }
         
    clocked_in_today = await attendance_logs_collection.count_documents(logs_query)
    
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

    # Calculate Absent count (Total - ClockedIn - OnLeave)
    # This is a simplified dashboard metric
    absent_count = max(0, total_employees - clocked_in_today - on_leave_count)

    return {
        "total_employees": total_employees,
        "clocked_in_today": clocked_in_today,
        "late_arrivals_today": total_alerts_today, 
        "on_leave": on_leave_count,
        "absent_today": absent_count,
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
    """Submit a new Leave or On-Duty (OD) request with optional proof (base64 image)."""
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
                # We'll continue without the proof if it fails, or we could raise an error
        
        new_request = {
            "employee_id": employee["email"],
            "organization_id": employee.get("organization_id", "system_org"),
            "leave_type": req["leave_type"], # sick, casual, on_duty, other
            "start_date": req["start_date"],
            "end_date": req["end_date"],
            "reason": req["reason"],
            "status": "pending",
            "proof_url": proof_file_url or req.get("proof_url"),
            "discussion": [],
            "created_at": datetime.now(timezone.utc)
        }
        result = await leave_requests_collection.insert_one(new_request)
        return {"status": "success", "request_id": str(result.inserted_id), "proof_url": proof_file_url}
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
    """Fetch pending leave requests from my team members."""
    subordinates = await employees_collection.find({"manager_id": manager["email"]}).to_list(length=100)
    sub_emails = [s["email"] for s in subordinates]
    
    cursor = leave_requests_collection.find({
        "employee_id": {"$in": sub_emails},
        "status": "pending"
    }).sort("created_at", -1)
    
    requests = await cursor.to_list(length=100)
    results = []
    for req in requests:
        emp = next((s for s in subordinates if s["email"] == req["employee_id"]), None)
        emp_name = emp["full_name"] if emp else req["employee_id"]
        
        results.append({
            "id": str(req["_id"]),
            "full_name": emp_name,
            "employee_id": req["employee_id"],
            "reason": req["reason"],
            "start_date": req["start_date"],
            "end_date": req["end_date"],
            "status": req["status"],
            "proof_url": req.get("proof_url")
        })
    return results

@app.post("/api/leave/request/{request_id}/approve")
async def manager_approve_leave(request_id: str, payload: dict, manager=Depends(get_current_employee)):
    """Allow a manager to approve/reject leave for their team."""
    status = payload.get("status")
    if status not in ["approved", "rejected"]:
        raise HTTPException(status_code=400, detail="Invalid status. Use 'approved' or 'rejected'.")
        
    leave_req = await leave_requests_collection.find_one({"_id": ObjectId(request_id)})
    if not leave_req:
        raise HTTPException(status_code=404, detail="Leave request not found")
        
    emp = await employees_collection.find_one({"email": leave_req["employee_id"], "manager_id": manager["email"]})
    if not emp:
         raise HTTPException(status_code=403, detail="Forbidden: You can only manage leaves for your direct reports.")
         
    await leave_requests_collection.update_one(
        {"_id": ObjectId(request_id)},
        {"$set": {
            "status": status,
            "processed_at": datetime.now(timezone.utc),
            "processed_by": manager["email"]
        }}
    )
    return {"status": "success", "new_status": status}

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
    enriched = []
    for r in requests:
        r["_id"] = str(r["_id"])
        emp_id = r.get("employee_id")
        if emp_id:
            emp = await employees_collection.find_one({"email": emp_id})
            r["full_name"] = emp.get("full_name", emp.get("name", "Unknown")) if emp else "Unknown"
        else:
            r["full_name"] = "Unknown"
            
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
async def handle_leave_request(request_id: str, action: str, admin: dict = Depends(get_current_admin)):
    """Handle (Approve/Reject/Cancel) a leave request (Role Scoped)."""
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
    result = await leave_requests_collection.update_one(
        {"_id": obj_id},
        {"$set": {
            "status": status_map[action],
            "processed_at": datetime.now(timezone.utc),
            "processed_by": admin.email
        }}
    )
        
    return {"status": "success"}



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
    for c in claims:
        c["_id"] = str(c["_id"])
        if isinstance(c.get("created_at"), datetime):
            c["created_at"] = c["created_at"].isoformat()
        if isinstance(c.get("resolved_at"), datetime):
            c["resolved_at"] = c["resolved_at"].isoformat()
        # Enrich with employee name
        emp = await employees_collection.find_one({"email": c.get("employee_id")})
        c["employee_name"] = emp["full_name"] if emp else c.get("employee_id", "Unknown")
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
    
    for a in alerts:
        a["_id"] = str(a["_id"])
        # Enrich with employee name
        emp = await employees_collection.find_one({"email": a["employee_id"]})
        a["employee_name"] = emp["full_name"] if emp else a["employee_id"]
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
    
    summary = {"total_records": len(logs), "check_ins": 0, "check_outs": 0, "unique_employees": set()}
    enriched = []
    
    for log in logs:
        emp = await employees_collection.find_one({"_id": ObjectId(log["user_id"])})
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
        summary["unique_employees"].add(log.get("email", ""))
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

    # 3. Fetch Approved Leaves for the month
    # Fix: Correct overlap logic and case-insensitivity for status
    month_start_str = month + "-01"
    month_end_str = next_month.strftime("%Y-%m-%d")
    
    leaves = await leave_requests_collection.find({
        "employee_id": email,
        "status": {"$regex": "^approved$", "$options": "i"},
        "start_date": {"$lt": month_end_str},
        "end_date": {"$gte": month_start_str}
    }).to_list(length=50)

    # 4. Process Data Day by Day
    daily_breakdown = []
    total_working_ms = 0
    present_days = 0
    absent_days = 0
    leaves_count = 0
    
    # We must loop through local days, so start with the local month boundary
    # Assuming IST (330 offset) if org missing settings -> fallback 330
    tz_offset = 330 # Should fetch from settings ideally
    
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
            "first_in": None,
            "last_out": None,
            "duration_hours": 0,
            "logs": []
        }

        # Check for Weekend (Saturday=5, Sunday=6)
        if current_day_local.weekday() >= 5:
            day_info["status"] = "Weekend"

        # Check for Leave
        is_on_leave = False
        for leave in leaves:
            if leave["start_date"] <= day_str <= leave["end_date"]:
                day_info["status"] = f"Leave ({leave.get('leave_type', 'General')})"
                is_on_leave = True
                leaves_count += 1
                break

        if day_logs:
            day_logs.sort(key=lambda x: x["timestamp"])
            present_days += 1
            
            day_info["first_in"] = day_logs[0]["timestamp"].replace(tzinfo=timezone.utc).isoformat()
            
            # Calculate duration: simplify by taking last check-out - first check-in
            # Better: sum durations between paired IN and OUT
            day_duration_ms = 0
            last_in_time = None
            
            for log in day_logs:
                # Dynamically calculate is_late to override buggy legacy historical DB states
                is_late = log.get("is_late", False)
                if log["type"] == "check-in":
                    # strictly check if time is past 10:15 local time
                    local_time = log["timestamp"].replace(tzinfo=timezone.utc) + timedelta(minutes=tz_offset)
                    if local_time.hour > 10 or (local_time.hour == 10 and local_time.minute > 15):
                        is_late = True
                    else:
                        is_late = False

                # Basic representation for frontend
                day_info["logs"].append({
                    "time": log["timestamp"].replace(tzinfo=timezone.utc).isoformat(),
                    "type": log["type"],
                    "status": "Late" if is_late else "Early Leave" if log.get("is_early_leave") else "Present",
                    "method": log.get("check_in_method", "N/A"),
                    "location": log.get("location"),
                    "selfie": log.get("selfie_url"),
                    "wifi_confidence": log.get("wifi_confidence", 0)
                })

                if log["type"] == "check-in":
                    last_in_time = log["timestamp"]
                    if last_in_time.tzinfo is None:
                        last_in_time = last_in_time.replace(tzinfo=timezone.utc)
                elif log["type"] == "check-out" and last_in_time:
                    # Ensure log timestamp is aware
                    log_ts = log["timestamp"]
                    if log_ts.tzinfo is None:
                        log_ts = log_ts.replace(tzinfo=timezone.utc)
                    
                    delta = log_ts - last_in_time
                    day_duration_ms += delta.total_seconds() * 1000
                    last_in_time = None
                    day_info["last_out"] = log_ts.isoformat()

            day_info["duration_hours"] = round(day_duration_ms / (1000 * 3600), 2)
            total_working_ms += day_duration_ms

            # Set status to Early Leave if the last check-out on this day was flagged
            if any(l.get("type") == "check-out" and l.get("is_early_leave") for l in day_logs):
                 day_info["status"] = "Early Leave"
            else:
                 # Check if the very first check-in log is dynamically evaluated as late
                 first_checkin = [l for l in day_logs if l["type"] == "check-in"]
                 if first_checkin:
                     loc_time = first_checkin[0]["timestamp"].replace(tzinfo=timezone.utc) + timedelta(minutes=tz_offset)
                     if loc_time.hour > 10 or (loc_time.hour == 10 and loc_time.minute > 15):
                         day_info["status"] = "Late"
                     else:
                         day_info["status"] = "Present"
                 else:
                     day_info["status"] = "Present"
        else:
            # Not present and not weekend and not leave -> Absent
            if day_info["status"] == "Absent":
                absent_days += 1

        daily_breakdown.append(day_info)
        current_day_local = next_day_local

    # 5. Summary Metrics
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
            "leaves_taken": len(leaves),
            "absent_days": absent_days
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

    device_data["status"] = "pending"
    device_data["registered_at"] = now
    device_data["approved_by"] = None
    device_data["approved_at"] = None
    device_data["revoked_by"] = None
    device_data["revoked_at"] = None
    device_data["revoke_reason"] = None

    result = await wfh_device_info_collection.insert_one(device_data)

    return {
        "status": "success",
        "message": "WFH device registered. Waiting for admin approval.",
        "device_status": "pending",
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

    device = await wfh_device_info_collection.find_one({
        "employee_id": employee_id,
        "organization_id": org_id,
        "device_id": device_id,
        "status": "approved"
    })

    if not device:
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
    today = now.strftime("%Y-%m-%d")

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

    await verify_wfh_device(employee_id, org_id, device_id)

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
        if session.get("date") != today_str:
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
    try:
        import os
        import re
        from PIL import Image
        
        # Try to resolve web URL or static mount path to local path
        if "uploads/" in image_path:
            idx = image_path.find("uploads/")
            image_path = image_path[idx:]
            
        # Try resolving relative path if it's not absolute
        resolved_path = image_path
        if not os.path.isabs(image_path) and not os.path.exists(image_path):
            alternative_path = os.path.join("..", "wfh_desktop", "agent", image_path)
            if os.path.exists(alternative_path):
                resolved_path = alternative_path
        
        if not os.path.exists(resolved_path):
            logger.warning(f"Screenshot file not found for OCR: {resolved_path}")
            return
            
        try:
            import pytesseract
            img = Image.open(resolved_path)
            text = pytesseract.image_to_string(img)
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
        image_url = item.get("image_url", "")
        if image_url and image_url.startswith("/uploads/"):
            item["image_url"] = build_static_upload_url(request, image_url)
        thumbnail_url = item.get("thumbnail_url", "")
        if thumbnail_url and thumbnail_url.startswith("/uploads/"):
            item["thumbnail_url"] = build_static_upload_url(request, thumbnail_url)
        
    return {"screenshots": screenshots}

def build_static_upload_url(request: Request, relative_path: str) -> str:
    base_url = str(request.base_url).rstrip("/")
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

    active_sessions = await wfh_sessions_collection.count_documents({
        **query,
        "status": "active"
    })

    today_alerts = await wfh_alerts_collection.count_documents({
        **query,
        "status": "pending",
        "timestamp": {
            "$gte": datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        }
    })

    productivity_cursor = wfh_productivity_collection.find({
        **query,
        "date": today
    })

    productivity_list = await productivity_cursor.to_list(length=1000)

    avg_productivity = 0
    if productivity_list:
        avg_productivity = round(
            sum(item.get("score", 0) for item in productivity_list) / len(productivity_list),
            2
        )

    wfh_employees = await employees_collection.count_documents({
        **query,
        "employee_type": "wfh"
    })

    return {
        "active_sessions": active_sessions,
        "wfh_employees": wfh_employees,
        "pending_alerts": today_alerts,
        "avg_productivity": avg_productivity,
        "date": today
    }


@app.get("/admin/wfh/live-view")
async def admin_wfh_live_view(current_admin: Admin = Depends(get_current_admin)):
    org_id = current_admin.organization_id

    query = {"employee_type": "wfh"}
    if org_id and org_id != "system_org":
        query["organization_id"] = org_id

    employees = await employees_collection.find(query).to_list(length=500)

    result = []

    for employee in employees:
        employee_id = str(employee["_id"])
        employee_email = employee.get("email")

        # Find active session if any
        active_session = await wfh_sessions_collection.find_one({
            "employee_id": employee_id,
            "status": "active"
        })

        latest_screenshot = await wfh_screenshots_collection.find_one(
            {"employee_id": employee_id},
            sort=[("timestamp", -1)]
        )

        latest_activity = await wfh_activity_collection.find_one(
            {"employee_id": employee_id},
            sort=[("timestamp", -1)]
        )

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
                "image_url": latest_screenshot.get("image_url"),
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
        image_url = item.get("image_url", "")
        if image_url and image_url.startswith("/uploads/"):
            item["image_url"] = build_static_upload_url(request, image_url)
        thumbnail_url = item.get("thumbnail_url", "")
        if thumbnail_url and thumbnail_url.startswith("/uploads/"):
            item["thumbnail_url"] = build_static_upload_url(request, thumbnail_url)

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
        if image_url.startswith("http://") or image_url.startswith("https://"):
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

    timeline = []

    screenshots = await wfh_screenshots_collection.find({**base_query, **time_filter}).to_list(length=300)
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

    activities = await wfh_activity_collection.find({**base_query, **time_filter}).to_list(length=300)
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

    alerts = await wfh_alerts_collection.find({**base_query, **time_filter}).to_list(length=300)
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

    timeline.sort(key=lambda x: x.get("timestamp") or datetime.min, reverse=True)

    return timeline


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
    
    report_data = []
    for s in screenshots:
        emp_email = ""
        emp_name = ""
        try:
            emp = await employees_collection.find_one({"_id": ObjectId(s["employee_id"])})
            if emp:
                emp_email = emp.get("email", "")
                emp_name = emp.get("full_name", "")
        except Exception:
            pass
            
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
