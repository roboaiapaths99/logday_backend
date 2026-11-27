from upload_security import SecureMediaRoute
from permissions import require_feature, require_any_feature, require_admin_session
from auth import issue_access_token, ensure_active_account
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

@router.post("/admin/import-employees")
async def admin_import_employees(file: bytes = File(...), current_admin: Admin = Depends(require_feature("employees"))):
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


@router.get("/admin/employees/import-template")
async def get_import_template(current_admin: Admin = Depends(require_feature("employees"))):
    """Download a CSV template for employee import."""
    csv_content = "full_name,email,employee_id,password,designation,department,employee_type,manager_email,beat_zone_name\n"
    csv_content += "John Doe,john@example.com,EMP001,,Sales Executive,Sales,field,manager@example.com,North Zone\n"
    csv_content += "Jane Smith,jane@example.com,EMP002,Secret@321,Developer,Engineering,desk,,\n"
    
    return StreamingResponse(
        io.BytesIO(csv_content.encode()),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=employee_import_template.csv"}
    )


@router.get("/admin/export-logs-pdf")
async def admin_export_logs_pdf(current_admin: Admin = Depends(require_feature("attendance"))):
    """Generate a PDF report of all attendance logs for the admin's organization/team scope."""
    try:
        org_id = current_admin.organization_id
        if not org_id and not _is_superadmin(current_admin):
            raise HTTPException(status_code=403, detail="Organization context required")

        # Enforce both tenant and manager team scope
        org_employees = await employees_collection.find(get_employee_filter(current_admin), {"_id": 1}).to_list(None)
        org_emp_ids = [str(emp["_id"]) for emp in org_employees]

        cursor = attendance_logs_collection.find({"user_id": {"$in": org_emp_ids}}).sort("timestamp", -1)
        logs = await cursor.to_list(length=5000)

        def _build_pdf():
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
            return buffer

        buffer = await run_in_threadpool(_build_pdf)
        return StreamingResponse(
            buffer,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=attendance_report_{datetime.now(timezone.utc).astimezone(timezone(timedelta(minutes=330))).strftime('%Y%m%d')}.pdf"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"PDF export error: {e}")
        raise HTTPException(status_code=500, detail=f"PDF generation failed: {str(e)}")


@router.get("/admin/export-logs-excel")
async def admin_export_logs_excel(current_admin: Admin = Depends(require_feature("attendance"))):
    """Export attendance logs to Excel for the admin's organization/team scope."""
    try:
        org_id = current_admin.organization_id
        if not org_id and not _is_superadmin(current_admin):
            raise HTTPException(status_code=403, detail="Organization context required")

        # Enforce both tenant and manager team scope
        org_employees = await employees_collection.find(get_employee_filter(current_admin), {"_id": 1}).to_list(None)
        org_emp_ids = [str(emp["_id"]) for emp in org_employees]

        logs = await attendance_logs_collection.find({"user_id": {"$in": org_emp_ids}}).sort("timestamp", -1).to_list(5000)

        headers_list = ["Employee", "Email", "Time", "Type", "Late", "Late Mins", "Address", "WiFi", "Distance (m)", "Duration (h)"]
        rows_data = []
        for log in logs:
            ts = log.get("timestamp")
            time_str = (ts.astimezone(timezone(timedelta(minutes=330))).strftime("%Y-%m-%d %H:%M:%S") + " (IST)") if isinstance(ts, datetime) else (str(ts) if ts else "N/A")
            rows_data.append({
                "Employee": log.get("full_name") or log.get("email") or "",
                "Email": log.get("email") or "",
                "Time": time_str,
                "Type": str(log.get("type") or "check-in").title(),
                "Late": "Yes" if log.get("is_late") else "No",
                "Late Mins": log.get("late_mins", 0),
                "Address": log.get("address") or "",
                "WiFi": log.get("wifi_ssid") or "",
                "Distance (m)": round(float(log.get("distance_meters") or 0), 1),
                "Duration (h)": log.get("duration_hours", 0),
            })

        def _build_excel():
            buffer = io.BytesIO()
            if pd is not None:
                try:
                    df = pd.DataFrame(rows_data, columns=headers_list)
                    with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
                        df.to_excel(writer, index=False, sheet_name='Attendance Logs')
                    buffer.seek(0)
                    return buffer
                except ImportError:
                    buffer = io.BytesIO()

            # Zero-dependency standards-compliant XLSX fallback so export never fails even without openpyxl
            from xml.sax.saxutils import escape
            def _cell_xml(val):
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    return f"<c><v>{val}</v></c>"
                clean = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(val if val is not None else ""))
                return f'<c t="inlineStr"><is><t xml:space="preserve">{escape(clean)}</t></is></c>'

            matrix = [headers_list] + [[r[col] for col in headers_list] for r in rows_data]
            sheet_xml = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            for r_idx, r_vals in enumerate(matrix, 1):
                sheet_xml += f'<row r="{r_idx}">' + "".join(_cell_xml(v) for v in r_vals) + "</row>"
            sheet_xml += "</sheetData></worksheet>"
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
                zf.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
                zf.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Attendance Logs" sheetId="1" r:id="rId1"/></sheets></workbook>')
                zf.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
                zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
            buffer.seek(0)
            return buffer

        buffer = await run_in_threadpool(_build_excel)
        return StreamingResponse(
            buffer,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=attendance_logs_{datetime.now(timezone.utc).astimezone(timezone(timedelta(minutes=330))).strftime('%Y%m%d')}.xlsx"}
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Excel export error: {e}")
        raise HTTPException(status_code=500, detail=f"Excel export failed: {str(e)}")


@router.post("/admin/login")
async def admin_login(req: AdminLoginRequest, request: Request):
    """Database-driven admin authentication with brute-force protection & non-blocking Argon2."""
    clean_email = req.email.strip().lower()
    check_login_rate_limit(request, clean_email)
    # 1. Check database for admin
    admin = await admins_collection.find_one({"email": clean_email})
    
    # 2. Verify password
    if admin and await verify_password_async(req.password, admin.get("hashed_password")):
        clear_failed_logins(request, clean_email)
        ensure_active_account(admin)
        token = await issue_access_token(admin, "admin")

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
    
    record_failed_login(request, clean_email)
    raise HTTPException(status_code=401, detail="Invalid admin credentials")


@router.post("/admin/register-organization")
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
        hashed_password = await get_password_hash_async(req.admin_password)
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


@router.get("/admin/sub-admins")
async def list_sub_admins(current_admin: Admin = Depends(require_feature("sub_admins"))):
    """List all admins for the organization (Owner/Superadmin only)."""
    if current_admin.role not in ["owner", "superadmin", "admin"]:
        raise HTTPException(status_code=403, detail="Only organization owners/admins can manage the admin team.")
    
    query = {} if _is_superadmin(current_admin) else {"organization_id": current_admin.organization_id}
    cursor = admins_collection.find(query, {"hashed_password": 0})
    admins = await cursor.to_list(length=100)
    for a in admins:
        a["_id"] = str(a["_id"])
    return admins


@router.post("/admin/sub-admins")
async def create_sub_admin(req: SubAdminCreate, current_admin: Admin = Depends(require_feature("sub_admins"))):
    """Create a new sub-admin for the organization (Owner/Superadmin only)."""
    if current_admin.role not in ["owner", "superadmin", "admin"]:
        logger.warning(f"Access Denied: Admin {current_admin.email} with role {current_admin.role} tried to manage sub-admins.")
        raise HTTPException(status_code=403, detail="Only organization owners/admins can add new admins.")
    
    if not _is_superadmin(current_admin) and req.role in ["superadmin", "owner"]:
        raise HTTPException(403, "Only a superadmin can assign this role")

    clean_email = req.email.strip().lower()
    # Check if admin already exists
    existing = await admins_collection.find_one({"email": clean_email})
    if existing:
        raise HTTPException(status_code=400, detail="Admin email already registered.")
    
    hashed_password = await get_password_hash_async(req.password)
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


@router.delete("/admin/sub-admins/{email}")
async def delete_sub_admin(email: str, current_admin: Admin = Depends(require_feature("sub_admins"))):
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


@router.get("/admin/me")
async def get_admin_me(current_admin: Admin = Depends(require_admin_session)):
    """Return current admin's profile including role and allowed features."""
    admin_data = {
        "email": current_admin.email,
        "full_name": current_admin.full_name,
        "role": current_admin.role,
        "organization_id": current_admin.organization_id,
    }
    # owner, superadmin, and admin get all features by default
    if current_admin.role in ["owner", "superadmin", "admin"]:
        admin_data["allowed_features"] = [
            "dashboard", "employees", "attendance", "leaves", "expenses", "reports",
            "announcements", "war_room", "territory", "nudge", "leaderboard", "flyfield",
            "onboarding", "exit_management", "payroll", "document_verification",
            "wfh_management", "wfh_monitoring", "sub_admins", "settings"
        ]
    else:
        admin_data["allowed_features"] = current_admin.allowed_features or []
    return admin_data


@router.put("/admin/sub-admins/{email}/permissions")
async def update_sub_admin_permissions(email: str, req: dict, current_admin: Admin = Depends(require_feature("sub_admins"))):
    """Update feature-level permissions for a sub-admin (Owner/Superadmin only)."""
    if current_admin.role not in ["owner", "superadmin", "admin"]:
        raise HTTPException(status_code=403, detail="Only organization owners/admins can update permissions.")
    
    allowed_features = req.get("allowed_features", [])
    role = req.get("role")  # optionally update role too
    
    from permissions import ALL_FEATURE_IDS
    if not isinstance(allowed_features, list) or any(f not in ALL_FEATURE_IDS for f in allowed_features):
        raise HTTPException(422, "Invalid feature list")
    if role and role not in {r.value for r in AdminRole}:
        raise HTTPException(422, "Invalid admin role")
    if not _is_superadmin(current_admin):
        if role in ["superadmin", "owner"]:
            raise HTTPException(403, "Only a superadmin can assign this role")
        target = await admins_collection.find_one({"email": email, "organization_id": current_admin.organization_id})
        if not target or target.get("role") in ["owner", "superadmin"]:
            raise HTTPException(403, "Cannot modify this account")

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


@router.get("/admin/employees")
async def admin_list_employees(current_admin: Admin = Depends(require_feature("employees"))):
    """List all employees for management (Role Scoped)."""
    filter_query = get_employee_filter(current_admin)
    cursor = employees_collection.find(filter_query, {"hashed_password": 0, "face_embedding": 0})
    employees = await cursor.to_list(length=1000)
    for emp in employees:
        emp["_id"] = str(emp["_id"])
    return employees


@router.put("/admin/employees/{email}")
async def admin_update_employee(email: str, req: EmployeeUpdate, current_admin: Admin = Depends(require_feature("employees"))):
    """Update employee details."""
    update_data = {k: v for k, v in req.model_dump().items() if v is not None}
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


@router.get("/admin/hrms/stats")
async def get_hrms_stats(current_admin: Admin = Depends(require_feature("dashboard"))):
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


@router.post("/admin/employees/bulk-update-type")

async def admin_bulk_update_employee_type(req: dict, current_admin: Admin = Depends(require_feature("employees"))):
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


@router.delete("/admin/employees/{email}")
async def admin_delete_employee(email: str, current_admin: Admin = Depends(require_feature("employees"))):
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


@router.get("/admin/logs")
async def admin_all_logs(
    limit: int = 100, 
    month: Optional[str] = None, 
    current_admin: Admin = Depends(require_feature("attendance"))
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


@router.post("/admin/employees")
async def admin_create_employee(req: RegisterRequest, current_admin: Admin = Depends(require_feature("employees"))):
    """Manually register a new employee (Admin)."""
    clean_email = req.email.strip().lower()
    # Check if employee already exists (globally unique email)
    existing = await employees_collection.find_one({"email": clean_email})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    # Embedding is optional for manual registration if they will enroll later
    embedding = None
    if req.face_image:
        from services.inference import run_inference
        embedding = await run_inference(get_face_embedding, req.face_image)

    hashed_password = await get_password_hash_async(req.password)
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
        "field_mode": getattr(req, "field_mode", "planned") or "planned",
        "organization_id": req.organization_id if (req.organization_id and _is_superadmin(current_admin)) else current_admin.organization_id,  # Superadmin can target any org
        "force_password_change": True,  # Employee must set their own password on first desk login
        "status": "Active"
    }

    await employees_collection.insert_one(employee_dict)
    logger.info(f"Employee {clean_email} created by admin {current_admin.email} with force_password_change=True")
    return {"message": f"Employee {req.full_name} registered successfully"}


@router.post("/admin/employees/{email}/reset-password")
async def admin_reset_password(email: str, req: dict, current_admin: Admin = Depends(require_feature("employees"))):
    """Reset an employee's password."""
    from urllib.parse import unquote
    clean_email = unquote(email).strip().lower()
    logger.info(f"Password reset requested for '{clean_email}' by admin '{current_admin.email}' (role={current_admin.role}, org={current_admin.organization_id})")
    
    # Only a system superadmin can reset an employee in another organization
    query = {"email": clean_email}
    is_privileged = current_admin.role == "superadmin"
    
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
    
    hashed_password = await get_password_hash_async(new_password)
    result = await employees_collection.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "hashed_password": hashed_password,
            "force_password_change": True  # Force them to change on next login
        }, "$inc": {"auth_version": 1}}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Employee not found")
    from database import auth_sessions_collection
    await auth_sessions_collection.update_many(
        {"account_id": str(user["_id"]), "account_type": "employee"},
        {"$set": {"revoked": True}}
    )
    
    logger.info(f"Password reset successful for '{clean_email}'")
    return {"message": "Password reset successfully"}


