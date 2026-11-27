from upload_security import save_local_image, save_local_bytes, decode_binary_payload
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

@router.post("/api/field/plan")
async def submit_visit_plan(plan: VisitPlan, employee=Depends(get_current_employee)):
    """Submit a daily visit plan for approval."""
    try:
        # Security Enforcement: Identity Hijack Prevention
        plan_dict = plan.model_dump()
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


@router.get("/api/field/plan/{employee_id}")
async def get_current_plan(employee_id: str, date: Optional[str] = None, current_user=Depends(get_current_employee)):
    """Retrieve the active (approved) plan for today."""
    user_email = (current_user.get("email") or "").lower().strip()
    user_emp_code = str(current_user.get("employee_id") or "").strip()
    req_id = employee_id.lower().strip()

    if req_id != user_email and employee_id.strip() != user_emp_code:
        raise HTTPException(status_code=403, detail="Access denied: Cannot view another agent's plan.")
        
    query_date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    plan_filter = {
        "$or": [
            {"employee_id": user_email},
            {"employee_id": employee_id},
            {"employee_id": user_emp_code}
        ] if user_emp_code else [{"employee_id": user_email}, {"employee_id": employee_id}],
        "date": query_date
    }
    plan = await visit_plans_collection.find_one({**plan_filter, "status": PlanStatus.APPROVED})
    
    if not plan:
        # Check for submitted/draft if no approved one exists
        plan = await visit_plans_collection.find_one(plan_filter)
        
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


@router.post("/api/field/plan/optimize")
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


