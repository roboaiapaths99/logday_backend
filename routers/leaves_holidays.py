from upload_security import save_local_image
from upload_security import SecureMediaRoute
from permissions import require_feature, require_any_feature, require_admin_session
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
    get_current_admin, get_current_employee, admin_oauth2_scheme, employee_oauth2_scheme,
    SECRET_KEY, ALGORITHM
)
from attendance_service import classify_attendance_day
from face_utils import get_face_embedding, verify_face, compare_faces, is_dummy_embedding
from sheets_sync import sync_to_google_sheets, sync_visit_to_google_sheets

logger = logging.getLogger("main")
APP_ENV = os.getenv("APP_ENV", "development")
from services.common_helpers import (
    BASE_DIR, LOG_DIR, GEOCODE_CACHE, MAX_GEOCODE_CACHE_SIZE,
    UPLOAD_DIR, PUBLIC_UPLOAD_FOLDERS,
    S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY,
    S3_BUCKET_NAME, S3_REGION_NAME, S3_PUBLIC_URL_PREFIX,
    build_static_upload_url,
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

@router.post("/api/leave/request")
async def create_leave_request(req: dict, employee=Depends(get_current_employee)):
    """Submit a new Leave or On-Duty (OD) request with optional duration (full_day, half_day, short_leave) and proof."""
    try:
        proof_file_url = None
        if req.get("proof_url"):
            proof_file_url = await save_local_image(req["proof_url"], employee, "leave_proof", "proofs")

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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to create leave request: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to submit request")


@router.get("/api/leave/my-requests")
async def get_my_leave_requests(employee=Depends(get_current_employee)):
    """List current employee's leave requests."""
    query = {"employee_id": employee["email"]}
    if employee.get("organization_id"):
        query["organization_id"] = employee["organization_id"]
    requests = await leave_requests_collection.find(query).sort("created_at", -1).to_list(length=100)
    for r in requests:
        r["_id"] = str(r["_id"])
        r["id"] = r["_id"]
        if isinstance(r.get("created_at"), datetime):
            r["created_at"] = r["created_at"].isoformat()
    return requests


@router.get("/api/leave/my-balance")
@router.get("/api/leave/balance")
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


@router.get("/api/manager/team-attendance")
async def get_team_attendance(manager=Depends(get_current_employee)):
    """Fetch current attendance status for all subordinates."""
    sub_query = {"manager_id": manager["email"]}
    if manager.get("organization_id"):
        sub_query["organization_id"] = manager["organization_id"]
    cursor = employees_collection.find(sub_query, {"face_embedding": 0, "hashed_password": 0})
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


@router.get("/api/manager/pending-leaves")
async def get_pending_leaves(manager=Depends(get_current_employee)):
    """Fetch pending leave requests for subordinates."""
    sub_query = {"manager_id": manager["email"]}
    if manager.get("organization_id"):
        sub_query["organization_id"] = manager["organization_id"]
    cursor = employees_collection.find(sub_query, {"email": 1})
    subordinates = await cursor.to_list(length=100)
    sub_emails = [s["email"] for s in subordinates if s.get("email")]
    
    leave_query = {
        "employee_id": {"$in": sub_emails},
        "status": "pending"
    }
    if manager.get("organization_id"):
        leave_query["organization_id"] = manager["organization_id"]
    requests = await leave_requests_collection.find(leave_query).sort("created_at", -1).to_list(length=100)
    
    for r in requests:
        r["_id"] = str(r["_id"])
        r["id"] = r["_id"]
        if isinstance(r.get("created_at"), datetime):
            r["created_at"] = r["created_at"].isoformat()
    return requests


@router.post("/api/manager/leave-decision")
async def leave_decision(payload: dict, manager=Depends(get_current_employee)):
    """Approve or reject a leave request (Manager level)."""
    try:
        req_id = payload.get("request_id")
        action = payload.get("action") # "approve" or "reject"
        is_paid = payload.get("is_paid")
        admin_notes = payload.get("admin_notes")
        
        if action not in ["approve", "reject"]:
            raise HTTPException(status_code=400, detail="Invalid action")
        if not req_id or not ObjectId.is_valid(str(req_id)):
            raise HTTPException(status_code=400, detail="Invalid request_id")
            
        obj_id = ObjectId(req_id)
        leave_req = await leave_requests_collection.find_one({"_id": obj_id})
        if not leave_req:
            raise HTTPException(status_code=404, detail="Request not found")
            
        # Verify manager owns this subordinate within the same organization
        emp_query = {"email": leave_req["employee_id"], "manager_id": manager["email"]}
        if manager.get("organization_id"):
            emp_query["organization_id"] = manager["organization_id"]
        emp = await employees_collection.find_one(emp_query)
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


@router.get("/admin/leave/requests")
async def admin_get_leave_requests(status: Optional[str] = None, admin: dict = Depends(require_feature("leaves"))):
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


@router.get("/api/leave/requests/{request_id}/discussion")
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


@router.post("/api/leave/requests/{request_id}/message")
async def post_leave_message(request_id: str, payload: dict, auth_data=Depends(get_current_any_user)):
    """Post a new message to the leave request discussion (Chat). Supports both Admin and Employee tokens."""
    try:
        obj_id = ObjectId(request_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request ID")
        
    if not payload or "message" not in payload:
        raise HTTPException(status_code=400, detail="Message content is required")

    req = await leave_requests_collection.find_one({"_id": obj_id})
    if not req:
        raise HTTPException(status_code=404, detail="Request not found")
        
    user, role = auth_data
    
    if role == "admin":
        admin_org = user.organization_id
        if not _is_superadmin(user) and admin_org and admin_org != req.get("organization_id"):
            raise HTTPException(status_code=403, detail="Access denied")
        if getattr(user, "role", "") == "manager":
            emp = await employees_collection.find_one({"email": req.get("employee_id"), **get_employee_filter(user)})
            if not emp:
                raise HTTPException(status_code=403, detail="Access denied")
        sender_id = user.email
        sender_name = user.full_name
    else:
        if user.get("email") != req.get("employee_id") or (
            user.get("organization_id") and req.get("organization_id") and user.get("organization_id") != req.get("organization_id")
        ):
            raise HTTPException(status_code=403, detail="Access denied")
        sender_id = user.get("email")
        sender_name = user.get("full_name", user.get("name", "Unknown"))

    message = {
        "sender_id": sender_id,
        "sender_name": sender_name,
        "role": role,
        "message": payload["message"],
        "timestamp": datetime.now(timezone.utc)
    }
    
    await leave_requests_collection.update_one(
        {"_id": obj_id},
        {"$push": {"discussion": message}}
    )
        
    return {"status": "success"}


@router.post("/admin/leave/requests/{request_id}/{action}")
async def handle_leave_request(request_id: str, action: str, req_body: Optional[dict] = None, admin: dict = Depends(require_feature("leaves"))):
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


@router.put("/admin/leave/requests/{request_id}/payment-status")
async def toggle_leave_payment_status(request_id: str, req_body: dict, admin: dict = Depends(require_feature("leaves"))):
    """Toggle whether an approved leave is Paid or Loss of Pay (LOP) for payroll."""
    try:
        obj_id = ObjectId(request_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request ID")

    leave_req = await leave_requests_collection.find_one({"_id": obj_id})
    if not leave_req:
        raise HTTPException(status_code=404, detail="Leave request not found")

    filter_query = get_employee_filter(admin)
    if "manager_id" in filter_query:
        emp = await employees_collection.find_one({"email": leave_req.get("employee_id"), **filter_query})
        if not emp:
            raise HTTPException(status_code=403, detail="Access denied: You can only manage leaves for your direct reports.")
    elif not _is_superadmin(admin) and admin.organization_id and admin.organization_id != leave_req.get("organization_id"):
        raise HTTPException(status_code=403, detail="Access denied: This request belongs to another organization.")

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


@router.post("/admin/attendance/convert-absent-to-leave")
async def convert_absent_to_leave(req: ConvertAbsentToLeaveRequest, admin: Admin = Depends(require_feature("attendance"))):
    """Admin regularization: Convert an Absent day into an Approved Leave (e.g. informed via Email/WhatsApp)."""
    clean_email = req.employee_email.strip().lower()
    clean_date = normalize_date_str(req.date)
    if not clean_date:
        raise HTTPException(status_code=400, detail="Invalid date format. Expected YYYY-MM-DD or DD/MM/YYYY")

    emp_scope = get_employee_filter(admin)
    emp = await employees_collection.find_one({"email": clean_email, **emp_scope})
    if not emp:
        # Try finding by employee_id within admin's tenant/manager scope
        emp = await employees_collection.find_one({"employee_id": req.employee_email.strip(), **emp_scope})
        if not emp:
            raise HTTPException(status_code=404, detail="Employee not found")
        clean_email = emp["email"]

    org_id = emp.get("organization_id") or admin.organization_id or "system_org"

    # Check if there is already an approved leave for this date
    existing = await leave_requests_collection.find_one({
        "employee_id": clean_email,
        "organization_id": org_id,
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


@router.post("/admin/holidays")
async def admin_create_holiday(req: HolidayCreateRequest, admin: dict = Depends(require_feature("leaves"))):
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


@router.post("/admin/holidays/bulk")
async def admin_bulk_create_holidays(req: HolidayBulkCreateRequest, admin: dict = Depends(require_feature("leaves"))):
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


@router.get("/admin/holidays")
async def admin_get_holidays(year: Optional[int] = None, month: Optional[str] = None, admin: dict = Depends(require_feature("leaves"))):
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


@router.delete("/admin/holidays/{holiday_id_or_date}")
async def admin_delete_holiday(holiday_id_or_date: str, admin: dict = Depends(require_feature("leaves"))):
    """Delete a holiday by its ID or YYYY-MM-DD date (strictly scoped to the admin's organization)."""
    org_id = admin.organization_id or "system_org"
    if ObjectId.is_valid(holiday_id_or_date):
        query = {"_id": ObjectId(holiday_id_or_date)}
        if not _is_superadmin(admin):
            query["organization_id"] = org_id
    else:
        if _is_superadmin(admin):
            query = {
                "$or": [
                    {"organization_id": org_id, "date": holiday_id_or_date},
                    {"organization_id": "system_org", "date": holiday_id_or_date}
                ]
            }
        else:
            query = {"organization_id": org_id, "date": holiday_id_or_date}

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


@router.get("/api/holidays")
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


@router.get("/admin/leave-policy")
async def admin_get_leave_policy(year: Optional[int] = None, admin: dict = Depends(require_feature("leaves"))):
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


@router.put("/admin/leave-policy")
async def admin_update_leave_policy(req: LeavePolicyUpdateRequest, admin: dict = Depends(require_feature("leaves"))):
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


@router.put("/admin/leave-policy/employee/{email}")
async def admin_update_employee_leave_quota(
    email: str,
    req: EmployeeLeaveQuotaUpdateRequest,
    admin: dict = Depends(require_feature("leaves"))
):
    """Set custom leave quota override for a specific employee."""
    clean_email = email.strip().lower()
    emp = await employees_collection.find_one({"email": clean_email, **get_employee_filter(admin)})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    org_id = emp.get("organization_id") or admin.organization_id or "system_org"
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


@router.get("/admin/leave-policy/employee/{email}/balance")
async def admin_get_employee_leave_balance(
    email: str,
    year: Optional[int] = None,
    month: Optional[str] = None,
    admin: dict = Depends(require_feature("leaves"))
):
    """Fetch live calculated leave balance for an employee."""
    clean_email = email.strip().lower()
    emp = await employees_collection.find_one({"email": clean_email, **get_employee_filter(admin)})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")
    org_id = emp.get("organization_id") or admin.organization_id or "system_org"
    balance = await calculate_leave_balance(clean_email, org_id, year, month)
    return balance


