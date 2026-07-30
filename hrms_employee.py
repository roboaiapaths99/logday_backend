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

@router.get("/onboarding")
async def get_my_onboarding(employee=Depends(get_current_employee)):
    """Fetch the logged-in employee's onboarding checklist, progress, and document list."""
    onb = await onboardings_collection.find_one({
        "employee_email": employee["email"],
        "organization_id": employee["organization_id"]
    })
    if not onb:
        # Fallback: check by email clean
        onb = await onboardings_collection.find_one({
            "employee_email": employee["email"].strip().lower()
        })
        if not onb:
            raise HTTPException(status_code=404, detail="No active onboarding process found for your account.")
            
    onb["_id"] = str(onb["_id"])
    return onb


@router.post("/onboarding/upload")
async def upload_onboarding_document(
    doc_type: str = Form(...),
    file: UploadFile = File(...),
    employee=Depends(get_current_employee)
):
    """Upload an onboarding document file."""
    onb = await onboardings_collection.find_one({
        "employee_email": employee["email"],
        "organization_id": employee["organization_id"]
    })
    if not onb:
        raise HTTPException(status_code=404, detail="Onboarding process not found.")

    if doc_type not in onb.get("documents_required", []):
        raise HTTPException(status_code=400, detail=f"Document type '{doc_type}' is not in the required documents list.")

    # Save file
    file_ext = os.path.splitext(file.filename)[1]
    filename = f"{employee['email'].replace('@', '_')}_{doc_type}{file_ext}"
    filepath = os.path.join(ONBOARDING_UPLOAD_DIR, filename)

    with open(filepath, "wb") as f:
        f.write(await file.read())

    file_url = f"/uploads/onboarding/{filename}"

    # Auto-verify the document
    from hrms_onboarding import auto_verify_document_file
    is_verified, verification_msg, extracted_details = await auto_verify_document_file(filepath, doc_type, employee)

    # Update onboarding document submissions list
    docs_submitted = onb.get("documents_submitted", [])
    found = False
    for doc in docs_submitted:
        if doc["type"] == doc_type:
            doc["verified"] = is_verified
            doc["submitted_at"] = datetime.now(timezone.utc).isoformat()
            doc["file_url"] = file_url
            doc["extracted_details"] = extracted_details
            doc["verification_message"] = verification_msg
            found = True
            break
            
    if not found:
        docs_submitted.append({
            "type": doc_type,
            "verified": is_verified,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "file_url": file_url,
            "extracted_details": extracted_details,
            "verification_message": verification_msg
        })

    await onboardings_collection.update_one(
        {"_id": onb["_id"]},
        {
            "$set": {
                "documents_submitted": docs_submitted,
                "updated_at": datetime.now(timezone.utc)
            }
        }
    )

    # Recalculate progress
    from hrms_onboarding import recalculate_progress
    await recalculate_progress(onb["_id"])

    updated_onb = await onboardings_collection.find_one({"_id": onb["_id"]})
    updated_onb["_id"] = str(updated_onb["_id"])
    return {
        "success": True,
        "file_url": file_url,
        "onboarding": updated_onb
    }


@router.put("/onboarding/tasks/{task_id}/complete")
async def complete_onboarding_task(
    task_id: str,
    req: dict = {},
    employee=Depends(get_current_employee)
):
    """Complete an employee-assigned onboarding task (e.g. policy acknowledgment)."""
    onb = await onboardings_collection.find_one({
        "employee_email": employee["email"],
        "organization_id": employee["organization_id"]
    })
    if not onb:
        raise HTTPException(status_code=404, detail="Onboarding process not found.")

    tasks = onb.get("tasks", [])
    task_found = False
    for t in tasks:
        if t["task_id"] == task_id:
            task_found = True
            t["status"] = OnboardingTaskStatus.COMPLETED
            t["completed_at"] = datetime.now(timezone.utc)
            t["completed_by"] = employee["email"]
            t["notes"] = req.get("notes") or "Completed by employee."
            break

    if not task_found:
        raise HTTPException(status_code=404, detail="Task not found in your onboarding checklist.")

    await onboardings_collection.update_one(
        {"_id": onb["_id"]},
        {
            "$set": {
                "tasks": tasks,
                "updated_at": datetime.now(timezone.utc)
            }
        }
    )

    # Recalculate progress
    from hrms_onboarding import recalculate_progress
    await recalculate_progress(onb["_id"])

    updated_onb = await onboardings_collection.find_one({"_id": onb["_id"]})
    updated_onb["_id"] = str(updated_onb["_id"])
    return updated_onb


# --- PAYROLL & PAYSLIPS ENDPOINTS ---

@router.get("/salary-structure")
async def get_salary_structure(employee=Depends(get_current_employee)):
    """Fetch current salary structure details."""
    salary = employee.get("salary_structure")
    if not salary:
        raise HTTPException(status_code=404, detail="Salary structure not configured yet. Please contact HR.")
    return salary