@router.post("/api/field/visit/check-in")
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
            selfie_url = await save_local_image(req["selfie_base64"], employee, "field_media", "selfies")

            # Optional face verification against stored descriptors
            try:
                user = await employees_collection.find_one({"email": employee["email"]})
                if user and user.get("face_embedding"):
                    from face_utils import verify_face as _v_face
                    from services.inference import run_inference
                    match, distance = await run_inference(_v_face, req["selfie_base64"], user["face_embedding"])
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

        site_photo_url = None
        if req.get("site_photo_base64"):
            site_photo_url = await save_local_image(req["site_photo_base64"], employee, "field_media", "site_photos")

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
            "site_photo_url": site_photo_url,
            "face_verified": face_verified,
            "status": "ongoing"
        }
        
        if agent_lat is not None and agent_lng is not None:
            log["check_in_location"] = {
                "type": "Point",
                "coordinates": [float(agent_lng), float(agent_lat)]
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


@router.post("/api/field/visit/check-out")
async def visit_check_out(req: dict, background_tasks: BackgroundTasks, employee=Depends(get_current_employee)):
    """Log check-out with remarks, media, person met, and outcome."""
    try:
        raw_visit_id = str(req.get("visit_id") or "").strip()
        if not raw_visit_id or not ObjectId.is_valid(raw_visit_id):
            raise HTTPException(status_code=400, detail="Invalid or missing visit_id")
        visit_obj_id = ObjectId(raw_visit_id)

        # Security: Verify that this visit belongs to BOTH the organization AND the logged-in employee
        visit = await visit_logs_collection.find_one({"_id": visit_obj_id})
        emp_email = (employee.get("email") or "").strip().lower()
        visit_owner = (visit.get("employee_id") or visit.get("employee_email") or "").strip().lower() if visit else ""
        if not visit or visit.get("organization_id") != employee.get("organization_id") or (visit_owner and visit_owner != emp_email):
            raise HTTPException(status_code=403, detail="Access denied: Visit record not found or ownership violation.")

        # --- Optional Face Verification for Check-out ---
        face_verified = True
        if req.get("selfie_base64"):
            try:
                target_descriptor = employee.get("face_embedding")
                if target_descriptor:
                    from services.inference import run_inference
                    is_match, score = await run_inference(verify_face, req["selfie_base64"], target_descriptor)
                    face_verified = is_match
                    if not is_match:
                        await trigger_alert(
                            "Identity", 
                            employee["email"], 
                            employee["organization_id"],
                            f"Face mismatch during check-out for visit {raw_visit_id}.",
                            "high",
                            {"id": raw_visit_id, "score": score}
                        )
                else:
                    logger.warning(f"Employee {employee['email']} has no enrolled face for verification.")
            except Exception as e:
                logger.error(f"Face verification during check-out failed: {e}")
                face_verified = False

        # --- Save voice note if provided ---
        voice_note_url = None
        if req.get("voice_note_base64"):
            audio_bytes = decode_binary_payload(req["voice_note_base64"])
            voice_note_url = await save_local_bytes(audio_bytes, employee, "field_media", "voice_notes", ".m4a", "audio/mp4")

        # --- Save site photo if provided ---
        site_photo_url = None
        if req.get("site_photo_base64"):
            site_photo_url = await save_local_image(req["site_photo_base64"], employee, "field_media", "site_photos")

        out_lat = req.get("lat") if req.get("lat") is not None else req.get("latitude")
        out_lng = req.get("lng") if req.get("lng") is not None else (req.get("long") if req.get("long") is not None else req.get("longitude"))

        update_data = {
            "check_out_time": datetime.now(timezone.utc),
            "check_out_lat": out_lat,
            "check_out_lng": out_lng,
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
            {"_id": visit_obj_id},
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


@router.post("/api/field/ping")
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

        ping_dict = ping.model_dump()
        ping_dict["employee_id"] = employee["email"]
        ping_dict["organization_id"] = employee["organization_id"]
        ping_dict["recorded_at"] = now
        if ping.lat is not None and ping.lng is not None:
            ping_dict["location"] = {
                "type": "Point",
                "coordinates": [float(ping.lng), float(ping.lat)]
            }
        
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


@router.post("/api/field/sync/batch")
async def sync_offline_batch(req: SyncBatchRequest, background_tasks: BackgroundTasks, employee=Depends(get_current_employee)):
    """Synchronize offline data back to the server with per-item acknowledgements, account ownership verification, and atomic idempotency."""
    try:
        from upload_security import save_local_image
        now = datetime.now(timezone.utc)
        synced_count = {"attendance": 0, "visits": 0, "pings": 0}
        acknowledged_ids = {"attendance": [], "visits": [], "pings": []}
        emp_email = (employee.get("email") or "").strip().lower()
        emp_org = employee["organization_id"]

        # 1. Sync Attendance Logs
        org_settings = await get_effective_org_settings(emp_org)
        tz_offset = int(org_settings.get("timezone_offset", 330))

        for att in req.attendance_logs:
            offline_id = str(att.get("offline_id") or "").strip()
            if not offline_id:
                continue
            item_owner = str(att.get("offline_owner") or att.get("email") or "").strip().lower()
            if item_owner and item_owner not in (emp_email, "anonymous"):
                logger.warning(f"Skipping offline attendance item {offline_id}: owner '{item_owner}' != current user '{emp_email}'")
                continue

            att_doc = dict(att)
            att_doc["offline_id"] = offline_id
            att_doc["synced_at"] = now
            att_doc["user_id"] = str(employee["_id"])
            att_doc["email"] = emp_email
            att_doc["full_name"] = employee.get("full_name")
            att_doc["organization_id"] = emp_org
            if "timestamp" in att_doc and isinstance(att_doc["timestamp"], str):
                try:
                    att_doc["timestamp"] = datetime.fromisoformat(att_doc["timestamp"].replace("Z", "+00:00"))
                except Exception:
                    pass

            try:
                l_ts = att_doc.get("timestamp") or now
                if l_ts.tzinfo is None:
                    l_ts = l_ts.replace(tzinfo=timezone.utc)
                local_ts = l_ts + timedelta(minutes=tz_offset)
                att_type = att_doc.get("type", "check-in")

                classification = classify_attendance_day(
                    first_checkin_local=local_ts,
                    last_checkout_local=None if att_type in ["check-in", "IN"] else local_ts,
                    worked_hours=0.0,
                    settings=org_settings or {},
                    employee_type=employee.get("employee_type", "field"),
                    approved_leave=None
                )
                att_doc["status"] = "SUCCESS"
                att_doc["attendance_status"] = classification["status"]
                att_doc["is_late"] = classification["is_late"]
                att_doc["is_early_leave"] = classification["is_early_leave"]
                att_doc["is_short_leave"] = classification["is_short_leave"]
                att_doc["is_half_day"] = classification["is_half_day"]
                att_doc["short_leave_type"] = classification["short_leave_type"]
                att_doc["half_day_type"] = classification["half_day_type"]
                att_doc["late_mins"] = classification["late_mins"]
                att_doc["early_exit_mins"] = classification["early_exit_mins"]
                att_doc["late_off_hours"] = classification["late_off_hours"]
                att_doc["early_off_hours"] = classification["early_off_hours"]
                att_doc["off_hours"] = classification["off_hours"]
                att_doc["effective_days"] = classification["effective_days"]
                att_doc["classification_reason"] = classification["classification_reason"]
            except Exception as ex:
                logger.warning(f"Offline sync classification error: {ex}")

            res = await attendance_logs_collection.update_one(
                {"organization_id": emp_org, "offline_id": offline_id},
                {"$setOnInsert": att_doc},
                upsert=True,
            )
            if getattr(res, "upserted_id", None) is not None:
                synced_count["attendance"] += 1
            acknowledged_ids["attendance"].append(offline_id)

        # 2. Sync Visits
        for visit in req.visits:
            offline_id = str(visit.get("offline_id") or "").strip()
            if not offline_id:
                continue
            item_owner = str(visit.get("offline_owner") or visit.get("employee_id") or "").strip().lower()
            if item_owner and item_owner not in (emp_email, "anonymous"):
                logger.warning(f"Skipping offline visit item {offline_id}: owner '{item_owner}' != current user '{emp_email}'")
                continue

            visit_doc = dict(visit)
            visit_doc["offline_id"] = offline_id
            visit_doc["synced_at"] = now
            visit_doc["employee_id"] = emp_email
            visit_doc["organization_id"] = emp_org

            for date_field in ["check_in_time", "check_out_time"]:
                if date_field in visit_doc and isinstance(visit_doc[date_field], str):
                    try:
                        visit_doc[date_field] = datetime.fromisoformat(visit_doc[date_field].replace("Z", "+00:00"))
                    except Exception:
                        pass

            if visit_doc.get("site_photo_base64"):
                visit_doc["site_photo_url"] = await save_local_image(visit_doc["site_photo_base64"], employee, "field_media", "site_photos")
                visit_doc.pop("site_photo_base64", None)

            if visit_doc.get("check_in_lat") is not None and visit_doc.get("check_in_lng") is not None:
                try:
                    visit_doc["check_in_location"] = {
                        "type": "Point",
                        "coordinates": [float(visit_doc["check_in_lng"]), float(visit_doc["check_in_lat"])]
                    }
                except (ValueError, TypeError):
                    pass

            res = await visit_logs_collection.update_one(
                {"organization_id": emp_org, "offline_id": offline_id},
                {"$setOnInsert": visit_doc},
                upsert=True,
            )
            if getattr(res, "upserted_id", None) is not None:
                background_tasks.add_task(sync_visit_to_google_sheets, visit_doc)
                synced_count["visits"] += 1
            acknowledged_ids["visits"].append(offline_id)

        # 3. Sync Pings
        for ping in req.pings:
            offline_id = str(ping.get("offline_id") or "").strip()
            if not offline_id:
                continue
            item_owner = str(ping.get("offline_owner") or ping.get("employee_id") or "").strip().lower()
            if item_owner and item_owner not in (emp_email, "anonymous"):
                logger.warning(f"Skipping offline ping item {offline_id}: owner '{item_owner}' != current user '{emp_email}'")
                continue

            ping_doc = dict(ping)
            ping_doc["offline_id"] = offline_id
            ping_doc["synced_at"] = now
            ping_doc["employee_id"] = emp_email
            ping_doc["organization_id"] = emp_org
            if "recorded_at" in ping_doc and isinstance(ping_doc["recorded_at"], str):
                try:
                    ping_doc["recorded_at"] = datetime.fromisoformat(ping_doc["recorded_at"].replace("Z", "+00:00"))
                except Exception:
                    pass
            if ping_doc.get("lat") is not None and ping_doc.get("lng") is not None:
                try:
                    ping_doc["location"] = {
                        "type": "Point",
                        "coordinates": [float(ping_doc["lng"]), float(ping_doc["lat"])]
                    }
                except (ValueError, TypeError):
                    pass

            res = await location_pings_collection.update_one(
                {"organization_id": emp_org, "offline_id": offline_id},
                {"$setOnInsert": ping_doc},
                upsert=True,
            )
            if getattr(res, "upserted_id", None) is not None:
                synced_count["pings"] += 1
            acknowledged_ids["pings"].append(offline_id)

        return {"status": "success", "synced": synced_count, "acknowledged_ids": acknowledged_ids}

    except Exception as e:
        logger.error(f"Batch sync failed: {e}")
        raise HTTPException(status_code=500, detail="Batch synchronization failed")


async def _get_admin_field_scope_query(admin, base_query: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Scope field admin queries by organization_id and manager team hierarchy."""
    query = dict(base_query or {})
    if not _is_superadmin(admin) and getattr(admin, "organization_id", None):
        query["organization_id"] = admin.organization_id
    if getattr(admin, "role", None) in (AdminRole.MANAGER, "manager"):
        scoped_emails = await get_scoped_employee_emails(admin)
        scoped_emp_ids = await get_scoped_employee_employee_ids(admin)
        allowed_ids = list(set((scoped_emails or []) + (scoped_emp_ids or [])))
        if "employee_id" in query and isinstance(query["employee_id"], str):
            if query["employee_id"] not in allowed_ids:
                query["employee_id"] = "__unauthorized__"
        else:
            query["employee_id"] = {"$in": allowed_ids}
    return query


@router.get("/api/field/km-suggestion")
async def get_km_suggestion(employee=Depends(get_current_employee)):
    """Calculate suggested KM for today based on active duty pings."""
    try:
        org_id = employee.get("organization_id")
        org_settings = await get_effective_org_settings(org_id) if org_id else {}
        tz_offset = org_settings.get("timezone_offset", employee.get("timezone_offset", 330))
        start_of_day = get_today_start(tz_offset)

        ping_query = {
            "employee_id": employee["email"],
            "recorded_at": {"$gte": start_of_day}
        }
        if org_id:
            ping_query["organization_id"] = org_id

        pings = await location_pings_collection.find(ping_query).sort("recorded_at", 1).to_list(length=2000)
        
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


@router.post("/api/field/reimbursement/claim")
async def submit_km_reimbursement(req: dict, employee=Depends(get_current_employee)):
    """Submit a formal KM reimbursement claim based on suggested KM."""
    try:
        date_str = req.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        total_km = float(req.get("total_km", 0))
        org_id = employee.get("organization_id")
        
        # Check if already claimed for this date within the organization
        existing_query = {
            "employee_id": employee["email"],
            "date": date_str
        }
        if org_id:
            existing_query["organization_id"] = org_id
        existing = await km_reimbursements_collection.find_one(existing_query)
        if existing:
            raise HTTPException(status_code=400, detail=f"KM reimbursement already claimed for {date_str}")
        
        # Fetch dynamic rate from organization settings
        org_settings = await settings_collection.find_one({"organization_id": org_id})
        rate_per_km = 10.0 # Default
        if org_settings and "field_rate_per_km" in org_settings:
            rate_per_km = float(org_settings["field_rate_per_km"])
        
        claim = {
            "employee_id": employee["email"],
            "organization_id": org_id,
            "date": date_str,
            "total_km": total_km,
            "rate_per_km": rate_per_km,
            "total_amount": total_km * rate_per_km,
            "status": "pending",
            "created_at": datetime.now(timezone.utc)
        }
        
        result = await km_reimbursements_collection.insert_one(claim)
        return {"success": True, "claim_id": str(result.inserted_id)}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to submit KM reimbursement: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/admin/field/reimbursements")
async def admin_list_km_claims(status: Optional[str] = "pending", admin=Depends(require_feature("war_room"))):
    """List KM reimbursement claims for approval."""
    base_query = {"status": status} if status else {}
    query = await _get_admin_field_scope_query(admin, base_query)
        
    claims = await km_reimbursements_collection.find(query).sort("created_at", -1).to_list(length=200)
    
    emp_emails = list({c["employee_id"] for c in claims if c.get("employee_id")})
    emp_lookup_query = {"email": {"$in": emp_emails}}
    if admin.organization_id:
        emp_lookup_query["organization_id"] = admin.organization_id
    employees = await employees_collection.find(emp_lookup_query, {"email": 1, "full_name": 1}).to_list(length=len(emp_emails) + 1)
    emp_map = {e["email"]: e["full_name"] for e in employees}

    for c in claims:
        c["_id"] = str(c["_id"])
        if isinstance(c.get("created_at"), datetime):
            c["created_at"] = c["created_at"].isoformat()
        c["full_name"] = emp_map.get(c.get("employee_id"), "Unknown")
        
    return claims


@router.post("/admin/field/reimbursements/{claim_id}/{action}")
async def process_km_reimbursement(claim_id: str, action: str, admin=Depends(require_feature("war_room"))):
    "Approve or Reject a KM reimbursement claim."
    if not ObjectId.is_valid(claim_id):
        raise HTTPException(status_code=400, detail="Invalid claim ID")

    new_status = "approved" if action == "approve" else "rejected"
    query = await _get_admin_field_scope_query(admin, {"_id": ObjectId(claim_id)})

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


@router.get("/admin/field/trail/{employee_email}")
async def get_agent_trail(employee_email: str, admin=Depends(require_feature("war_room"))):
    """Fetch today's location trail for a specific agent (Admin only)."""
    try:
        base_filter = get_employee_filter(admin)
        emp_query = {"email": employee_email, **base_filter}
        
        emp = await employees_collection.find_one(emp_query, {"face_embedding": 0, "hashed_password": 0})
        if not emp:
            raise HTTPException(status_code=404, detail="Employee not found or unauthorized")

        org_id = emp.get("organization_id") or getattr(admin, "organization_id", None)
        org_settings = await get_effective_org_settings(org_id) if org_id else {}
        tz_offset = org_settings.get("timezone_offset", emp.get("timezone_offset", 330))
        start_of_day = get_today_start(tz_offset)

        ping_query = {
            "employee_id": employee_email,
            "recorded_at": {"$gte": start_of_day}
        }
        if org_id:
            ping_query["organization_id"] = org_id

        pings = await location_pings_collection.find(ping_query).sort("recorded_at", 1).to_list(length=2000)
        
        trail = [[p["lat"], p["lng"]] for p in pings]
        return {"trail": trail}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Trail fetch failed: {e}")
        return {"trail": []}


@router.get("/api/field/summary/{employee_id}")
async def get_field_day_summary(employee_id: str, date: Optional[str] = None, current_user=Depends(get_current_employee)):
    """Get summarized KM and Visit count for the day."""
    # Privacy Enforcement
    if employee_id != current_user["email"]:
        raise HTTPException(status_code=403, detail="Access denied: Cannot view another agent's summary.")

    org_id = current_user.get("organization_id")
    org_settings_doc = await get_effective_org_settings(org_id) if org_id else {}
    tz_offset = org_settings_doc.get("timezone_offset", current_user.get("timezone_offset", 330))
    today_start = get_today_start(tz_offset)
    local_now = datetime.now(timezone.utc) + timedelta(minutes=tz_offset)
    query_date = date or local_now.strftime("%Y-%m-%d")
    
    # 1. Total Visits
    visit_query = {
        "employee_id": employee_id,
        "date": query_date,
        "status": "completed"
    }
    if org_id:
        visit_query["organization_id"] = org_id
    visits = await visit_logs_collection.count_documents(visit_query)
    
    # 2. Total KM (Sum of suggested KM or accepted claims for today)
    # For now, we'll try to get it from location pings if recorded
    total_km = 0.0
    ping_query = {
        "employee_id": employee_id,
        "recorded_at": {
            "$gte": today_start,
            "$lt": today_start + timedelta(days=1)
        }
    }
    if org_id:
        ping_query["organization_id"] = org_id
    pings = await location_pings_collection.find(ping_query).sort("recorded_at", 1).to_list(length=1000)
    
    if len(pings) > 1:
        for i in range(len(pings)-1):
            p1 = pings[i]
            p2 = pings[i+1]
            if "lat" in p1 and "lng" in p1 and "lat" in p2 and "lng" in p2:
                d = calculate_haversine(p1["lat"], p1["lng"], p2["lat"], p2["lng"])
                total_km += d
        total_km = total_km / 1000.0 # Convert to KM
    
    # 3. Current Attendance Status and Classification (TODAY ONLY)
    att_query = {
        "$or": [
            {"email": employee_id},
            {"user_id": employee_id},
            {"email": employee_id.lower().strip()},
            {"user_id": employee_id.lower().strip()}
        ],
        "timestamp": {"$gte": today_start}
    }
    if org_id:
        att_query["organization_id"] = org_id
    today_field_logs = await attendance_logs_collection.find(att_query).sort("timestamp", 1).to_list(length=100)

    first_in_log = next((l for l in today_field_logs if l.get("type") in ["check-in", "IN"]), None)
    last_out_log = next((l for l in reversed(today_field_logs) if l.get("type") in ["check-out", "OUT"]), None)

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


@router.put("/admin/employees/{email}/territory")
async def update_territory(email: str, req: dict, admin=Depends(require_feature("territory"))):
    """Update employee territory settings (radius or polygon)."""
    clean_email = email.lower().strip()
    
    # Check if employee exists within admin scope
    base_filter = get_employee_filter(admin)
    query = {"$or": [{"email": clean_email}, {"employee_id": clean_email}]}
    if base_filter:
        query = {"$and": [query, base_filter]}

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
        lat = req.get("territory_center_lat")
        lng = req.get("territory_center_lng")
        rad = req.get("territory_radius_meters")
        update_fields["territory_center_lat"] = float(lat) if (lat is not None and str(lat).strip() != "") else None
        update_fields["territory_center_lng"] = float(lng) if (lng is not None and str(lng).strip() != "") else None
        update_fields["territory_radius_meters"] = float(rad) if (rad is not None and str(rad).strip() != "") else 5000.0
        # Clear polygon fields when switching to radius
        update_fields["territory_polygon"] = None

    result = await employees_collection.update_one({"_id": user["_id"]}, {"$set": update_fields})
    return {"status": "success", "message": f"Territory updated to {req.get('territory_type', 'radius')} for {email}"}


@router.get("/admin/field/visit-plans")
async def get_plans_for_approval(status: str = "submitted", admin=Depends(require_feature("war_room"))):
    """List visit plans with filtering, execution progress, and proof media."""
    from collections import defaultdict
    base_query = {"status": status} if (status and status != "all") else {}
    query = await _get_admin_field_scope_query(admin, base_query)

    plans = await visit_plans_collection.find(query).sort([("date", -1), ("_id", -1)]).to_list(length=150)
    
    # Enrichment: Add employee names and details
    emp_ids = list({p["employee_id"] for p in plans if p.get("employee_id")})
    emp_lookup_query = {"$or": [{"employee_id": {"$in": emp_ids}}, {"email": {"$in": emp_ids}}]}
    if admin.organization_id:
        emp_lookup_query["organization_id"] = admin.organization_id
    employees = await employees_collection.find(
        emp_lookup_query,
        {"employee_id": 1, "email": 1, "full_name": 1, "phone": 1}
    ).to_list(length=len(emp_ids) * 2 + 10)
    
    emp_map = {}
    for e in employees:
        if e.get("employee_id"):
            emp_map[e["employee_id"]] = e
        if e.get("email"):
            emp_map[e["email"]] = e

    # Fetch visit logs for the dates of these plans to link live execution & proof photos
    plan_dates = list({p["date"] for p in plans if p.get("date")})
    visit_logs_query = {"date": {"$in": plan_dates}}
    if admin.organization_id:
        visit_logs_query["organization_id"] = admin.organization_id
        
    visit_logs = await visit_logs_collection.find(visit_logs_query).to_list(length=1000)
    
    # Group logs by (employee_identifier, date)
    logs_by_key = defaultdict(list)
    for vl in visit_logs:
        vl["_id"] = str(vl["_id"])
        if "check_in_time" in vl and isinstance(vl["check_in_time"], datetime):
            vl["check_in_time"] = vl["check_in_time"].isoformat()
        if "check_out_time" in vl and isinstance(vl["check_out_time"], datetime):
            vl["check_out_time"] = vl["check_out_time"].isoformat()
        emp_key = vl.get("employee_id")
        d_key = vl.get("date")
        if emp_key and d_key:
            logs_by_key[(emp_key, d_key)].append(vl)

    enriched_plans = []
    for plan in plans:
        plan["_id"] = str(plan["_id"])
        emp_obj = emp_map.get(plan.get("employee_id"), {})
        plan["full_name"] = emp_obj.get("full_name", plan.get("employee_id", "Unknown"))
        plan["employee_email"] = emp_obj.get("email", plan.get("employee_id"))
        plan["employee_phone"] = emp_obj.get("phone", "")

        # Find matching logs for this plan
        p_date = plan.get("date")
        emp_id = plan.get("employee_id")
        emp_email = emp_obj.get("email")
        matched_logs = logs_by_key.get((emp_id, p_date), [])
        if not matched_logs and emp_email:
            matched_logs = logs_by_key.get((emp_email, p_date), [])

        stops = plan.get("stops", [])
        completed_stops = 0
        ongoing_stops = 0

        for stop in stops:
            seq = stop.get("sequence_order")
            pname = (stop.get("place_name") or "").strip().lower()

            # Find matching log
            stop_log = None
            for lg in matched_logs:
                lg_stop_id = lg.get("visit_plan_stop_id")
                if lg_stop_id is not None and str(lg_stop_id) == str(seq):
                    stop_log = lg
                    break
            if not stop_log:
                for lg in matched_logs:
                    if (lg.get("place_name") or "").strip().lower() == pname:
                        stop_log = lg
                        break

            if stop_log:
                s_status = stop_log.get("status", "pending")
                if s_status == "completed":
                    completed_stops += 1
                elif s_status == "ongoing":
                    ongoing_stops += 1

                stop["execution"] = {
                    "visit_id": stop_log.get("_id"),
                    "status": s_status,
                    "check_in_time": stop_log.get("check_in_time"),
                    "check_out_time": stop_log.get("check_out_time"),
                    "check_in_lat": stop_log.get("check_in_lat"),
                    "check_in_lng": stop_log.get("check_in_lng"),
                    "check_out_lat": stop_log.get("check_out_lat"),
                    "check_out_lng": stop_log.get("check_out_lng"),
                    "geofence_validated": stop_log.get("geofence_validated", True),
                    "distance_meters": stop_log.get("geofence_distance_meters"),
                    "site_photo_url": stop_log.get("site_photo_url"),
                    "selfie_url": stop_log.get("selfie_url"),
                    "voice_note_url": stop_log.get("voice_note_url"),
                    "person_met_name": stop_log.get("person_met_name"),
                    "person_met_role": stop_log.get("person_met_role"),
                    "remarks": stop_log.get("remarks"),
                    "outcome": stop_log.get("outcome"),
                    "order_captured": stop_log.get("order_captured", False),
                    "lead_captured": stop_log.get("lead_captured", False),
                    "lead_details": stop_log.get("lead_details")
                }
            else:
                stop["execution"] = {
                    "status": "pending",
                    "geofence_validated": None,
                    "site_photo_url": None,
                    "selfie_url": None
                }

        plan["execution_summary"] = {
            "total_stops": len(stops),
            "completed_stops": completed_stops,
            "ongoing_stops": ongoing_stops,
            "all_completed": len(stops) > 0 and completed_stops == len(stops)
        }
        enriched_plans.append(plan)
        
    return enriched_plans


@router.post("/admin/field/visit-plans/{plan_id}/{action}")
async def process_visit_plan(plan_id: str, action: str, admin=Depends(require_feature("war_room"))):
    """Approve or Reject a visit plan."""
    if not ObjectId.is_valid(plan_id):
        raise HTTPException(status_code=400, detail="Invalid plan ID")

    new_status = PlanStatus.APPROVED if action == "approve" else PlanStatus.REJECTED
    query = await _get_admin_field_scope_query(admin, {"_id": ObjectId(plan_id)})
    result = await visit_plans_collection.update_one(
        query,
        {"$set": {"status": new_status, "processed_at": datetime.now(timezone.utc)}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Plan not found or access denied")
    return {"status": "success"}


@router.put("/admin/field/visit-plans/{plan_id}")
async def update_visit_plan(plan_id: str, req: dict, admin=Depends(require_feature("war_room"))):
    """Update a visit plan (reorder stops, edit details, add comments) before or after approval."""
    if not ObjectId.is_valid(plan_id):
        raise HTTPException(status_code=400, detail="Invalid plan ID")
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

        query = await _get_admin_field_scope_query(admin, {"_id": ObjectId(plan_id)})
        result = await visit_plans_collection.update_one(
            query,
            {"$set": update_data}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Plan not found or access denied")
            
        return {"status": "success", "message": "Plan updated successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to update plan: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/field/generate-otp/{employee_id}")
async def generate_attendance_otp(employee_id: str, admin=Depends(require_feature("war_room"))):
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


@router.get("/admin/field/live-status")
async def get_field_live_status(admin=Depends(require_feature("war_room"))):
    """Live operational data for the War Room map (optimized O(1) batched queries)."""
    try:
        # Apply RBAC: managers see only their team
        base_filter = get_employee_filter(admin)
        query = {**base_filter, "employee_type": "field"}
            
        field_emps = await employees_collection.find(query, {"face_embedding": 0, "hashed_password": 0}).to_list(length=100)
        if not field_emps:
            return JSONResponse(content={"agents": [], "stats": {"active": 0, "idle": 0, "breach": 0}})

        now = datetime.now(timezone.utc)
        emails = [emp["email"] for emp in field_emps if emp.get("email")]
        org_ids = list({emp.get("organization_id") for emp in field_emps if emp.get("organization_id")})

        # 1. Batch-fetch organization settings & timezone offsets
        settings_docs = await settings_collection.find({"organization_id": {"$in": org_ids}}).to_list(length=max(1, len(org_ids)))
        tz_by_org = {s["organization_id"]: s.get("timezone_offset", 330) for s in settings_docs if s.get("organization_id")}
        start_of_day_by_org = {oid: get_today_start(tz_by_org.get(oid, 330)) for oid in org_ids}
        earliest_start_of_day = min(start_of_day_by_org.values()) if start_of_day_by_org else get_today_start(330)

        # 2. Batch-fetch today's pings for all field employees (sorted ascending for Haversine KM calculation)
        today_pings_all = await location_pings_collection.find({
            "employee_id": {"$in": emails},
            "recorded_at": {"$gte": earliest_start_of_day}
        }).sort("recorded_at", 1).to_list(length=50000)

        pings_today_by_emp = {}
        latest_ping_by_emp = {}
        for p in today_pings_all:
            e_id = p.get("employee_id")
            if e_id:
                pings_today_by_emp.setdefault(e_id, []).append(p)
                latest_ping_by_emp[e_id] = p

        # For any employee without a ping today, fetch their most recent historical ping in one aggregation
        missing_ping_emails = [em for em in emails if em not in latest_ping_by_emp]
        if missing_ping_emails:
            hist_pings = await location_pings_collection.aggregate([
                {"$match": {"employee_id": {"$in": missing_ping_emails}}},
                {"$sort": {"recorded_at": -1}},
                {"$group": {"_id": "$employee_id", "doc": {"$first": "$$ROOT"}}}
            ]).to_list(length=len(missing_ping_emails))
            for hp in hist_pings:
                if hp.get("_id") and hp.get("doc"):
                    latest_ping_by_emp[hp["_id"]] = hp["doc"]

        # 3. Batch-fetch active check-in visit logs
        active_visits_all = await visit_logs_collection.find({
            "employee_id": {"$in": emails},
            "check_out_time": None
        }).sort("check_in_time", -1).to_list(length=1000)

        active_visit_by_emp = {}
        active_visit_ids = set()
        for av in active_visits_all:
            e_id = av.get("employee_id")
            if e_id and e_id not in active_visit_by_emp:
                active_visit_by_emp[e_id] = av
                if av.get("visit_id"):
                    active_visit_ids.add(av["visit_id"])

        # 4. Batch-fetch visit plans for any active visit_ids
        stop_place_by_visit_id = {}
        if active_visit_ids:
            plans = await visit_plans_collection.find({
                "organization_id": {"$in": org_ids},
                "stops.visit_id": {"$in": list(active_visit_ids)}
            }).to_list(length=500)
            for pl in plans:
                for st in pl.get("stops", []):
                    vid = st.get("visit_id")
                    if vid in active_visit_ids and st.get("place_name"):
                        stop_place_by_visit_id[vid] = st["place_name"]

        agents = []
        active_count = 0
        idle_count = 0
        breach_count = 0

        for emp in field_emps:
            try:
                emp_email = emp["email"]
                emp_org_id = emp.get("organization_id")
                ping = latest_ping_by_emp.get(emp_email)
                active_visit_log = active_visit_by_emp.get(emp_email)

                status = "Inactive"
                current_visit = None
                if active_visit_log and active_visit_log.get("visit_id"):
                    current_visit = stop_place_by_visit_id.get(active_visit_log["visit_id"])

                if ping and ping.get("recorded_at"):
                    rp = ping["recorded_at"]
                    if isinstance(rp, str):
                        try:
                            rp = datetime.fromisoformat(rp.replace("Z", "+00:00"))
                        except Exception:
                            rp = now

                    ping_time = rp.replace(tzinfo=timezone.utc) if rp.tzinfo is None else rp
                    if now - ping_time < timedelta(minutes=10):
                        status = "On-Site" if active_visit_log else "Traveling"
                        active_count += 1
                    else:
                        status = "Idle"
                        idle_count += 1

                # Filter today's pings by the employee's exact org start_of_day
                emp_start_of_day = start_of_day_by_org.get(emp_org_id, earliest_start_of_day)
                raw_emp_pings = pings_today_by_emp.get(emp_email, [])
                pings_today_list = [
                    p for p in raw_emp_pings
                    if not isinstance(p.get("recorded_at"), datetime)
                    or (p["recorded_at"].replace(tzinfo=timezone.utc) if p["recorded_at"].tzinfo is None else p["recorded_at"]) >= emp_start_of_day
                ]

                total_km_calc = 0.0
                if len(pings_today_list) > 1:
                    for i in range(len(pings_today_list) - 1):
                        p1 = pings_today_list[i]
                        p2 = pings_today_list[i + 1]
                        if p1.get("lat") and p1.get("lng") and p2.get("lat") and p2.get("lng"):
                            lat1, lon1 = math.radians(p1["lat"]), math.radians(p1["lng"])
                            lat2, lon2 = math.radians(p2["lat"]), math.radians(p2["lng"])
                            dlat = lat2 - lat1
                            dlon = lon2 - lon1
                            a = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
                            c = 2 * math.asin(math.sqrt(a))
                            total_km_calc += 6371 * c

                km_today_final = float(round(total_km_calc, 2))

                lp = ping.get("recorded_at") if ping else None
                if isinstance(lp, datetime):
                    lp = lp.isoformat()

                agents.append({
                    "id": str(emp["_id"]),
                    "email": str(emp_email),
                    "name": str(emp.get("full_name", emp_email)),
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
        content = {"error": "Failed to load live field status"} if APP_ENV == "production" else {"error": str(e), "traceback": err}
        return JSONResponse(status_code=500, content=content)


@router.post("/api/field/expenses")
async def submit_expense(req: dict, employee=Depends(get_current_employee)):
    # Handle Base64 Receipt Image if present
    receipt_url = req.get("receipt_url", "")
    if receipt_url:
        receipt_url = await save_local_image(receipt_url, employee, "expense_receipt", "receipts")

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


@router.get("/api/field/expenses")
async def get_my_expenses(employee=Depends(get_current_employee)):
    """Field employee fetches their own expense claims."""
    query = {"employee_id": employee["email"]}
    if employee.get("organization_id"):
        query["organization_id"] = employee["organization_id"]
    claims = await expense_claims_collection.find(
        query
    ).sort("created_at", -1).to_list(length=100)
    for c in claims:
        c["_id"] = str(c["_id"])
        if isinstance(c.get("created_at"), datetime):
            c["created_at"] = c["created_at"].isoformat()
        if isinstance(c.get("resolved_at"), datetime):
            c["resolved_at"] = c["resolved_at"].isoformat()
    return claims


@router.get("/admin/expenses")
async def admin_list_expenses(status: Optional[str] = None, admin=Depends(require_feature("expenses"))):
    """Admin fetches all expense claims, optionally filtered by status."""
    base_query = {"status": status} if status else {}
    query = await _get_admin_field_scope_query(admin, base_query)
    
    claims = await expense_claims_collection.find(query).sort("created_at", -1).to_list(length=200)
    
    emp_emails = list({c.get("employee_id") for c in claims if c.get("employee_id")})
    emp_lookup_query = {"email": {"$in": emp_emails}}
    if admin.organization_id:
        emp_lookup_query["organization_id"] = admin.organization_id
    employees = await employees_collection.find(emp_lookup_query, {"email": 1, "full_name": 1}).to_list(length=len(emp_emails) + 1)
    emp_map = {e["email"]: e["full_name"] for e in employees}

    for c in claims:
        c["_id"] = str(c["_id"])
        if isinstance(c.get("created_at"), datetime):
            c["created_at"] = c["created_at"].isoformat()
        if isinstance(c.get("resolved_at"), datetime):
            c["resolved_at"] = c["resolved_at"].isoformat()
        c["employee_name"] = emp_map.get(c.get("employee_id"), c.get("employee_id", "Unknown"))
    return claims


@router.put("/admin/expenses/{claim_id}")
async def admin_update_expense(claim_id: str, req: dict, admin=Depends(require_feature("expenses"))):
    """Admin approves, rejects, or queries an expense claim."""
    if not ObjectId.is_valid(claim_id):
        raise HTTPException(status_code=400, detail="Invalid claim ID")

    action = req.get("action", "approve")  # approve | reject | query
    query = await _get_admin_field_scope_query(admin, {"_id": ObjectId(claim_id)})

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


@router.get("/admin/field/alerts")
async def get_alerts(
    type: Optional[str] = None,
    severity: Optional[str] = None,
    status: Optional[str] = "pending",
    employee_id: Optional[str] = None,
    source: Optional[str] = None,
    admin=Depends(require_feature("war_room"))
):
    """Retrieve filtered security and operational alerts."""
    base_query = {}
    if type: base_query["type"] = type
    if severity: base_query["severity"] = severity
    if status and status != "all": base_query["status"] = status
    if employee_id: base_query["employee_id"] = employee_id
    if source:
        base_query["metadata.source"] = source

    query = await _get_admin_field_scope_query(admin, base_query)
    alerts = await alerts_collection.find(query).sort("timestamp", -1).to_list(length=100)
    
    emp_emails = list({a["employee_id"] for a in alerts if a.get("employee_id")})
    emp_lookup_query = {"email": {"$in": emp_emails}}
    if admin.organization_id:
        emp_lookup_query["organization_id"] = admin.organization_id
    employees = await employees_collection.find(emp_lookup_query, {"email": 1, "full_name": 1}).to_list(length=len(emp_emails) + 1)
    emp_map = {e["email"]: e["full_name"] for e in employees}

    for a in alerts:
        a["_id"] = str(a["_id"])
        a["employee_name"] = emp_map.get(a.get("employee_id"), a.get("employee_id"))
        if isinstance(a.get("timestamp"), datetime):
            a["timestamp"] = a["timestamp"].isoformat()
            
    return alerts


@router.put("/admin/field/alerts/{alert_id}")
async def update_alert_status(alert_id: str, req: dict, admin=Depends(require_feature("war_room"))):
    """Update alert status (resolved, dismissed)."""
    if not ObjectId.is_valid(alert_id):
        raise HTTPException(status_code=400, detail="Invalid alert ID")

    status = req.get("status")
    if status not in ["resolved", "dismissed", "pending"]:
        raise HTTPException(status_code=400, detail="Invalid status")

    query = await _get_admin_field_scope_query(admin, {"_id": ObjectId(alert_id)})
    result = await alerts_collection.update_one(
        query,
        {"$set": {"status": status, "resolved_at": datetime.now(timezone.utc)}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"status": "success", "message": f"Alert marked as {status}"}


@router.get("/admin/field/heatmap-data")
async def get_heatmap_data(admin=Depends(require_feature("war_room"))):
    """Aggregation of pings for heatmap visualization."""
    org_id = getattr(admin, "organization_id", None)
    org_settings = await get_effective_org_settings(org_id) if org_id else {}
    tz_offset = org_settings.get("timezone_offset", 330)
    start_of_day = get_today_start(tz_offset)

    query = await _get_admin_field_scope_query(admin, {"recorded_at": {"$gte": start_of_day}})
    pings = await location_pings_collection.find(query).to_list(length=5000)
    
    # Format for Leaflet.heat: [[lat, lng, intensity], ...]
    heatmap_data = [[p["lat"], p["lng"], 0.5] for p in pings]
    return heatmap_data


@router.post("/admin/field/trigger-sla-check")
async def trigger_sla_check_manual(admin=Depends(require_feature("war_room"))):
    """Manually trigger the SLA check for debugging."""
    await check_missed_visits(force=True)
    return {"status": "success", "message": "Manual SLA check triggered"}


@router.post("/api/field/plan/template")
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


@router.get("/api/field/plan/templates/{employee_email}")
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


@router.delete("/api/field/plan/template/{template_id}")
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


@router.get("/api/field/leaderboard")
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

    top_ids = [row["_id"] for row in results if row.get("_id")]
    emp_lookup_query = {"$or": [{"email": {"$in": top_ids}}, {"employee_id": {"$in": top_ids}}]}
    if org_id:
        emp_lookup_query["organization_id"] = org_id
    emp_docs = await employees_collection.find(
        emp_lookup_query,
        {"email": 1, "employee_id": 1, "full_name": 1, "designation": 1}
    ).to_list(length=len(top_ids) * 2 + 1)
    emp_map = {}
    for e in emp_docs:
        if e.get("email"):
            emp_map[e["email"]] = e
        if e.get("employee_id"):
            emp_map[e["employee_id"]] = e

    leaderboard = []
    for idx, row in enumerate(results):
        emp = emp_map.get(row["_id"])
        emp_name = emp.get("full_name", row["_id"]) if emp else row["_id"]
        emp_designation = emp.get("designation", "Field Agent") if emp else "Field Agent"

        # Sum KM for the week from location pings
        ping_query = {
            "employee_id": row["_id"],
            "recorded_at": {"$gte": week_start, "$lt": week_end}
        }
        if org_id:
            ping_query["organization_id"] = org_id
        pings = await location_pings_collection.find(ping_query).sort("recorded_at", 1).to_list(length=5000)

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