@router.post("/admin/employees/{email}/clear-binding")
async def admin_clear_binding(email: str, current_admin: Admin = Depends(require_feature("employees"))):
    """Clear hardware binding for an employee."""
    from urllib.parse import unquote
    clean_email = unquote(email).strip().lower()
    
    # Only a system superadmin can clear another organization's binding
    query = {"email": clean_email}
    is_privileged = current_admin.role == "superadmin"
    
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


@router.post("/admin/employees/bulk-assign-manager")
async def bulk_assign_manager(req: dict, current_admin: Admin = Depends(require_feature("employees"))):
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


@router.get("/admin/settings")
async def get_settings(current_admin: Admin = Depends(require_feature("settings"))):
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
            return default_settings.model_dump()
        
        # Merge stored data into the model to handle defaults and types safely
        stored_data = {k: v for k, v in settings.items() if k not in ["_id", "organization_id", "id"]}
        
        # Combine default values with stored values, ignoring None or invalid types in stored_data
        settings_dict = default_settings.model_dump()
        for k, v in stored_data.items():
            if k in settings_dict and v is not None:
                settings_dict[k] = v
                
        return settings_dict
    except Exception as e:
        logger.error(f"Error fetching settings: {e}")
        return SystemSettings().model_dump()


@router.put("/admin/settings")
async def update_settings(req: SystemSettings, current_admin: Admin = Depends(require_feature("settings"))):
    """Update organization-specific configuration and branding."""
    org_id = current_admin.organization_id
    if not org_id:
        raise HTTPException(status_code=403, detail="Organization linkage required for settings update.")

    try:
        update_dict = req.model_dump()
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


