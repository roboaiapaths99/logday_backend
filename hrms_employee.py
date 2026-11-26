import os
import uuid
import io
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from bson import ObjectId

from database import (
    onboardings_collection, employees_collection,
    payrolls_collection, exit_managements_collection
)
from auth import get_current_employee
from hrms_models import OnboardingStatus, OnboardingTaskStatus, ExitStatus, ClearanceStatus

# ReportLab imports for PDF generation
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

router = APIRouter(prefix="/api/employee", tags=["HRMS Employee"])

UPLOAD_DIR = "uploads"
ONBOARDING_UPLOAD_DIR = os.path.join(UPLOAD_DIR, "onboarding")
if not os.path.exists(ONBOARDING_UPLOAD_DIR):
    os.makedirs(ONBOARDING_UPLOAD_DIR, exist_ok=True)


# --- ONBOARDING ENDPOINTS ---
# Note: Employee onboarding endpoints (/api/employee/onboarding, /api/employee/onboarding/upload,
# /api/employee/onboarding/tasks/{task_id}/complete) are served by employee_onboarding_router in hrms_onboarding.py.



# --- PAYROLL & PAYSLIPS ENDPOINTS ---

@router.get("/salary-structure")
async def get_salary_structure(employee=Depends(get_current_employee)):
    """Fetch current salary structure details, falling back to _resolve_employee_salary_structure or latest payroll."""
    from hrms_payroll import _resolve_employee_salary_structure
    clean_email = (employee.get("email") or "").strip().lower()
    sal = await _resolve_employee_salary_structure(employee)
    if not sal.get("ctc"):
        latest_p = await payrolls_collection.find_one(
            {"employee_email": clean_email},
            sort=[("payroll_month", -1)]
        )
        if latest_p and latest_p.get("ctc"):
            sal["ctc"] = float(latest_p.get("ctc", 0.0))
            sal["monthly_salary"] = float(latest_p.get("monthly_salary") or round(sal["ctc"] / 12.0, 2))
            sal["basic_salary"] = float(latest_p.get("basic_salary") or round(sal["monthly_salary"] * 0.40, 2))
            sal["hra"] = float(latest_p.get("hra") or round(sal["basic_salary"] * 0.50, 2))
            sal["special_allowance"] = float(latest_p.get("special_allowance") or max(0.0, round(sal["monthly_salary"] - sal["basic_salary"] - sal["hra"], 2)))
    if "_id" in sal:
        sal["_id"] = str(sal["_id"])
    return sal


@router.get("/payslips")
async def get_my_payslips(employee=Depends(get_current_employee)):
    """List all processed monthly payslips (APPROVED, LOCKED, or PAID status)."""
    clean_email = (employee.get("email") or "").strip().lower()
    cursor = payrolls_collection.find({
        "employee_email": {"$in": [clean_email, employee.get("email", "")]},
        "status": {"$in": ["locked", "paid", "approved", "processed"]}
    }).sort("payroll_month", -1)
    
    payrolls = await cursor.to_list(length=100)
    for p in payrolls:
        p["_id"] = str(p["_id"])
    return payrolls


@router.get("/payslips/{id}/download")
async def download_payslip_pdf(id: str, employee=Depends(get_current_employee)):
    """Generate and stream the PDF version of a payslip."""
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")

    clean_email = (employee.get("email") or "").strip().lower()
    p = await payrolls_collection.find_one({
        "_id": obj_id,
        "employee_email": {"$in": [clean_email, employee.get("email", "")]}
    })
    if not p:
        raise HTTPException(status_code=404, detail="Payslip record not found.")

    from hrms_payroll import generate_payslip_pdf_bytes, _attach_company_logo_to_payroll
    from fastapi.concurrency import run_in_threadpool
    await _attach_company_logo_to_payroll(p)
    pdf_bytes = await run_in_threadpool(generate_payslip_pdf_bytes, p)
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=payslip_{p['payroll_month']}.pdf"}
    )


# --- EXIT MANAGEMENT ENDPOINTS ---

@router.get("/exit/clearance")
async def get_my_exit_clearance(employee=Depends(get_current_employee)):
    """Fetch notice details and live clearance checklist for resigning employees."""
    exit_info = await exit_managements_collection.find_one({
        "employee_email": employee["email"],
        "organization_id": employee["organization_id"],
        "status": {"$in": [ExitStatus.PENDING, ExitStatus.IN_PROGRESS, ExitStatus.COMPLETED]}
    })
    if not exit_info:
        raise HTTPException(status_code=404, detail="No exit clearance flow active for this account.")
        
    exit_info["_id"] = str(exit_info["_id"])
    return exit_info