@router.get("/payslips")
async def get_my_payslips(employee=Depends(get_current_employee)):
    """List all processed monthly payslips (LOCKED or PAID status)."""
    cursor = payrolls_collection.find({
        "employee_email": employee["email"],
        "organization_id": employee["organization_id"],
        "status": {"$in": ["locked", "paid"]}
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

    p = await payrolls_collection.find_one({
        "_id": obj_id,
        "employee_email": employee["email"],
        "organization_id": employee["organization_id"]
    })
    if not p:
        raise HTTPException(status_code=404, detail="Payslip record not found.")

    # PDF Generation
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=40, leftMargin=40, topMargin=40, bottomMargin=40)
    elements = []

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'PayslipTitle',
        parent=styles['Heading1'],
        fontSize=20,
        textColor=colors.HexColor("#4f46e5"),
        spaceAfter=15
    )
    section_title_style = ParagraphStyle(
        'SectionTitle',
        parent=styles['Heading2'],
        fontSize=12,
        textColor=colors.HexColor("#1e293b"),
        spaceBefore=12,
        spaceAfter=6
    )
    normal_style = styles['Normal']

    # Header / Title
    elements.append(Paragraph("LogDay Enterprise Payslip", title_style))
    elements.append(Paragraph(f"Pay Period: {p['payroll_month']}", section_title_style))
    elements.append(Spacer(1, 10))

    # Employee Details Table
    emp_details = [
        [Paragraph(f"<b>Employee Name:</b> {p['employee_name']}", normal_style), Paragraph(f"<b>Employee ID:</b> {p['employee_id']}", normal_style)],
        [Paragraph(f"<b>Department:</b> {p['department']}", normal_style), Paragraph(f"<b>Designation:</b> {p['designation']}", normal_style)],
        [Paragraph(f"<b>Bank Account:</b> {p.get('bank_account') or 'N/A'}", normal_style), Paragraph(f"<b>IFSC Code:</b> {p.get('bank_ifsc') or 'N/A'}", normal_style)],
        [Paragraph(f"<b>PAN Number:</b> {p.get('pan_number') or 'N/A'}", normal_style), Paragraph(f"<b>Working Days:</b> {p.get('working_days', 0)} (Pres: {p.get('present_days', 0)}, LOP: {p.get('absent_days', 0)})", normal_style)]
    ]
    t_details = Table(emp_details, colWidths=[260, 260])
    t_details.setStyle(TableStyle([
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#e2e8f0")),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
    ]))
    elements.append(t_details)
    elements.append(Spacer(1, 15))

    # Earnings & Deductions Tables side-by-side or stacked
    earnings_data = [
        [Paragraph("<b>Earnings</b>", normal_style), Paragraph("<b>Amount (INR)</b>", normal_style)],
        [Paragraph("Basic Salary", normal_style), f"{p.get('basic_salary', 0.0):,.2f}"],
        [Paragraph("HRA", normal_style), f"{p.get('hra', 0.0):,.2f}"],
        [Paragraph("Special Allowance", normal_style), f"{p.get('special_allowance', 0.0):,.2f}"],
        [Paragraph("Overtime / OT Payout", normal_style), f"{p.get('overtime_amount', 0.0):,.2f}"],
        [Paragraph("Performance Bonus", normal_style), f"{p.get('bonus', 0.0):,.2f}"],
        [Paragraph("Other Earnings", normal_style), f"{p.get('other_earnings', 0.0):,.2f}"],
        [Paragraph("<b>Gross Earnings</b>", normal_style), f"<b>{p.get('gross_salary', 0.0):,.2f}</b>"]
    ]

    deductions_data = [
        [Paragraph("<b>Deductions</b>", normal_style), Paragraph("<b>Amount (INR)</b>", normal_style)],
        [Paragraph("Provident Fund (PF)", normal_style), f"{p.get('pf_employee', 0.0):,.2f}"],
        [Paragraph("Employee State Ins (ESI)", normal_style), f"{p.get('esi_employee', 0.0):,.2f}"],
        [Paragraph("Professional Tax (PT)", normal_style), f"{p.get('professional_tax', 0.0):,.2f}"],
        [Paragraph("Income Tax / TDS", normal_style), f"{p.get('income_tax_tds', 0.0):,.2f}"],
        [Paragraph("Loan Recovery Deductions", normal_style), f"{p.get('loan_deductions', 0.0):,.2f}"],
        [Paragraph("Salary Advance Recovery", normal_style), f"{p.get('advance_deductions', 0.0):,.2f}"],
        [Paragraph("Other Deductions (LOP, etc.)", normal_style), f"{p.get('lop_deduction', 0.0):,.2f}"],
        [Paragraph("<b>Total Deductions</b>", normal_style), f"<b>{p.get('total_deductions', 0.0):,.2f}</b>"]
    ]

    # Combine tables
    salary_breakdown = [
        [Table(earnings_data, colWidths=[150, 100]), Table(deductions_data, colWidths=[150, 100])]
    ]
    t_breakdown = Table(salary_breakdown, colWidths=[260, 260])
    t_breakdown.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    
    # Styles for inner tables
    breakdown_table_style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#f8fafc")),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#f1f5f9")),
        ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
    ])
    salary_breakdown[0][0].setStyle(breakdown_table_style)
    salary_breakdown[0][1].setStyle(breakdown_table_style)

    elements.append(t_breakdown)
    elements.append(Spacer(1, 15))

    travel_reimb = p.get('travel_reimbursement', 0.0)
    exp_claims = p.get('expense_claims', 0.0)
    if travel_reimb > 0 or exp_claims > 0:
        reimb_rows = [
            [Paragraph("<b>Reimbursements (Non-Taxable)</b>", normal_style), Paragraph("<b>Amount (INR)</b>", normal_style)]
        ]
        if travel_reimb > 0:
            reimb_rows.append([Paragraph("KM Travel Reimbursement", normal_style), f"{travel_reimb:,.2f}"])
        if exp_claims > 0:
            reimb_rows.append([Paragraph("Approved Expense Claims", normal_style), f"{exp_claims:,.2f}"])
            
        t_reimb = Table(reimb_rows, colWidths=[370, 150])
        t_reimb.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#f8fafc")),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#f1f5f9")),
            ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
        ]))
        elements.append(t_reimb)
        elements.append(Spacer(1, 15))

    # Net Salary Banner
    net_data = [
        [Paragraph("<font size=12 color=white><b>NET SALARY PAYOUT (INR)</b></font>", normal_style), 
         Paragraph(f"<font size=14 color=white><b>Rs. {p.get('net_salary', 0.0):,.2f}</b></font>", normal_style)]
    ]
    t_net = Table(net_data, colWidths=[260, 260])
    t_net.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#4f46e5")),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
    ]))
    elements.append(t_net)

    doc.build(elements)
    buffer.seek(0)
    
    return StreamingResponse(
        buffer,
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

    proposed_lwd = req.get("proposed_lwd")
    exit_reason = req.get("exit_reason", "resignation")
    notes = req.get("notes") or ""

    if not proposed_lwd:
        raise HTTPException(status_code=400, detail="Proposed Last Working Day (proposed_lwd) is required.")

    # Validate proposed_lwd format (YYYY-MM-DD)
    try:
        datetime.strptime(proposed_lwd, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format for Proposed Last Working Day. Expected YYYY-MM-DD")

    # Create exit document
    exit_doc = {
        "organization_id": employee["organization_id"],
        "employee_email": employee["email"],
        "employee_name": employee["full_name"],
        "employee_id": employee.get("employee_id", "EMP"),
        "department": employee.get("department", "General"),
        "designation": employee.get("designation", "Employee"),
        "exit_reason": exit_reason,
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
            "processed": False,
            "processed_at": None
        },
        "documents_issued": [],
        "created_at": datetime.now(timezone.utc),
        "created_by": employee["email"]
    }

    result = await exit_managements_collection.insert_one(exit_doc)
    exit_doc["_id"] = str(result.inserted_id)
    return exit_doc


@router.post("/exit/interview")
async def submit_exit_interview(req: dict, employee=Depends(get_current_employee)):
    """Record exit interview questionnaire details by the employee."""
    exit_info = await exit_managements_collection.find_one({
        "employee_email": employee["email"],
        "organization_id": employee["organization_id"],
        "status": {"$in": [ExitStatus.PENDING, ExitStatus.IN_PROGRESS]}
    })
    if not exit_info:
        raise HTTPException(status_code=404, detail="No active exit process found.")

    interview_data = {
        "conducted": True,
        "interviewer": req.get("interviewer") or "HR Portal",
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "reason_for_leaving": req.get("reason_for_leaving") or "Other",
        "feedback": req.get("feedback") or "",
        "would_rejoin": req.get("would_rejoin") or "maybe",
        "improvement_suggestions": req.get("improvement_suggestions") or ""
    }

    await exit_managements_collection.update_one(
        {"_id": exit_info["_id"]},
        {"$set": {
            "exit_interview": interview_data,
            "clearances.hr.items.0.done": True, # Automatically tick Exit Interview as done in HR Clearance Checklist
            "updated_at": datetime.now(timezone.utc)
        }}
    )

    # Recalculate progress/checklists status if needed (conducted in admin backend but nice to update local clearance check)
    updated_exit = await exit_managements_collection.find_one({"_id": exit_info["_id"]})
    updated_exit["_id"] = str(updated_exit["_id"])
    return updated_exit