@router.post("/admin/upload-logo")
async def admin_upload_logo(request: Request, file: bytes = File(...), current_admin: Admin = Depends(require_feature("settings"))):
    """Upload organization logo image."""
    try:
        org_id = current_admin.organization_id
        if not org_id:
            raise HTTPException(status_code=403, detail="Organization context required")

        # Generate unique filename inside public logos folder
        file_extension = "png"
        logos_dir = os.path.join(UPLOAD_DIR, "logos")
        os.makedirs(logos_dir, exist_ok=True)
        filename = f"logo_{org_id}_{uuid.uuid4().hex[:8]}.{file_extension}"
        file_path = os.path.join(logos_dir, filename)

        def _write_logo():
            with open(file_path, "wb") as buffer:
                buffer.write(file)
        await run_in_threadpool(_write_logo)

        # Generate proxy-aware public URL (never hardcoding localhost:8001 in production)
        configured_base = (os.getenv("PUBLIC_API_URL") or os.getenv("API_BASE_URL") or "").strip().rstrip("/")
        if configured_base:
            logo_url = f"{configured_base}/uploads/logos/{filename}"
        else:
            logo_url = build_static_upload_url(request, f"/uploads/logos/{filename}")

        return {"logo_url": logo_url}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Logo upload failed: {e}")
        raise HTTPException(status_code=500, detail=f"Logo upload failed: {str(e)}")


@router.get("/admin/stats")
async def get_admin_stats(admin: dict = Depends(require_feature("dashboard"))):
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
        # Only count leaves for my team (support both employee_id and employee_email fields)
        team_emails = [e.get("email") for e in org_employees if e.get("email")]
        leave_query["$or"] = [
            {"employee_id": {"$in": team_emails}},
            {"employee_email": {"$in": team_emails}}
        ]

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


@router.get("/admin/stats/attendance-chart")
async def get_attendance_chart(admin: dict = Depends(require_feature("dashboard"))):
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


@router.get("/admin/live-feed")
async def get_admin_live_feed(skip: int = 0, limit: int = 50, admin: dict = Depends(require_feature("attendance"))):
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


@router.post("/admin/employees/bulk-update")
async def admin_bulk_update_employees(req: dict, admin=Depends(require_feature("employees"))):
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


