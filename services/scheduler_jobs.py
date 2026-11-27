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
    tasks_collection,
    scheduler_locks_collection,
)
from pymongo import ReturnDocument
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
from attendance_service import classify_attendance_day

logger = logging.getLogger("main")
APP_ENV = os.getenv("APP_ENV", "development")
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from services.common_helpers import (
    trigger_alert, delete_file_from_storage
)

scheduler = AsyncIOScheduler()


async def acquire_scheduler_lock(job_id: str, ttl_seconds: int = 240) -> bool:
    """
    Distributed MongoDB lock so scheduled background jobs run at most once per interval
    even when multiple Gunicorn/Uvicorn web workers are running concurrently.
    """
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=max(10, ttl_seconds))
    owner_pid = f"{os.uname().nodename if hasattr(os, 'uname') else 'host'}:{os.getpid()}"
    try:
        existing = await scheduler_locks_collection.find_one({"_id": job_id})
        if existing:
            exp = existing.get("expires_at")
            if isinstance(exp, datetime):
                exp_utc = exp.replace(tzinfo=timezone.utc) if exp.tzinfo is None else exp.astimezone(timezone.utc)
                if exp_utc > now:
                    return False
            claimed = await scheduler_locks_collection.find_one_and_update(
                {"_id": job_id, "expires_at": existing.get("expires_at")},
                {"$set": {"expires_at": expires_at, "locked_at": now, "owner": owner_pid}},
                return_document=ReturnDocument.AFTER,
            )
            return claimed is not None

        await scheduler_locks_collection.insert_one({
            "_id": job_id,
            "expires_at": expires_at,
            "locked_at": now,
            "owner": owner_pid,
        })
        return True
    except Exception:
        return False


async def check_missed_visits(force: bool = False):
    """
    Scheduled job to check for missed visits.
    A visit is considered missed if it was approved for today (in local org time, default IST +5:30)
    but not checked in by 5:30 PM local time (12:00 PM UTC).
    """
    try:
        if not force and not await acquire_scheduler_lock("check_missed_visits", ttl_seconds=1800):
            return
        now = datetime.now(timezone.utc)
        local_now = now + timedelta(minutes=330)
        if not force and local_now.hour < 17:
            return

        logger.info("Running scheduled job: check_missed_visits")
        today_str = local_now.strftime("%Y-%m-%d")
        local_day_start = datetime(local_now.year, local_now.month, local_now.day, 0, 0, 0, tzinfo=timezone.utc)
        day_start = local_day_start - timedelta(minutes=330)

        approved_plans_cursor = visit_plans_collection.find({
            "date": today_str,
            "status": {"$in": [PlanStatus.APPROVED, "approved"]}
        })

        async for plan in approved_plans_cursor:
            employee_id = plan["employee_id"]
            organization_id = plan["organization_id"]
            missed_stops = []

            for idx, stop in enumerate(plan.get("stops", [])):
                if stop.get("status") in ["completed", "visited", "checked_in"]:
                    continue
                stop_id = stop.get("sequence_order", idx + 1)
                existing_log = await visit_logs_collection.find_one({
                    "employee_id": employee_id,
                    "date": today_str,
                    "$or": [
                        {"visit_plan_stop_id": stop_id},
                        {"visit_id": str(plan["_id"]), "sequence_order": stop_id}
                    ]
                })
                if not existing_log:
                    missed_stops.append(stop)

            if missed_stops:
                existing_alert = await alerts_collection.find_one({
                    "employee_id": employee_id,
                    "type": "Productivity",
                    "detail": {"$regex": "SLA Breach|Missed visit"},
                    "timestamp": {"$gte": day_start}
                })
                if not existing_alert:
                    detail = f"SLA Breach: {len(missed_stops)} planned visit(s) missed for today."
                    await trigger_alert(
                        "Productivity",
                        employee_id,
                        organization_id,
                        detail,
                        "medium",
                        {"plan_id": str(plan["_id"]), "missed_count": len(missed_stops)}
                    )
                    logger.warning(detail)
        logger.info("Finished scheduled job: check_missed_visits")
    except Exception as e:
        logger.error(f"SLA Check failed: {e}")


async def purge_old_screenshots():
    """Daily clean-up of expired screenshots and aged location pings from DB and local storage."""
    if not await acquire_scheduler_lock("purge_old_screenshots", ttl_seconds=3600):
        return
    logger.info("Starting scheduled job: purge_old_screenshots")
    try:
        orgs = await organizations_collection.find({}).to_list(length=1000)
        purged_count = 0
        now = datetime.now(timezone.utc)
        
        for org in orgs:
            org_id = str(org["_id"])
            policy = org.get("wfh_policy", {}) or {}
            retention_days = int(policy.get("screenshot_retention_days", 5) or 5)
            
            cutoff_date = now - timedelta(days=retention_days)
            query = {
                "organization_id": org_id,
                "timestamp": {"$lt": cutoff_date}
            }
            
            # Process in batches of 500 until all expired screenshots for this org are purged
            for _ in range(20):
                expired_screens = await wfh_screenshots_collection.find(query).to_list(length=500)
                if not expired_screens:
                    break
                batch_deleted = 0
                for screen in expired_screens:
                    image_url = screen.get("image_url", "")
                    if await delete_file_from_storage(image_url):
                        await wfh_screenshots_collection.delete_one({"_id": screen["_id"]})
                        purged_count += 1
                        batch_deleted += 1
                if not batch_deleted:
                    break

        # Purge location pings older than LOCATION_PING_RETENTION_DAYS (default 90 days) to prevent unbounded DB growth
        ping_retention_days = int(os.getenv("LOCATION_PING_RETENTION_DAYS", "90") or 90)
        ping_cutoff = now - timedelta(days=max(7, ping_retention_days))
        ping_del = await location_pings_collection.delete_many({"recorded_at": {"$lt": ping_cutoff}})
        purged_pings = getattr(ping_del, "deleted_count", 0)

        logger.info(f"Finished scheduled job: purge_old_screenshots. Purged {purged_count} screenshots and {purged_pings} old location pings.")
    except Exception as e:
        logger.error(f"Error running purge_old_screenshots: {e}")


