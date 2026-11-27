from upload_security import SecureMediaRoute
from permissions import require_feature, require_any_feature, require_admin_session
from auth import issue_access_token, resolve_access_token, ensure_active_account
from invitations import validate_invitation, consume_invitation
from fastapi import APIRouter, FastAPI, HTTPException, File, Depends, BackgroundTasks, Request, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse, Response
from starlette import status
import asyncio
from datetime import datetime, timezone, timedelta
import os
import calendar
import csv
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
try:
    import pandas as pd
except ImportError:
    pd = None
from bson import ObjectId
try:
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
except ImportError:
    letter = landscape = SimpleDocTemplate = Table = TableStyle = Paragraph = Spacer = colors = getSampleStyleSheet = None
from dotenv import load_dotenv
from typing import Optional, List, Dict, Any
import logging
import base64
import uuid
import requests
import httpx
try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None
try:
    import boto3
    from botocore.exceptions import NoCredentialsError
except ImportError:
    boto3 = None
    NoCredentialsError = Exception

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
    tasks_collection
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
    get_password_hash_async, verify_password_async,
    check_login_rate_limit, record_failed_login, clear_failed_logins,
    get_current_admin, get_current_employee, admin_oauth2_scheme, employee_oauth2_scheme,
    SECRET_KEY, ALGORITHM
)
from face_utils import get_face_embedding, verify_face, compare_faces, is_dummy_embedding
from sheets_sync import sync_to_google_sheets, sync_visit_to_google_sheets
from attendance_service import classify_attendance_day

logger = logging.getLogger("main")
APP_ENV = os.getenv("APP_ENV", "development")
from services.common_helpers import (
    BASE_DIR, LOG_DIR, GEOCODE_CACHE, MAX_GEOCODE_CACHE_SIZE,
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
from services.scheduler_jobs import check_missed_visits, purge_old_screenshots

router = APIRouter(route_class=SecureMediaRoute)

@router.post("/register", response_model=LoginResponse)
async def register(req: RegisterRequest):
    """Register a new employee with face image and enterprise metadata."""
    try:
        clean_email = req.email.strip().lower()
        logger.info(f"Received registration request for: {clean_email}")

        invitation = await validate_invitation(req.invitation_token, clean_email)

        # Check if employee already exists
        existing = await employees_collection.find_one({"email": clean_email})
        if existing:
            raise HTTPException(status_code=400, detail="Email already registered")

        # Generate face embedding
        if not req.face_image:
            raise HTTPException(status_code=400, detail="face_image is required for registration")
        
        from services.inference import run_inference
        from upload_security import save_local_image
        embedding = await run_inference(get_face_embedding, req.face_image)
        if embedding is None:
            raise HTTPException(status_code=400, detail="No face detected in image. Please try again with a clear photo.")

        # Create employee record
        hashed_password = await get_password_hash_async(req.password)
        invitation = await consume_invitation(req.invitation_token, clean_email)
        emp_oid = ObjectId()
        try:
            profile_image_url = await save_local_image(
                req.face_image,
                {"_id": emp_oid, "email": clean_email, "organization_id": invitation["organization_id"]},
                "onboarding",
                "profiles",
            )
        except HTTPException:
            profile_image_url = None
        employee_dict = {
            "_id": emp_oid,
            "full_name": req.full_name,
            "email": clean_email,
            "employee_id": invitation["employee_id"],
            "designation": invitation["designation"],
            "department": invitation["department"],
            "organization_id": invitation["organization_id"],
            "employee_type": invitation["employee_type"],
            "hashed_password": hashed_password,
            "face_embedding": embedding,
            "profile_image": profile_image_url,
            "device_id": req.device_id,
            "created_at": datetime.now(timezone.utc),
            "status": "Active"
        }

        # For Field employees, initialize default territory if not provided
        if invitation["employee_type"] == EmployeeType.FIELD:
            employee_dict.update({
                "territory_type": TerritoryType.RADIUS,
                "territory_radius_meters": 500, # Default 500m
                "gps_otp_fallback_enabled": True
            })

        await employees_collection.insert_one(employee_dict)
        logger.info(f"User {req.email} saved to database successfully as {req.employee_type}.")

        # Generate token
        access_token = await issue_access_token(employee_dict, "employee")
        
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


@router.get("/organization/discover/{slug}")
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
        "settings": sanitize_public_settings(settings)
    }


@router.get("/settings/{slug}")
async def get_public_settings_by_slug(slug: str):
    """Public endpoint for apps to fetch organization settings by slug."""
    # Handle "null" or "undefined" string from frontend JS
    if not slug or slug in ["null", "undefined", "generic"]:
        return SystemSettings().model_dump()
        
    org = await organizations_collection.find_one({"slug": slug})
    if not org:
        # Fallback to default instead of 404 to prevent app crashes
        return SystemSettings().model_dump()
    
    settings_doc = await settings_collection.find_one({"organization_id": str(org["_id"])})
    if settings_doc:
        return sanitize_public_settings(settings_doc)
    return SystemSettings().model_dump()