@router.post("/exit/resign")
async def file_resignation(req: dict, employee=Depends(get_current_employee)):
    """Submit a formal resignation, initiating clearances."""
    # Check if there is already an active exit clearance
    existing = await exit_managements_collection.find_one({
        "employee_email": employee["email"],
        "organization_id": employee["organization_id"],
        "status": {"$in": [ExitStatus.PENDING, ExitStatus.IN_PROGRESS]}
    })
    if existing:
        raise HTTPException(status_code=400, detail="You have already submitted a resignation request that is currently active.")

    proposed_lwd = req.get("proposed_lwd") or req.get("last_working_day")
    exit_reason = req.get("exit_reason") or req.get("reason") or "resignation"
    notes = req.get("notes") or req.get("handover_notes") or ""

    if not proposed_lwd:
        raise HTTPException(status_code=400, detail="Proposed Last Working Day (proposed_lwd) is required.")

    # Validate proposed_lwd format (YYYY-MM-DD)
    try:
        datetime.strptime(proposed_lwd, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format for Proposed Last Working Day. Expected YYYY-MM-DD")

    now_iso = datetime.now(timezone.utc).isoformat()
    # Create exit document
    exit_doc = {
        "organization_id": employee["organization_id"],
        "employee_email": employee["email"].strip().lower(),
        "employee_name": employee.get("full_name") or employee.get("name", "Employee"),
        "employee_id": employee.get("employee_id") or str(employee.get("_id", "EMP")),
        "department": employee.get("department", "General"),
        "designation": employee.get("designation", "Employee"),
        "joining_date": employee.get("date_of_joining") or employee.get("joining_date") or "N/A",
        "exit_reason": exit_reason,
        "reason": exit_reason,
        "handover_notes": notes,
        "resignation_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "last_working_day": proposed_lwd,
        "notice_period_days": 30,
        "notice_served_days": 30,
        "status": ExitStatus.PENDING,
        "progress": 0.0,
        "assigned_to": "", # HR Admin assignee
        "clearances": {
            "hr": {
                "status": ClearanceStatus.PENDING,
                "items": [
                    {"item": "Exit interview conducted", "done": False},
                    {"item": "ID card returned", "done": False},
                    {"item": "Policy acknowledgments signed", "done": False}
                ],
                "cleared_by": None,
                "cleared_at": None,
                "notes": ""
            },
            "it": {
                "status": ClearanceStatus.PENDING,
                "items": [
                    {"item": "Laptop returned", "done": False},
                    {"item": "Email access revoked", "done": False},
                    {"item": "VPN access revoked", "done": False}
                ],
                "cleared_by": None,
                "cleared_at": None,
                "notes": ""
            },
            "finance": {
                "status": ClearanceStatus.PENDING,
                "items": [
                    {"item": "Pending reimbursements settled", "done": False},
                    {"item": "Salary dues cleared", "done": False},
                    {"item": "Loan recovery completed", "done": False}
                ],
                "cleared_by": None,
                "cleared_at": None,
                "notes": ""
            },
            "assets": {
                "status": ClearanceStatus.PENDING,
                "items": [
                    {"item": "Access card returned", "done": False},
                    {"item": "Company phone returned", "done": False}
                ],
                "cleared_by": None,
                "cleared_at": None,
                "notes": ""
            }
        },
        "exit_interview": {
            "conducted": False,
            "interviewer": None,
            "date": None,
            "reason_for_leaving": None,
            "feedback": None,
            "would_rejoin": None,
            "improvement_suggestions": None
        },
        "final_settlement": {
            "calculated": False,
            "salary_dues": 0.0,
            "leave_encashment": 0.0,
            "bonus_due": 0.0,
            "gratuity": 0.0,
            "notice_period_recovery": 0.0,
            "other_deductions": 0.0,
            "total_payable": 0.0,
            "gross_payable": 0.0,
            "deductions": 0.0,
            "net_payable": 0.0,
            "processed": False,
            "processed_at": None
        },
        "documents_issued": [],
        "created_at": now_iso,
        "updated_at": now_iso,
        "created_by": employee["email"]
    }

    result = await exit_managements_collection.insert_one(exit_doc)
    exit_doc["_id"] = str(result.inserted_id)
    return exit_doc