@router.post("/login", response_model=LoginResponse)
async def login(req: LoginRequest, request: Request):
    """Login with email and password. When organization_id is provided, employee must belong to that org (multi-tenant security)."""
    import hmac
    clean_email = req.email.strip().lower()
    check_login_rate_limit(request, clean_email)
    logger.info(f"--- Login attempt for '{clean_email}' (org={req.organization_id}) ---")
    
    user = await employees_collection.find_one({"email": clean_email})
    is_admin_login = False
    if not user or not await verify_password_async(req.password, user.get("hashed_password", "")):
        record_failed_login(request, clean_email)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    ensure_active_account(user)
    is_valid = True
    if not is_valid:
        record_failed_login(request, clean_email)
        logger.warning(f"Login failed: Invalid password for '{clean_email}'.")
        raise HTTPException(status_code=401, detail="Invalid password.")
    
    clear_failed_logins(request, clean_email)
    logger.info(f"Authentication successful for '{clean_email}' (as {'admin' if is_admin_login else 'employee'}).")

    # Employee login never grants admin privileges or changes organization membership.
    is_reviewer = False
    is_superadmin = False
    if req.organization_id and user.get("organization_id") != req.organization_id:
        raise HTTPException(403, "Access denied. Your account belongs to another organization.")

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
    access_token = await issue_access_token(user, "employee")
    
    # Check if this user is a manager (has subordinates)
    subordinates_count = await employees_collection.count_documents({"manager_id": clean_email})
    is_manager = subordinates_count > 0
    
    # Check if user needs face enrollment (no face_embedding or dummy/mock embedding)
    needs_enrollment = (user.get("face_embedding") is None) or is_dummy_embedding(user.get("face_embedding"))
    
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


@router.post("/auth/check")
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


@router.get("/me", response_model=EmployeeProfile)
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


@router.post("/api/me/change-password")
async def change_password(req: ChangePasswordRequest, employee=Depends(get_current_employee)):
    """Change employee password and clear force_password_change flag."""
    user = await employees_collection.find_one({"email": employee["email"]})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    is_valid = await verify_password_async(req.old_password, user.get("hashed_password", ""))
    if not is_valid:
        raise HTTPException(status_code=400, detail="Incorrect old password")
        
    new_hashed_password = await get_password_hash_async(req.new_password)
    await employees_collection.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "hashed_password": new_hashed_password,
            "force_password_change": False
        }, "$inc": {"auth_version": 1}}
    )
    from database import auth_sessions_collection
    await auth_sessions_collection.update_many(
        {"account_id": str(user["_id"]), "account_type": "employee"},
        {"$set": {"revoked": True}}
    )
    
    # Check if face enrollment is still needed
    needs_enrollment = (user.get("face_embedding") is None) or is_dummy_embedding(user.get("face_embedding"))
    
    return {
        "status": "success", 
        "message": "Password updated successfully",
        "needs_face_enrollment": needs_enrollment
    }


@router.get("/api/employee/profile")
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
        "has_face_data": bool(user.get("face_embedding")) and not is_dummy_embedding(user.get("face_embedding")),
        "needs_face_enrollment": (user.get("face_embedding") is None) or is_dummy_embedding(user.get("face_embedding")),
        "organization_id": str(user.get("organization_id", "")),
        "is_manager": user.get("is_manager", False),
        "status": user.get("status", "Active"),
        "current_status": current_status
    }


@router.post("/api/employee/update-face")
async def update_employee_face(req: dict, current_user: dict = Depends(get_current_employee)):
    """Enroll or Update Face Biometrics."""
    # Support both 'image' and 'face_image' parameters for compatibility
    image_data = req.get("face_image") or req.get("image")
    if not image_data:
        raise HTTPException(status_code=400, detail="No face image provided.")
    
    try:
        from services.inference import run_inference
        embedding = await run_inference(get_face_embedding, image_data)
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


@router.get("/settings")
async def get_public_settings(request: Request):
    """Public settings for mobile app (timings, etc.). Supports Org-specific overrides if token present."""
    auth_header = request.headers.get("Authorization")
    org_id = None
    
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        try:
            user, _ = await resolve_access_token(token, "employee")
            org_id = user.get("organization_id")
        except Exception:
            pass # Invalid token, fall back to global
            
    if org_id:
        settings = await settings_collection.find_one({"organization_id": org_id})
        if settings:
            return sanitize_public_settings(settings)
            
    # Fallback to global config
    settings = await settings_collection.find_one({"id": "config"})
    if not settings:
        return SystemSettings().model_dump()
    return sanitize_public_settings(settings)


@router.get("/organizations/search")
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
        
        # Format results (strip oversized inline base64 logos to avoid Android 2KB SecureStore limit)
        results = []
        for org in orgs:
            raw_logo = org.get("logo_url")
            safe_logo = None if (raw_logo and len(str(raw_logo)) > 500) else raw_logo
            result_item = {
                "id": str(org["_id"]),
                "name": org.get("name", "Unknown"),
                "slug": org.get("slug", ""),
                "logo_url": safe_logo,
                "primary_color": org.get("primary_color", "#0f172a"),
                "_id": str(org["_id"])  # Include both 'id' and '_id' for compatibility
            }
            results.append(result_item)
        
        # Return plain list for Play Store mobile app FlatList compatibility
        return results
        
    except Exception as e:
        logger.error(f"[SEARCH] Error searching organizations: {str(e)}", exc_info=True)
        return []


@router.get("/organizations/debug")
async def debug_organizations(current_admin: Admin = Depends(require_feature("settings"))):
    """Debug endpoint: List all organizations in database (superadmin only, disabled in prod)"""
    if APP_ENV == "production" or not _is_superadmin(current_admin):
        raise HTTPException(status_code=403, detail="Debug endpoint not available")
    
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


