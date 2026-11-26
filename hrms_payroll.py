import base64
import calendar
import io
import json
import os
import smtplib
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from typing import Any, List, Optional, Tuple
from fastapi import APIRouter, Body, Depends, HTTPException, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from bson import ObjectId
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, Image as RLImage
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

from database import (
    payrolls_collection, employees_collection,
    attendance_logs_collection, leave_requests_collection,
    km_reimbursements_collection, expense_claims_collection,
    holidays_collection, leave_policies_collection, settings_collection,
    salary_structures_collection, wfh_alerts_collection, organizations_collection
)
from models import Admin
from auth import get_current_admin
from hrms_models import PayrollStatus, PayrollCreate, PayrollRunRequest, DaysOffSaveRequest
from permissions import require_feature
from attendance_service import classify_attendance_day

router = APIRouter(
    prefix="/admin/payroll",
    tags=["HRMS Payroll"],
    dependencies=[Depends(require_feature("payroll"))]
)

async def fetch_approved_claims_and_reimbursements(employee_email: str, org_id: str, payroll_month: str):
    """Fetch approved KM travel claims and expense claims for an employee in a month."""
    # KM claims match
    km_cursor = km_reimbursements_collection.find({
        "employee_id": employee_email,
        "organization_id": org_id,
        "status": "approved",
        "date": {"$regex": f"^{payroll_month}"}
    })
    km_claims = await km_cursor.to_list(length=100)
    total_km_reimbursement = sum(float(c.get("total_amount", 0.0)) for c in km_claims)

    # Expense claims match
    year, month = map(int, payroll_month.split("-"))
    start_dt = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end_dt = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end_dt = datetime(year, month + 1, 1, tzinfo=timezone.utc)
        
    expense_cursor = expense_claims_collection.find({
        "employee_id": employee_email,
        "organization_id": org_id,
        "status": "approved",
        "created_at": {"$gte": start_dt, "$lt": end_dt}
    })
    expense_claims = await expense_cursor.to_list(length=100)
    total_expense_claims = sum(float(c.get("amount", 0.0)) for c in expense_claims)

    return total_km_reimbursement, total_expense_claims

def get_org_filter(admin: Admin):
    if admin.role == "superadmin" or admin.organization_id == "system_org":
        return {}
    return {"organization_id": admin.organization_id}

def count_working_days(year: int, month: int) -> int:
    """Calculate working days in a month (excluding Sundays)."""
    num_days = calendar.monthrange(year, month)[1]
    working_days = 0
    for day in range(1, num_days + 1):
        dt = datetime(year, month, day)
        # 6 is Sunday in Python's datetime.weekday() (Monday is 0, Sunday is 6)
        if dt.weekday() != 6:
            working_days += 1
    return working_days

def calculate_payslip_fields(ctc: float, working_days: int, present_days: float,
                             paid_leave_days: float = 0.0, unpaid_leave_days: float = 0.0,
                             short_leave_penalty_days: float = 0.0, leave_days: Optional[float] = None,
                             overtime_hours: float = 0.0, bonus: float = 0.0, other_earnings: float = 0.0,
                             reimbursements: float = 0.0,
                             loan_deductions: float = 0.0, advance_deductions: float = 0.0, other_deductions: float = 0.0,
                             salary_structure: dict = None,
                             absent_days_override: Optional[float] = None):
    """Core enterprise salary engine with Loss of Pay (LOP) integration."""
    monthly_salary = ctc / 12.0
    
    if salary_structure and salary_structure.get("basic_salary"):
        basic_salary = float(salary_structure["basic_salary"])
    else:
        basic_salary = monthly_salary * 0.40  # Default 40%
        
    if salary_structure and salary_structure.get("hra"):
        hra = float(salary_structure["hra"])
    else:
        hra = basic_salary * 0.50  # Default 50% of basic
        
    special_allowance = monthly_salary - basic_salary - hra
    if special_allowance < 0:
        special_allowance = 0.0
        
    if leave_days is not None and paid_leave_days == 0.0 and unpaid_leave_days == 0.0:
        paid_leave_days = float(leave_days)

    # Calculate unexcused absent days
    if absent_days_override is not None:
        absent_days = max(0.0, float(absent_days_override))
    else:
        absent_days = max(0.0, float(working_days) - float(present_days) - float(paid_leave_days) - float(unpaid_leave_days))
    
    # Total Loss of Pay (LOP) Days = Unexcused Absents + Unpaid Leaves + Short Leave penalties
    lop_days = round(absent_days + float(unpaid_leave_days) + float(short_leave_penalty_days), 2)
    
    # Loss of Pay deduction
    daily_wage = monthly_salary / max(1, working_days)
    lop_deduction = daily_wage * lop_days
    
    # Net attendance salary
    attendance_salary = max(0.0, monthly_salary - lop_deduction)
        
    # Overtime (hourly rate x 1.5)
    hourly_rate = daily_wage / 8.0
    overtime_amount = overtime_hours * hourly_rate * 1.5
    
    # Gross salary (Taxable)
    gross_salary = attendance_salary + overtime_amount + bonus + other_earnings
    
    # Statutory deductions flags
    pf_enabled = salary_structure.get("pf_enabled", True) if salary_structure else True
    esi_enabled = salary_structure.get("esi_enabled", False) if salary_structure else False
    pt_enabled = salary_structure.get("professional_tax_enabled", True) if salary_structure else True
    
    # PF: 12% of basic capped at 1800
    if pf_enabled:
        pf_employee = min(basic_salary * 0.12, 1800.0)
        pf_employer = pf_employee
    else:
        pf_employee = 0.0
        pf_employer = 0.0
        
    # ESI: 0.75% of gross if gross <= 21000
    if esi_enabled and gross_salary <= 21000.0:
        esi_employee = gross_salary * 0.0075
        esi_employer = gross_salary * 0.0325
    else:
        esi_employee = 0.0
        esi_employer = 0.0
        
    # Professional Tax
    professional_tax = 0.0
    if pt_enabled:
        if gross_salary <= 10000.0:
            professional_tax = 0.0
        elif gross_salary <= 20000.0:
            professional_tax = 200.0
        elif gross_salary <= 30000.0:
            professional_tax = 400.0
        else:
            professional_tax = 600.0
            
    # Income Tax TDS (Indian Slabs)
    annual_gross = gross_salary * 12.0
    annual_pf = pf_employee * 12.0
    taxable_income = max(0.0, annual_gross - annual_pf - 50000.0)  # less 50k standard deduction
    
    annual_tax = 0.0
    if taxable_income <= 250000.0:
        annual_tax = 0.0
    elif taxable_income <= 500000.0:
        annual_tax = (taxable_income - 250000.0) * 0.05
    elif taxable_income <= 1000000.0:
        annual_tax = 12500.0 + (taxable_income - 500000.0) * 0.20
    else:
        annual_tax = 112500.0 + (taxable_income - 1000000.0) * 0.30
        
    monthly_tds = annual_tax / 12.0
    
    total_deductions = pf_employee + esi_employee + professional_tax + monthly_tds + loan_deductions + advance_deductions + other_deductions
    net_salary = max(0.0, gross_salary - total_deductions + reimbursements)
        
    return {
        "ctc": ctc,
        "monthly_salary": round(monthly_salary, 2),
        "basic_salary": round(basic_salary, 2),
        "hra": round(hra, 2),
        "special_allowance": round(special_allowance, 2),
        "daily_wage": round(daily_wage, 2),
        "absent_days": round(absent_days, 2),
        "paid_leave_days": round(float(paid_leave_days), 2),
        "unpaid_leave_days": round(float(unpaid_leave_days), 2),
        "short_leave_penalty_days": round(float(short_leave_penalty_days), 2),
        "lop_days": round(lop_days, 2),
        "lop_deduction": round(lop_deduction, 2),
        "attendance_salary": round(attendance_salary, 2),
        "overtime_amount": round(overtime_amount, 2),
        "gross_salary": round(gross_salary, 2),
        "pf_employee": round(pf_employee, 2),
        "pf_employer": round(pf_employer, 2),
        "esi_employee": round(esi_employee, 2),
        "esi_employer": round(esi_employer, 2),
        "professional_tax": round(professional_tax, 2),
        "income_tax_tds": round(monthly_tds, 2),
        "total_deductions": round(total_deductions, 2),
        "net_salary": round(net_salary, 2)
    }

async def fetch_attendance_data(employee_email: str, org_id: str, payroll_month: str):
    """Fetch working days, present days, paid leaves, unpaid leaves (LOP), and short-leave penalties for the month."""
    emp = await employees_collection.find_one({"email": employee_email, "organization_id": org_id})
    if not emp:
        return 0, 0.0, 0.0, 0.0, 0.0, 0.0
        
    emp_id = str(emp["_id"])
    year, month = map(int, payroll_month.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    
    # 1. Total working days is full calendar days (since we pay for holidays/weekends)
    working_days = num_days
    
    # Fetch configured days off (holidays/weekends)
    days_off_doc = await holidays_collection.find_one({
        "organization_id": org_id,
        "payroll_month": payroll_month,
        "days_off": {"$exists": True}
    })
    if not days_off_doc:
        days_off_doc = await holidays_collection.find_one({
            "organization_id": "system_org",
            "payroll_month": payroll_month,
            "days_off": {"$exists": True}
        })
    
    if days_off_doc:
        days_off = set(days_off_doc.get("days_off", []))
    else:
        days_off = set()

    # Also load any named holidays directly from holidays_collection
    try:
        named_h = await holidays_collection.find({
            "$or": [
                {"organization_id": org_id, "payroll_month": payroll_month, "date": {"$exists": True}},
                {"organization_id": "system_org", "payroll_month": payroll_month, "date": {"$exists": True}}
            ]
        }).to_list(length=100)
        for nh in named_h:
            if "day" in nh and nh["day"] is not None:
                days_off.add(int(nh["day"]))
            elif "date" in nh and nh["date"]:
                try:
                    days_off.add(int(str(nh["date"]).split("-")[2]))
                except Exception:
                    pass
    except Exception:
        pass

    for day in range(1, num_days + 1):
        dt = datetime(year, month, day)
        if dt.weekday() == 6:  # Sunday
            days_off.add(day)

    # Fetch Leave Policy & System Settings first so we know timezone_offset
    policy = await leave_policies_collection.find_one({
        "$or": [
            {"organization_id": org_id, "year": year},
            {"organization_id": "system_org", "year": year},
            {"organization_id": org_id},
            {"organization_id": "system_org"}
        ]
    })
    org_settings = await settings_collection.find_one({
        "$or": [
            {"organization_id": org_id},
            {"organization_id": "system_org"}
        ]
    })
    tz_offset = int((org_settings or {}).get("timezone_offset", 330))

    # Local month boundaries [00:00 on 1st local .. 00:00 on next month 1st local) converted to UTC
    local_start_dt = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        local_end_dt = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        local_end_dt = datetime(year, month + 1, 1, tzinfo=timezone.utc)
    start_dt = local_start_dt - timedelta(minutes=tz_offset)
    end_dt = local_end_dt - timedelta(minutes=tz_offset)

    paid_leave_types = ["sick", "casual", "on_duty", "duty"]
    short_leave_monthly_limit = 2
    short_leave_penalty_rule = "half_day_per_2_excess"
    if policy:
        paid_leave_types = policy.get("paid_leave_types", paid_leave_types)
        short_leave_monthly_limit = int(policy.get("short_leave_monthly_limit", 2))
        short_leave_penalty_rule = policy.get("short_leave_penalty_rule", "half_day_per_2_excess")

    # classify_attendance_day imported from attendance_service

    # 2. Approved leave days (with duration and paid status)
    emp_identifiers = [employee_email.strip().lower(), employee_email.strip(), emp_id]
    if "employee_id" in emp and emp["employee_id"]:
        emp_identifiers.append(str(emp["employee_id"]).strip())

    cursor_leaves = leave_requests_collection.find({
        "$or": [
            {"employee_id": {"$in": emp_identifiers}},
            {"email": {"$in": emp_identifiers}}
        ],
        "status": {"$regex": "^approved$", "$options": "i"}
    })
    leaves = await cursor_leaves.to_list(length=200)

    paid_leave_days = 0.0
    unpaid_leave_days = 0.0
    calendar_leave_by_day = {}
    start_month_str = local_start_dt.strftime("%Y-%m-%d")
    end_month_str = (local_end_dt - timedelta(days=1)).strftime("%Y-%m-%d")

    # Map approved leaves by date YYYY-MM-DD
    leaves_by_date = {}
    short_leave_days_set = set()

    for l in leaves:
        l_start = str(l.get("start_date", ""))
        l_end = str(l.get("end_date", l_start))
        if not l_start:
            continue
            
        if l_start <= end_month_str and l_end >= start_month_str:
            dur_type = str(l.get("duration_type", "")).lower().strip()
            if l.get("duration") is not None:
                dur = float(l["duration"])
            elif dur_type in ["first_half", "second_half", "half_day"]:
                dur = 0.5
            elif dur_type == "short_leave":
                dur = 0.0
            else:
                cur_dt = max(datetime.strptime(l_start[:10], "%Y-%m-%d"), datetime.strptime(start_month_str, "%Y-%m-%d"))
                end_dt_loop = min(datetime.strptime(l_end[:10], "%Y-%m-%d"), datetime.strptime(end_month_str, "%Y-%m-%d"))
                dur = 0.0
                while cur_dt <= end_dt_loop:
                    if cur_dt.day not in days_off:
                        dur += 1.0
                    cur_dt += timedelta(days=1)

            l_type = str(l.get("leave_type", "other")).lower().strip()
            if l.get("is_paid") is not None:
                is_p = bool(l.get("is_paid"))
            else:
                is_p = any(pt in l_type for pt in paid_leave_types)

            # Map to individual days for attendance classifier lookup & covered calendar leave days
            cur_dt = max(datetime.strptime(l_start[:10], "%Y-%m-%d"), datetime.strptime(start_month_str, "%Y-%m-%d"))
            end_dt_loop = min(datetime.strptime(l_end[:10], "%Y-%m-%d"), datetime.strptime(end_month_str, "%Y-%m-%d"))
            while cur_dt <= end_dt_loop:
                d_str = cur_dt.strftime("%Y-%m-%d")
                # Prefer keeping paid or non-penalty leave in leaves_by_date if multiple exist on same date
                if d_str not in leaves_by_date or is_p:
                    leaves_by_date[d_str] = l
                if dur_type == "short_leave":
                    short_leave_days_set.add(cur_dt.day)
                elif cur_dt.day not in days_off and "on_duty" not in l_type and "duty" not in l_type:
                    cov_val = 0.5 if dur_type in ["first_half", "second_half", "half_day"] else 1.0
                    calendar_leave_by_day[cur_dt.day] = min(1.0, calendar_leave_by_day.get(cur_dt.day, 0.0) + cov_val)
                cur_dt += timedelta(days=1)

            if is_p and "on_duty" not in l_type and "duty" not in l_type:
                paid_leave_days += dur
            elif not is_p and "on_duty" not in l_type and "duty" not in l_type:
                unpaid_leave_days += dur

    # 3. Fetch Attendance Logs to evaluate daily hours and presence
    cursor_logs = attendance_logs_collection.find({
        "$or": [
            {"user_id": {"$in": emp_identifiers}},
            {"email": {"$in": emp_identifiers}}
        ],
        "timestamp": {"$gte": start_dt, "$lt": end_dt}
    }).sort("timestamp", 1)
    logs = await cursor_logs.to_list(length=1000)

    daily_logs = {}
    for log in logs:
        ts = log["timestamp"]
        if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
        local_ts = ts + timedelta(minutes=tz_offset)
        if local_ts.year != year or local_ts.month != month:
            continue
        d_num = local_ts.day
        if d_num not in daily_logs:
            daily_logs[d_num] = []
        daily_logs[d_num].append(log)

    check_ins_presence = 0.0

    for d_num, d_logs in daily_logs.items():
        if d_num in days_off:
            continue
        
        checkins = [l for l in d_logs if l.get("type") in ["check-in", "IN"]]
        checkouts = [l for l in d_logs if l.get("type") in ["check-out", "OUT"]]
        
        if not checkins:
            continue
            
        first_in = checkins[0]["timestamp"]
        if first_in.tzinfo is None: first_in = first_in.replace(tzinfo=timezone.utc)
        first_in_local = first_in + timedelta(minutes=tz_offset)

        last_out = checkouts[-1]["timestamp"] if checkouts else None
        if last_out and last_out.tzinfo is None: last_out = last_out.replace(tzinfo=timezone.utc)
        last_out_local = (last_out + timedelta(minutes=tz_offset)) if last_out else None

        # Calculate paired working session duration
        duration_sec = 0.0
        last_in_ts = None
        for l in d_logs:
            l_ts = l["timestamp"]
            if l_ts.tzinfo is None: l_ts = l_ts.replace(tzinfo=timezone.utc)
            l_type = l.get("type")
            if l_type in ["check-in", "IN"]:
                last_in_ts = l_ts
            elif l_type in ["check-out", "OUT"] and last_in_ts:
                duration_sec += max(0.0, (l_ts - last_in_ts).total_seconds())
                last_in_ts = None
        
        duration_hours = round(duration_sec / 3600.0, 2)
        if duration_hours == 0.0 and checkouts and checkins:
            duration_hours = round(max(0.0, (last_out - first_in).total_seconds()) / 3600.0, 2)

        day_date_str = f"{year:04d}-{month:02d}-{d_num:02d}"
        matching_leave = leaves_by_date.get(day_date_str)

        classification = classify_attendance_day(
            first_checkin_local=first_in_local,
            last_checkout_local=last_out_local,
            worked_hours=duration_hours,
            settings=org_settings or {},
            employee_type=emp.get("employee_type", "desk"),
            approved_leave=matching_leave
        )

        check_ins_presence += classification.get("effective_days", 1.0)
        if classification.get("is_short_leave"):
            short_leave_days_set.add(d_num)

    present_days = check_ins_presence + len(days_off)
    short_leaves_count = len(short_leave_days_set)

    short_leave_penalty_days = 0.0
    if short_leaves_count > short_leave_monthly_limit:
        excess_short = short_leaves_count - short_leave_monthly_limit
        if short_leave_penalty_rule in ["1_excess_quarter_day", "quarter_day_per_excess"]:
            short_leave_penalty_days = round(excess_short * 0.25, 2)
        elif short_leave_penalty_rule in ["1_excess_half_day", "half_day_per_excess"]:
            short_leave_penalty_days = round(excess_short * 0.50, 2)
        elif short_leave_penalty_rule in ["1_excess_full_day", "full_day_per_excess"]:
            short_leave_penalty_days = round(excess_short * 1.00, 2)
        elif short_leave_penalty_rule in ["full_day_per_2_excess", "2_excess_full_day"]:
            short_leave_penalty_days = round((excess_short // 2) * 1.0, 2)
        elif short_leave_penalty_rule in ["3_excess_full_day", "full_day_per_3_excess"]:
            short_leave_penalty_days = round((excess_short // 3) * 1.0, 2)
        elif short_leave_penalty_rule == "no_penalty":
            short_leave_penalty_days = 0.0
        else:  # half_day_per_2_excess / default
            short_leave_penalty_days = round((excess_short // 2) * 0.5, 2)

    covered_calendar_leave_days = sum(calendar_leave_by_day.values())

    actual_work_days = num_days - len(days_off)
    absent_days = max(0.0, float(actual_work_days) - float(check_ins_presence) - float(covered_calendar_leave_days))
    
    return working_days, present_days, paid_leave_days, unpaid_leave_days, absent_days, short_leave_penalty_days

async def _resolve_employee_salary_structure(emp: dict) -> dict:
    """Resolve salary_structure from employee record or salary_structures_collection, with safe defaults."""
    sal = emp.get("salary_structure")
    if isinstance(sal, dict) and sal.get("ctc") is not None:
        return sal
    sal_doc = await salary_structures_collection.find_one({"employee_email": emp["email"]})
    if sal_doc and sal_doc.get("ctc") is not None:
        return sal_doc
    # Check if top-level ctc or monthly_salary exists on employee doc
    raw_ctc = float(emp.get("ctc") or (float(emp.get("monthly_salary") or emp.get("salary") or 0.0) * 12.0))
    monthly = raw_ctc / 12.0
    basic = round(monthly * 0.40, 2)
    hra = round(basic * 0.50, 2)
    special = max(0.0, round(monthly - basic - hra, 2))
    return {
        "ctc": raw_ctc,
        "monthly_salary": round(monthly, 2),
        "basic_salary": basic,
        "hra": hra,
        "special_allowance": special,
        "pf_enabled": False if raw_ctc == 0 else True,
        "esi_enabled": False,
        "professional_tax_enabled": False if raw_ctc == 0 else True,
        "bank_name": (sal or {}).get("bank_name") or emp.get("bank_name"),
        "bank_account": (sal or {}).get("bank_account") or emp.get("bank_account"),
        "bank_ifsc": (sal or {}).get("bank_ifsc") or emp.get("bank_ifsc"),
        "pan_number": (sal or {}).get("pan_number") or emp.get("pan_number"),
    }


async def _auto_generate_missing_payrolls(payroll_month: str, current_admin: Admin, department: Optional[str] = None):
    """Ensure every active employee in the organization has an auto-calculated payroll record for payroll_month."""
    try:
        year, month = map(int, payroll_month.split("-"))
        if month < 1 or month > 12:
            return
    except Exception:
        return

    org_id = current_admin.organization_id
    emp_query: dict = {
        "$or": [
            {"status": {"$in": ["Active", "active", None]}},
            {"status": {"$exists": False}}
        ]
    }
    if not (current_admin.role == "superadmin" or org_id == "system_org"):
        emp_query["organization_id"] = org_id
    if department:
        emp_query["department"] = department

    employees = await employees_collection.find(emp_query).to_list(length=1000)
    if not employees:
        return

    existing_docs = await payrolls_collection.find({
        **get_org_filter(current_admin),
        "payroll_month": payroll_month
    }).to_list(length=1000)
    existing_by_email = {
        (d.get("employee_email") or "").strip().lower(): d
        for d in existing_docs
        if d.get("employee_email")
    }

    for emp in employees:
        emp_email = (emp.get("email") or "").strip().lower()
        if not emp_email:
            continue
        existing_rec = existing_by_email.get(emp_email)
        # Skip if already finalized/approved/sent or manually edited by admin
        if existing_rec:
            is_unmodified_draft = (
                existing_rec.get("status") == PayrollStatus.DRAFT
                and not existing_rec.get("email_sent")
                and not existing_rec.get("updated_by")
                and abs(float(existing_rec.get("admin_adjustment") or 0.0)) < 0.01
            )
            if not is_unmodified_draft:
                continue
        try:
            emp_org_id = emp.get("organization_id") or org_id or "system_org"
            w_days, p_days, paid_l, unpaid_l, a_days, short_pen = await fetch_attendance_data(emp_email, emp_org_id, payroll_month)
            salary_structure = await _resolve_employee_salary_structure(emp)
            travel_reimb, exp_claims = await fetch_approved_claims_and_reimbursements(emp_email, emp_org_id, payroll_month)
            reimbursements = travel_reimb + exp_claims

            bonus_val = float((existing_rec or {}).get("bonus") or 0.0)
            ot_hours_val = float((existing_rec or {}).get("overtime_hours") or 0.0)
            other_earn_val = float((existing_rec or {}).get("other_earnings") or 0.0)
            loan_ded_val = float((existing_rec or {}).get("loan_deductions") or 0.0)
            adv_ded_val = float((existing_rec or {}).get("advance_deductions") or 0.0)
            other_ded_val = float((existing_rec or {}).get("other_deductions") or 0.0)

            results = calculate_payslip_fields(
                ctc=float(salary_structure.get("ctc") or 0.0),
                working_days=w_days,
                present_days=p_days,
                paid_leave_days=paid_l,
                unpaid_leave_days=unpaid_l,
                short_leave_penalty_days=short_pen,
                overtime_hours=ot_hours_val,
                bonus=bonus_val,
                other_earnings=other_earn_val,
                reimbursements=reimbursements,
                loan_deductions=loan_ded_val,
                advance_deductions=adv_ded_val,
                other_deductions=other_ded_val,
                salary_structure=salary_structure,
                absent_days_override=a_days
            )

            if existing_rec:
                await payrolls_collection.update_one(
                    {"_id": existing_rec["_id"]},
                    {"$set": {
                        "working_days": w_days,
                        "present_days": p_days,
                        "leave_days": paid_l + unpaid_l,
                        "paid_leave_days": results["paid_leave_days"],
                        "unpaid_leave_days": results["unpaid_leave_days"],
                        "short_leave_penalty_days": results["short_leave_penalty_days"],
                        "absent_days": results["absent_days"],
                        "lop_days": results["lop_days"],
                        "ctc": results["ctc"],
                        "monthly_salary": results["monthly_salary"],
                        "basic_salary": results["basic_salary"],
                        "hra": results["hra"],
                        "special_allowance": results["special_allowance"],
                        "daily_wage": results["daily_wage"],
                        "lop_deduction": results["lop_deduction"],
                        "attendance_salary": results["attendance_salary"],
                        "travel_reimbursement": travel_reimb,
                        "expense_claims": exp_claims,
                        "reimbursements": reimbursements,
                        "gross_salary": results["gross_salary"],
                        "pf_employee": results["pf_employee"],
                        "pf_employer": results["pf_employer"],
                        "esi_employee": results["esi_employee"],
                        "esi_employer": results["esi_employer"],
                        "professional_tax": results["professional_tax"],
                        "income_tax_tds": results["income_tax_tds"],
                        "total_deductions": results["total_deductions"],
                        "calculated_net_salary": results["net_salary"],
                        "net_salary": results["net_salary"],
                    }}
                )
            else:
                payroll_doc = {
                    "organization_id": emp_org_id,
                    "employee_email": emp_email,
                    "employee_name": emp.get("full_name") or emp_email.split("@")[0],
                    "employee_id": emp.get("employee_id", "EMP"),
                    "department": emp.get("department", "General"),
                    "designation": emp.get("designation", "Employee"),
                    "payroll_month": payroll_month,
                    "working_days": w_days,
                    "present_days": p_days,
                    "leave_days": paid_l + unpaid_l,
                    "paid_leave_days": results["paid_leave_days"],
                    "unpaid_leave_days": results["unpaid_leave_days"],
                    "short_leave_penalty_days": results["short_leave_penalty_days"],
                    "absent_days": results["absent_days"],
                    "lop_days": results["lop_days"],
                    "ctc": results["ctc"],
                    "monthly_salary": results["monthly_salary"],
                    "basic_salary": results["basic_salary"],
                    "hra": results["hra"],
                    "special_allowance": results["special_allowance"],
                    "daily_wage": results["daily_wage"],
                    "lop_deduction": results["lop_deduction"],
                    "attendance_salary": results["attendance_salary"],
                    "overtime_hours": 0.0,
                    "overtime_amount": 0.0,
                    "bonus": 0.0,
                    "other_earnings": 0.0,
                    "travel_reimbursement": travel_reimb,
                    "expense_claims": exp_claims,
                    "reimbursements": reimbursements,
                    "gross_salary": results["gross_salary"],
                    "pf_employee": results["pf_employee"],
                    "pf_employer": results["pf_employer"],
                    "esi_employee": results["esi_employee"],
                    "esi_employer": results["esi_employer"],
                    "professional_tax": results["professional_tax"],
                    "income_tax_tds": results["income_tax_tds"],
                    "loan_deductions": 0.0,
                    "advance_deductions": 0.0,
                    "other_deductions": 0.0,
                    "total_deductions": results["total_deductions"],
                    "calculated_net_salary": results["net_salary"],
                    "admin_adjustment": 0.0,
                    "net_salary": results["net_salary"],
                    "status": PayrollStatus.DRAFT,
                    "email_sent": False,
                    "last_sent_at": None,
                    "sent_to_email": None,
                    "approved_by": None,
                    "approved_at": None,
                    "locked_by": None,
                    "locked_at": None,
                    "payment_date": None,
                    "payment_reference": None,
                    "bank_name": salary_structure.get("bank_name"),
                    "bank_account": salary_structure.get("bank_account"),
                    "bank_ifsc": salary_structure.get("bank_ifsc"),
                    "pan_number": salary_structure.get("pan_number"),
                    "created_at": datetime.now(timezone.utc),
                    "created_by": current_admin.email
                }
                await payrolls_collection.insert_one(payroll_doc)
        except Exception:
            continue


@router.get("")
async def list_payrolls(
    payroll_month: Optional[str] = None,
    department: Optional[str] = None,
    status: Optional[str] = None,
    auto_sync: bool = True,
    current_admin: Admin = Depends(get_current_admin)
):
    """List payroll records for a month and/or department. Automatically calculates missing employee drafts."""
    if payroll_month and auto_sync:
        await _auto_generate_missing_payrolls(payroll_month, current_admin, department)

    org_filter = get_org_filter(current_admin)
    query = {**org_filter}
    
    if payroll_month:
        query["payroll_month"] = payroll_month
    if department:
        query["department"] = department
    if status:
        query["status"] = status
        
    cursor = payrolls_collection.find(query).sort([("employee_name", 1)])
    payrolls = await cursor.to_list(length=1000)
    if payrolls:
        sample = await _attach_company_logo_to_payroll({"organization_id": current_admin.organization_id or payrolls[0].get("organization_id")})
        c_logo = sample.get("company_logo_url")
        c_name = sample.get("company_name")
        for p in payrolls:
            p["_id"] = str(p["_id"])
            if c_logo and not p.get("company_logo_url"):
                p["company_logo_url"] = c_logo
            if c_name and not p.get("company_name"):
                p["company_name"] = c_name
    return payrolls

@router.get("/stats")
async def get_payroll_stats(payroll_month: str, current_admin: Admin = Depends(get_current_admin)):
    """Summary of total gross, net, deductions, and processed counts for a month."""
    org_filter = get_org_filter(current_admin)
    
    pipeline = [
        {"$match": {**org_filter, "payroll_month": payroll_month}},
        {"$group": {
            "_id": None,
            "total_gross": {"$sum": "$gross_salary"},
            "total_net": {"$sum": "$net_salary"},
            "total_pf": {"$sum": "$pf_employee"},
            "total_tds": {"$sum": "$income_tax_tds"},
            "count": {"$sum": 1}
        }}
    ]
    
    results = await payrolls_collection.aggregate(pipeline).to_list(1)
    if not results:
        return {
            "total_gross": 0.0,
            "total_net": 0.0,
            "total_pf": 0.0,
            "total_tds": 0.0,
            "count": 0
        }
        
    r = results[0]
    return {
        "total_gross": round(r["total_gross"], 2),
        "total_net": round(r["total_net"], 2),
        "total_pf": round(r["total_pf"], 2),
        "total_tds": round(r["total_tds"], 2),
        "count": r["count"]
    }

@router.get("/days-off")
async def get_days_off(payroll_month: str, organization_id: Optional[str] = None, current_admin: Admin = Depends(get_current_admin)):
    """Fetch configured days off (non-working days) for the month."""
    org_id = current_admin.organization_id
    if (current_admin.role == "superadmin" or org_id == "system_org") and organization_id:
        org_id = organization_id
        
    doc = await holidays_collection.find_one({
        "organization_id": org_id,
        "payroll_month": payroll_month,
        "days_off": {"$exists": True}
    })
    if doc:
        return {"days_off": doc.get("days_off", [])}
    
    # Return default Sundays + any named holidays configured for this month
    year, month = map(int, payroll_month.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    days_off_set = set()
    for day in range(1, num_days + 1):
        dt = datetime(year, month, day)
        if dt.weekday() == 6: # Sunday
            days_off_set.add(day)

    try:
        named_h = await holidays_collection.find({
            "$or": [
                {"organization_id": org_id, "payroll_month": payroll_month, "date": {"$exists": True}},
                {"organization_id": "system_org", "payroll_month": payroll_month, "date": {"$exists": True}}
            ]
        }).to_list(length=100)
        for nh in named_h:
            if "day" in nh and nh["day"] is not None:
                days_off_set.add(int(nh["day"]))
            elif "date" in nh and nh["date"]:
                try:
                    days_off_set.add(int(str(nh["date"]).split("-")[2]))
                except Exception:
                    pass
    except Exception:
        pass

    return {"days_off": sorted(list(days_off_set))}

@router.post("/days-off")
async def save_days_off(req: DaysOffSaveRequest, current_admin: Admin = Depends(get_current_admin)):
    """Save custom days off configuration for the month without overwriting named holiday documents."""
    org_id = current_admin.organization_id
    if (current_admin.role == "superadmin" or org_id == "system_org") and req.organization_id:
        org_id = req.organization_id
        
    try:
        year, month = map(int, req.payroll_month.split("-"))
        num_days = calendar.monthrange(year, month)[1]
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid payroll month format. Expected YYYY-MM")
        
    for d in req.days_off:
        if d < 1 or d > num_days:
            raise HTTPException(status_code=400, detail=f"Invalid day {d} for month {req.payroll_month}")
            
    await holidays_collection.update_one(
        {"organization_id": org_id, "payroll_month": req.payroll_month, "days_off": {"$exists": True}},
        {"$set": {
            "organization_id": org_id,
            "payroll_month": req.payroll_month,
            "days_off": sorted(list(set(req.days_off))),
            "updated_at": datetime.now(timezone.utc)
        }},
        upsert=True
    )
    return {"message": "Days off saved successfully", "days_off": sorted(list(set(req.days_off)))}


MASKED_SECRET = "••••••••"
DEFAULT_SENDER_NAME = "LogDay Payroll & HR"


async def _get_org_smtp_config(org_id: str) -> dict:
    """Resolve email delivery settings (Brevo first, SMTP as legacy fallback) from DB or environment."""
    if not org_id or org_id == "system_org":
        settings_doc = await settings_collection.find_one({"$or": [{"organization_id": "system_org"}, {"id": "config"}]})
    else:
        settings_doc = await settings_collection.find_one({"organization_id": org_id})
        if not settings_doc:
            settings_doc = await settings_collection.find_one({"$or": [{"organization_id": "system_org"}, {"id": "config"}]})
    smtp_db = (settings_doc or {}).get("smtp_config") or {}

    # --- Brevo (primary) ---
    brevo_api_key = smtp_db.get("brevo_api_key") or os.getenv("BREVO_API_KEY", "")
    brevo_sender_email = (
        smtp_db.get("brevo_sender_email")
        or os.getenv("BREVO_SENDER_EMAIL")
        or smtp_db.get("smtp_from_email")
        or ""
    )
    brevo_sender_name = (
        smtp_db.get("brevo_sender_name")
        or os.getenv("BREVO_SENDER_NAME")
        or smtp_db.get("smtp_from_name")
        or DEFAULT_SENDER_NAME
    )
    brevo_ready = bool(brevo_api_key and brevo_sender_email)

    # --- Legacy SMTP (fallback, env or old DB config) ---
    host = smtp_db.get("smtp_host") or os.getenv("SMTP_HOST", "")
    port = int(smtp_db.get("smtp_port") or os.getenv("SMTP_PORT", "587"))
    user = smtp_db.get("smtp_user") or os.getenv("SMTP_USER", "")
    password = smtp_db.get("smtp_password") or os.getenv("SMTP_PASSWORD") or os.getenv("SMTP_PASS", "")
    from_email = smtp_db.get("smtp_from_email") or os.getenv("SMTP_FROM_EMAIL") or user
    from_name = smtp_db.get("smtp_from_name") or os.getenv("SMTP_FROM_NAME", DEFAULT_SENDER_NAME)
    smtp_ready = bool(host and user and password)

    provider = "brevo" if brevo_ready else ("smtp" if smtp_ready else "none")

    return {
        "provider": provider,
        "brevo_api_key": brevo_api_key,
        "brevo_sender_email": brevo_sender_email,
        "brevo_sender_name": brevo_sender_name,
        "smtp_host": host,
        "smtp_port": port,
        "smtp_user": user,
        "smtp_password": password,
        "smtp_from_email": from_email,
        "smtp_from_name": from_name,
        "configured": provider != "none",
    }


def _public_email_settings(cfg: dict) -> dict:
    """Email settings safe to return to the admin UI (no secrets)."""
    return {
        "provider": cfg["provider"],
        "brevo_sender_email": cfg["brevo_sender_email"],
        "brevo_sender_name": cfg["brevo_sender_name"],
        "has_brevo_api_key": bool(cfg["brevo_api_key"]),
        "configured": cfg["configured"],
    }


@router.get("/smtp-settings")
async def get_payroll_smtp_settings(current_admin: Admin = Depends(get_current_admin)):
    """Fetch current email (Brevo) configuration status for sending salary slips."""
    org_id = current_admin.organization_id or "system_org"
    cfg = await _get_org_smtp_config(org_id)
    return _public_email_settings(cfg)


@router.post("/smtp-settings")
async def save_payroll_smtp_settings(req: dict, current_admin: Admin = Depends(get_current_admin)):
    """Save Brevo email configuration for sending salary slips to employee login emails."""
    org_id = current_admin.organization_id or "system_org"
    if org_id == "system_org":
        settings_doc = await settings_collection.find_one({"$or": [{"organization_id": "system_org"}, {"id": "config"}]}) or {}
    else:
        settings_doc = await settings_collection.find_one({"organization_id": org_id}) or {}
    stored = settings_doc.get("smtp_config") or {}

    new_key = (req.get("brevo_api_key") or "").strip()
    if not new_key or new_key == MASKED_SECRET:
        new_key = stored.get("brevo_api_key", "")

    sender_email = (req.get("brevo_sender_email") or "").strip().lower()
    if not sender_email:
        raise HTTPException(status_code=400, detail="Sender email is required (must be a verified sender in Brevo).")
    if not new_key and not os.getenv("BREVO_API_KEY"):
        raise HTTPException(status_code=400, detail="Brevo API key is required.")

    updates = {
        "smtp_config.brevo_api_key": new_key,
        "smtp_config.brevo_sender_email": sender_email,
        "smtp_config.brevo_sender_name": (req.get("brevo_sender_name") or DEFAULT_SENDER_NAME).strip(),
        "smtp_config.updated_at": datetime.now(timezone.utc).isoformat(),
        "smtp_config.updated_by": current_admin.email,
    }
    if org_id == "system_org":
        await settings_collection.update_one(
            {"id": "config"} if settings_doc.get("id") == "config" else {"organization_id": "system_org"},
            {"$set": {**updates, "organization_id": "system_org"}},
            upsert=True,
        )
    else:
        await settings_collection.update_one({"organization_id": org_id}, {"$set": updates}, upsert=True)

    cfg = await _get_org_smtp_config(org_id)
    return {
        "status": "success",
        "message": "Brevo email settings saved successfully.",
        **_public_email_settings(cfg),
    }


@router.post("/smtp-settings/test")
async def test_payroll_email_settings(req: dict = Body(default={}), current_admin: Admin = Depends(get_current_admin)):
    """Send a test email via the configured provider (defaults to the logged-in admin's email)."""
    org_id = current_admin.organization_id or "system_org"
    cfg = await _get_org_smtp_config(org_id)
    if not cfg["configured"]:
        raise HTTPException(status_code=400, detail="Email is not configured. Save your Brevo API key and sender email first.")

    to_email = (req.get("to_email") or current_admin.email or "").strip().lower()
    if not to_email:
        raise HTTPException(status_code=400, detail="No recipient email for the test message.")

    html = (
        "<div style=\"font-family: Arial, sans-serif; font-size: 14px; color: #1e293b;\">"
        "<p>This is a test email from <b>LogDay Payroll</b>.</p>"
        "<p>If you received it, salary slips will be delivered to employees successfully.</p></div>"
    )
    ok, note = await run_in_threadpool(
        _send_email_sync, cfg, to_email, "LogDay Payroll — Test Email", html, None, None
    )
    if not ok:
        raise HTTPException(status_code=502, detail=note)
    return {"status": "success", "message": f"Test email sent to {to_email} via {cfg['provider'].upper()}."}


@router.get("/{id}")
async def get_payroll(id: str, current_admin: Admin = Depends(get_current_admin)):
    """Fetch details of a single employee's payroll record."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")
        
    p = await payrolls_collection.find_one({**org_filter, "_id": obj_id})
    if not p:
        raise HTTPException(status_code=404, detail="Payroll record not found")
        
    p["_id"] = str(p["_id"])
    await _attach_company_logo_to_payroll(p)
    return p

@router.post("/calculate")
async def calculate_employee_payroll(payload: PayrollCreate, current_admin: Admin = Depends(get_current_admin)):
    """Calculate and save/update a single DRAFT payroll record."""
    org_id = current_admin.organization_id
    email_clean = payload.employee_email.strip().lower()
    
    if current_admin.role == "superadmin" or org_id == "system_org":
        emp = await employees_collection.find_one({"email": email_clean})
        if not emp:
            raise HTTPException(status_code=404, detail="Employee not found")
        org_id = emp["organization_id"]
    else:
        emp = await employees_collection.find_one({"email": email_clean, "organization_id": org_id})
        if not emp:
            raise HTTPException(status_code=404, detail="Employee not found")
        
    salary_structure = emp.get("salary_structure")
    if not salary_structure:
        raise HTTPException(status_code=400, detail="Salary structure not configured for this employee. Configure CTC first.")
        
    # Auto-fetch attendance values if not manually supplied
    w_days = payload.working_days
    p_days = payload.present_days
    l_days = payload.leave_days
    paid_l_days = 0.0
    unpaid_l_days = 0.0
    short_penalty_days = 0.0
    fetched_a_override = None
    
    if w_days is None or p_days is None or l_days is None:
        fetched_w, fetched_p, fetched_paid_l, fetched_unpaid_l, fetched_a, fetched_short_pen = await fetch_attendance_data(email_clean, org_id, payload.payroll_month)
        w_days = w_days if w_days is not None else fetched_w
        p_days = p_days if p_days is not None else fetched_p
        paid_l_days = fetched_paid_l
        unpaid_l_days = fetched_unpaid_l
        short_penalty_days = fetched_short_pen
        fetched_a_override = fetched_a
        l_days = l_days if l_days is not None else (fetched_paid_l + fetched_unpaid_l)
    else:
        paid_l_days = float(l_days)

    # Fetch travel reimbursements and expenses
    travel_reimb, exp_claims = await fetch_approved_claims_and_reimbursements(email_clean, org_id, payload.payroll_month)
    reimbursements = travel_reimb + exp_claims

    # Run calculations
    results = calculate_payslip_fields(
        ctc=salary_structure.get("ctc", 0.0),
        working_days=w_days,
        present_days=p_days,
        paid_leave_days=paid_l_days,
        unpaid_leave_days=unpaid_l_days,
        short_leave_penalty_days=short_penalty_days,
        leave_days=l_days,
        overtime_hours=payload.overtime_hours,
        bonus=payload.bonus,
        other_earnings=payload.other_earnings,
        reimbursements=reimbursements,
        loan_deductions=payload.loan_deductions,
        advance_deductions=payload.advance_deductions,
        other_deductions=payload.other_deductions,
        salary_structure=salary_structure,
        absent_days_override=fetched_a_override
    )

    # Document details
    payroll_doc = {
        "organization_id": org_id,
        "employee_email": email_clean,
        "employee_name": emp["full_name"],
        "employee_id": emp.get("employee_id", "EMP"),
        "department": emp.get("department", "General"),
        "designation": emp.get("designation", "Employee"),
        "payroll_month": payload.payroll_month,
        
        "working_days": w_days,
        "present_days": p_days,
        "leave_days": l_days,
        "paid_leave_days": results["paid_leave_days"],
        "unpaid_leave_days": results["unpaid_leave_days"],
        "short_leave_penalty_days": results["short_leave_penalty_days"],
        "absent_days": results["absent_days"],
        "lop_days": results["lop_days"],
        
        "ctc": results["ctc"],
        "monthly_salary": results["monthly_salary"],
        "basic_salary": results["basic_salary"],
        "hra": results["hra"],
        "special_allowance": results["special_allowance"],
        "daily_wage": results["daily_wage"],
        
        "lop_deduction": results["lop_deduction"],
        "attendance_salary": results["attendance_salary"],
        "overtime_hours": payload.overtime_hours,
        "overtime_amount": results["overtime_amount"],
        "bonus": payload.bonus,
        "other_earnings": payload.other_earnings,
        "travel_reimbursement": travel_reimb,
        "expense_claims": exp_claims,
        "reimbursements": reimbursements,
        "gross_salary": results["gross_salary"],
        
        "pf_employee": results["pf_employee"],
        "pf_employer": results["pf_employer"],
        "esi_employee": results["esi_employee"],
        "esi_employer": results["esi_employer"],
        "professional_tax": results["professional_tax"],
        "income_tax_tds": results["income_tax_tds"],
        "loan_deductions": payload.loan_deductions,
        "advance_deductions": payload.advance_deductions,
        "other_deductions": payload.other_deductions,
        "total_deductions": results["total_deductions"],
        
        "net_salary": results["net_salary"],
        
        "status": PayrollStatus.DRAFT,
        "approved_by": None,
        "approved_at": None,
        "locked_by": None,
        "locked_at": None,
        "payment_date": None,
        "payment_reference": None,
        
        "bank_name": salary_structure.get("bank_name"),
        "bank_account": salary_structure.get("bank_account"),
        "bank_ifsc": salary_structure.get("bank_ifsc"),
        "pan_number": salary_structure.get("pan_number"),
        
        "created_at": datetime.now(timezone.utc),
        "created_by": current_admin.email
    }

    # Upsert: check if record already exists for this employee and month
    existing = await payrolls_collection.find_one({
        "employee_email": email_clean,
        "organization_id": org_id,
        "payroll_month": payload.payroll_month
    })
    
    if existing:
        if existing["status"] != PayrollStatus.DRAFT:
            raise HTTPException(status_code=400, detail="Cannot recalculate non-draft payroll record.")
        await payrolls_collection.replace_one({"_id": existing["_id"]}, payroll_doc)
        payroll_doc["_id"] = str(existing["_id"])
    else:
        res = await payrolls_collection.insert_one(payroll_doc)
        payroll_doc["_id"] = str(res.inserted_id)
        
    return payroll_doc

@router.post("/run-monthly")
async def run_monthly_payroll(payload: PayrollRunRequest, current_admin: Admin = Depends(get_current_admin)):
    """Run bulk payroll calculations for all active employees according to salary and attendance."""
    org_id = current_admin.organization_id
    
    # Query all active employees (including those whose salary structure will be initialized or updated)
    emp_query: dict = {
        "$or": [
            {"status": {"$in": ["Active", "active", None]}},
            {"status": {"$exists": False}}
        ]
    }
    if not (current_admin.role == "superadmin" or org_id == "system_org"):
        emp_query["organization_id"] = org_id
        
    if payload.department:
        emp_query["department"] = payload.department
    if payload.employee_emails:
        emp_query["email"] = {"$in": [e.strip().lower() for e in payload.employee_emails]}
        
    cursor = employees_collection.find(emp_query)
    employees = await cursor.to_list(length=1000)
    
    processed_count = 0
    errors = []
    
    for emp in employees:
        try:
            emp_org_id = emp.get("organization_id") or org_id or "system_org"
            emp_email = (emp.get("email") or "").strip().lower()
            if not emp_email:
                continue

            # Check if payroll already exists for this employee/month
            existing = await payrolls_collection.find_one({
                "employee_email": emp_email,
                "organization_id": emp_org_id,
                "payroll_month": payload.payroll_month
            })
            if existing and existing["status"] in [PayrollStatus.LOCKED, PayrollStatus.PAID]:
                continue  # Skip locked/paid records
                
            w_days, p_days, paid_l, unpaid_l, a_days, short_pen = await fetch_attendance_data(emp_email, emp_org_id, payload.payroll_month)
            salary_structure = await _resolve_employee_salary_structure(emp)
            
            # Fetch travel reimbursements and expenses
            travel_reimb, exp_claims = await fetch_approved_claims_and_reimbursements(emp_email, emp_org_id, payload.payroll_month)
            reimbursements = travel_reimb + exp_claims

            results = calculate_payslip_fields(
                ctc=float(salary_structure.get("ctc") or 0.0),
                working_days=w_days,
                present_days=p_days,
                paid_leave_days=paid_l,
                unpaid_leave_days=unpaid_l,
                short_leave_penalty_days=short_pen,
                other_earnings=0.0,
                reimbursements=reimbursements,
                salary_structure=salary_structure,
                absent_days_override=a_days
            )
            
            payroll_doc = {
                "organization_id": emp_org_id,
                "employee_email": emp_email,
                "employee_name": emp.get("full_name") or emp_email.split("@")[0],
                "employee_id": emp.get("employee_id", "EMP"),
                "department": emp.get("department", "General"),
                "designation": emp.get("designation", "Employee"),
                "payroll_month": payload.payroll_month,
                
                "working_days": w_days,
                "present_days": p_days,
                "leave_days": paid_l + unpaid_l,
                "paid_leave_days": results["paid_leave_days"],
                "unpaid_leave_days": results["unpaid_leave_days"],
                "short_leave_penalty_days": results["short_leave_penalty_days"],
                "absent_days": results["absent_days"],
                "lop_days": results["lop_days"],
                
                "ctc": results["ctc"],
                "monthly_salary": results["monthly_salary"],
                "basic_salary": results["basic_salary"],
                "hra": results["hra"],
                "special_allowance": results["special_allowance"],
                "daily_wage": results["daily_wage"],
                
                "lop_deduction": results["lop_deduction"],
                "attendance_salary": results["attendance_salary"],
                "overtime_hours": 0.0,
                "overtime_amount": 0.0,
                "bonus": 0.0,
                "other_earnings": 0.0,
                "travel_reimbursement": travel_reimb,
                "expense_claims": exp_claims,
                "reimbursements": reimbursements,
                "gross_salary": results["gross_salary"],
                
                "pf_employee": results["pf_employee"],
                "pf_employer": results["pf_employer"],
                "esi_employee": results["esi_employee"],
                "esi_employer": results["esi_employer"],
                "professional_tax": results["professional_tax"],
                "income_tax_tds": results["income_tax_tds"],
                "loan_deductions": 0.0,
                "advance_deductions": 0.0,
                "other_deductions": 0.0,
                "total_deductions": results["total_deductions"],
                
                "calculated_net_salary": results["net_salary"],
                "admin_adjustment": 0.0,
                "net_salary": results["net_salary"],
                
                "status": PayrollStatus.APPROVED if payload.auto_approve else (existing.get("status") if existing else PayrollStatus.DRAFT),
                "email_sent": existing.get("email_sent", False) if existing else False,
                "last_sent_at": existing.get("last_sent_at") if existing else None,
                "sent_to_email": existing.get("sent_to_email") if existing else None,
                "approved_by": current_admin.email if payload.auto_approve else (existing.get("approved_by") if existing else None),
                "approved_at": datetime.now(timezone.utc) if payload.auto_approve else (existing.get("approved_at") if existing else None),
                "locked_by": None,
                "locked_at": None,
                "payment_date": None,
                "payment_reference": None,
                
                "bank_name": salary_structure.get("bank_name"),
                "bank_account": salary_structure.get("bank_account"),
                "bank_ifsc": salary_structure.get("bank_ifsc"),
                "pan_number": salary_structure.get("pan_number"),
                
                "created_at": datetime.now(timezone.utc),
                "created_by": current_admin.email
            }
            
            if existing:
                await payrolls_collection.replace_one({"_id": existing["_id"]}, payroll_doc)
            else:
                await payrolls_collection.insert_one(payroll_doc)
            processed_count += 1
        except Exception as e:
            errors.append(f"Error processing {emp.get('email')}: {str(e)}")

            
    return {
        "message": f"Successfully calculated monthly payroll for {processed_count} employees.",
        "processed_count": processed_count,
        "errors": errors
    }

@router.put("/{id}")
async def edit_payroll_draft(id: str, updates: dict, current_admin: Admin = Depends(get_current_admin)):
    """Edit payroll salary, attendance days, bonuses/deductions, or override the final net payable amount directly."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")
        
    p = await payrolls_collection.find_one({**org_filter, "_id": obj_id})
    if not p:
        raise HTTPException(status_code=404, detail="Payroll record not found")
        
    if p["status"] == PayrollStatus.PAID:
        raise HTTPException(status_code=400, detail="Cannot edit a payroll record that has already been marked as Paid")
        
    # Allow admin to update Monthly Base Salary or Annual CTC directly
    new_ctc = float(p.get("ctc", 0.0))
    salary_updated = False
    if updates.get("monthly_salary") is not None:
        new_ctc = float(updates["monthly_salary"]) * 12.0
        salary_updated = True
    elif updates.get("ctc") is not None:
        new_ctc = float(updates["ctc"])
        salary_updated = True

    working_days = int(updates.get("working_days", p.get("working_days", 30)) or 30)
    present_days = float(updates.get("present_days", p.get("present_days", working_days)))
    paid_leave_days = float(updates.get("paid_leave_days", p.get("paid_leave_days", p.get("leave_days", 0.0))))
    unpaid_leave_days = float(updates.get("unpaid_leave_days", p.get("unpaid_leave_days", 0.0)))
    short_leave_penalty_days = float(updates.get("short_leave_penalty_days", p.get("short_leave_penalty_days", 0.0)))

    # Apply manual earnings / deduction values
    bonus = float(updates.get("bonus", p.get("bonus", 0.0)) or 0.0)
    other_earnings = float(updates.get("other_earnings", p.get("other_earnings", 0.0)) or 0.0)
    overtime_hours = float(updates.get("overtime_hours", p.get("overtime_hours", 0.0)) or 0.0)
    loan_deductions = float(updates.get("loan_deductions", p.get("loan_deductions", 0.0)) or 0.0)
    advance_deductions = float(updates.get("advance_deductions", p.get("advance_deductions", 0.0)) or 0.0)
    other_deductions = float(updates.get("other_deductions", p.get("other_deductions", 0.0)) or 0.0)
    
    reimbursements = float(p.get("travel_reimbursement", 0.0)) + float(p.get("expense_claims", 0.0))

    pf_enabled = updates.get("pf_enabled") if updates.get("pf_enabled") is not None else (p.get("pf_employee", 0.0) > 0)
    esi_enabled = updates.get("esi_enabled") if updates.get("esi_enabled") is not None else (p.get("esi_employee", 0.0) > 0)
    pt_enabled = updates.get("professional_tax_enabled") if updates.get("professional_tax_enabled") is not None else (p.get("professional_tax", 0.0) > 0)

    basic_override = float(updates["basic_salary"]) if updates.get("basic_salary") is not None else (None if salary_updated else p.get("basic_salary"))
    hra_override = float(updates["hra"]) if updates.get("hra") is not None else (None if salary_updated else p.get("hra"))

    # Persist updated base salary structure back to employee document so future calculations use it
    if salary_updated:
        monthly_val = round(new_ctc / 12.0, 2)
        basic_val = round(monthly_val * 0.40, 2)
        hra_val = round(basic_val * 0.50, 2)
        special_val = max(0.0, round(monthly_val - basic_val - hra_val, 2))
        await employees_collection.update_one(
            {"email": p["employee_email"], "organization_id": p["organization_id"]},
            {"$set": {
                "salary_structure.ctc": round(new_ctc, 2),
                "salary_structure.monthly_salary": monthly_val,
                "salary_structure.basic_salary": basic_val,
                "salary_structure.hra": hra_val,
                "salary_structure.special_allowance": special_val,
                "salary_structure.pf_enabled": bool(pf_enabled),
                "salary_structure.esi_enabled": bool(esi_enabled),
                "salary_structure.professional_tax_enabled": bool(pt_enabled),
            }}
        )

    # Recalculate using parameters
    results = calculate_payslip_fields(
        ctc=new_ctc,
        working_days=working_days,
        present_days=present_days,
        paid_leave_days=paid_leave_days,
        unpaid_leave_days=unpaid_leave_days,
        short_leave_penalty_days=short_leave_penalty_days,
        overtime_hours=overtime_hours,
        bonus=bonus,
        other_earnings=other_earnings,
        reimbursements=reimbursements,
        loan_deductions=loan_deductions,
        advance_deductions=advance_deductions,
        other_deductions=other_deductions,
        salary_structure={
            "pf_enabled": bool(pf_enabled),
            "esi_enabled": bool(esi_enabled),
            "professional_tax_enabled": bool(pt_enabled),
            "basic_salary": basic_override,
            "hra": hra_override
        }
    )

    calculated_net = results["net_salary"]
    final_net = calculated_net
    admin_adjustment = 0.0

    # Direct final amount override if admin changed the final net salary price directly
    if updates.get("custom_net_salary") is not None and str(updates.get("custom_net_salary")).strip() != "":
        custom_net = round(float(updates["custom_net_salary"]), 2)
        if abs(custom_net - calculated_net) >= 0.01:
            final_net = custom_net
            admin_adjustment = round(custom_net - calculated_net, 2)
    
    updated_fields = {
        "ctc": results["ctc"],
        "monthly_salary": results["monthly_salary"],
        "basic_salary": results["basic_salary"],
        "hra": results["hra"],
        "special_allowance": results["special_allowance"],
        "daily_wage": results["daily_wage"],
        "working_days": working_days,
        "present_days": present_days,
        "paid_leave_days": results["paid_leave_days"],
        "unpaid_leave_days": results["unpaid_leave_days"],
        "leave_days": results["paid_leave_days"] + results["unpaid_leave_days"],
        "absent_days": results["absent_days"],
        "lop_days": results["lop_days"],
        "bonus": bonus,
        "other_earnings": other_earnings,
        "overtime_hours": overtime_hours,
        "overtime_amount": results["overtime_amount"],
        "loan_deductions": loan_deductions,
        "advance_deductions": advance_deductions,
        "other_deductions": other_deductions,
        "lop_deduction": results["lop_deduction"],
        "attendance_salary": results["attendance_salary"],
        "gross_salary": results["gross_salary"],
        "pf_employee": results["pf_employee"],
        "pf_employer": results["pf_employer"],
        "esi_employee": results["esi_employee"],
        "esi_employer": results["esi_employer"],
        "professional_tax": results["professional_tax"],
        "income_tax_tds": results["income_tax_tds"],
        "total_deductions": results["total_deductions"],
        "calculated_net_salary": calculated_net,
        "admin_adjustment": admin_adjustment,
        "net_salary": final_net,
        "updated_at": datetime.now(timezone.utc),
        "updated_by": current_admin.email
    }
    
    await payrolls_collection.update_one({"_id": obj_id}, {"$set": updated_fields})
    
    p_updated = await payrolls_collection.find_one({"_id": obj_id})
    p_updated["_id"] = str(p_updated["_id"])
    await _attach_company_logo_to_payroll(p_updated)
    return p_updated

@router.post("/{id}/approve")
async def approve_payroll(id: str, current_admin: Admin = Depends(get_current_admin)):
    """Approve a draft payroll record."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")
        
    p = await payrolls_collection.find_one({**org_filter, "_id": obj_id})
    if not p:
        raise HTTPException(status_code=404, detail="Payroll record not found")
        
    if p["status"] != PayrollStatus.DRAFT:
        raise HTTPException(status_code=400, detail="Can only approve draft records")
        
    await payrolls_collection.update_one(
        {"_id": obj_id},
        {"$set": {
            "status": PayrollStatus.APPROVED,
            "approved_by": current_admin.email,
            "approved_at": datetime.now(timezone.utc)
        }}
    )
    return {"message": "Payroll approved successfully", "status": PayrollStatus.APPROVED}

@router.post("/bulk-approve")
async def bulk_approve_payrolls(payload: Any = Body(...), current_admin: Admin = Depends(get_current_admin)):
    """Approve multiple draft payroll records at once. Accepts either ['id1', ...] or {'ids': ['id1', ...]}."""
    if isinstance(payload, dict):
        ids = payload.get("ids") or []
    elif isinstance(payload, list):
        ids = payload
    else:
        ids = []
    org_filter = get_org_filter(current_admin)
    obj_ids = []
    for sid in ids:
        try:
            obj_ids.append(ObjectId(sid))
        except Exception:
            pass
            
    result = await payrolls_collection.update_many(
        {"_id": {"$in": obj_ids}, "status": PayrollStatus.DRAFT, **org_filter},
        {"$set": {
            "status": PayrollStatus.APPROVED,
            "approved_by": current_admin.email,
            "approved_at": datetime.now(timezone.utc)
        }}
    )
    return {"message": f"Successfully approved {result.modified_count} payroll records."}

@router.post("/{id}/lock")
async def lock_payroll(id: str, current_admin: Admin = Depends(get_current_admin)):
    """Lock an approved payroll record to finalize it."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")
        
    p = await payrolls_collection.find_one({**org_filter, "_id": obj_id})
    if not p:
        raise HTTPException(status_code=404, detail="Payroll record not found")
        
    if p["status"] != PayrollStatus.APPROVED:
        raise HTTPException(status_code=400, detail="Can only lock approved records")
        
    await payrolls_collection.update_one(
        {"_id": obj_id},
        {"$set": {
            "status": PayrollStatus.LOCKED,
            "locked_by": current_admin.email,
            "locked_at": datetime.now(timezone.utc)
        }}
    )
    return {"message": "Payroll locked successfully", "status": PayrollStatus.LOCKED}

@router.post("/{id}/mark-paid")
async def mark_payroll_paid(id: str, req: dict, current_admin: Admin = Depends(get_current_admin)):
    """Mark a locked payroll record as paid."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")
        
    p = await payrolls_collection.find_one({**org_filter, "_id": obj_id})
    if not p:
        raise HTTPException(status_code=404, detail="Payroll record not found")
        
    if p["status"] != PayrollStatus.LOCKED:
        raise HTTPException(status_code=400, detail="Can only mark locked records as paid")
        
    pay_ref = req.get("payment_reference")
    if not pay_ref:
        raise HTTPException(status_code=400, detail="Payment reference is required")
        
    await payrolls_collection.update_one(
        {"_id": obj_id},
        {"$set": {
            "status": PayrollStatus.PAID,
            "payment_reference": pay_ref,
            "payment_date": datetime.now(timezone.utc).strftime("%Y-%m-%d")
        }}
    )
    return {"message": "Payroll marked as paid", "status": PayrollStatus.PAID}

@router.get("/{id}/payslip")
async def get_payslip_data(id: str, current_admin: Admin = Depends(get_current_admin)):
    """Generate payslip details for rendering."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")
        
    p = await payrolls_collection.find_one({**org_filter, "_id": obj_id})
    if not p:
        raise HTTPException(status_code=404, detail="Payroll record not found")
        
    p["_id"] = str(p["_id"])
    await _attach_company_logo_to_payroll(p)
    return p


async def _attach_company_logo_to_payroll(p: dict) -> dict:
    """Resolve the company's actual logo_url and name from organizations_collection / settings_collection."""
    if not p:
        return p
    org_id = p.get("organization_id") or "system_org"
    logo_url = None
    company_name = None

    if org_id != "system_org" and ObjectId.is_valid(str(org_id)):
        org_doc = await organizations_collection.find_one({"_id": ObjectId(str(org_id))})
        if org_doc:
            logo_url = org_doc.get("logo_url")
            company_name = org_doc.get("name")

    if not logo_url:
        settings_doc = await settings_collection.find_one({"organization_id": org_id})
        if not settings_doc and org_id == "system_org":
            settings_doc = await settings_collection.find_one({"id": "config"})
        if settings_doc and settings_doc.get("logo_url"):
            logo_url = settings_doc.get("logo_url")

    p["company_logo_url"] = logo_url
    if company_name:
        p["company_name"] = company_name
    return p


def _resolve_company_logo_source(p: Optional[dict] = None) -> Any:
    """Return a file path or BytesIO object for the company's logo to embed in ReportLab PDF."""
    base_dir = os.path.dirname(os.path.abspath("hrms_payroll.py"))
    logo_url = (p or {}).get("company_logo_url")

    if logo_url and isinstance(logo_url, str):
        logo_url = logo_url.strip()
        # 1. Base64 data URI
        if logo_url.startswith("data:image") and "," in logo_url:
            try:
                b64_data = logo_url.split(",", 1)[1]
                return io.BytesIO(base64.b64decode(b64_data))
            except Exception:
                pass

        # 2. Local /uploads/... path (relative or full URL pointing to /uploads/...)
        if "/uploads/" in logo_url:
            rel_upload = logo_url.split("/uploads/", 1)[1].split("?", 1)[0].lstrip("/\\")
            local_upload_path = os.path.join(base_dir, "uploads", rel_upload)
            if os.path.exists(local_upload_path):
                return local_upload_path

        # 3. External HTTP/HTTPS URL
        if logo_url.startswith("http://") or logo_url.startswith("https://"):
            try:
                req = urllib.request.Request(logo_url, headers={"User-Agent": "LogDayPayroll/1.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return io.BytesIO(resp.read())
            except Exception:
                pass

    # 4. Default Company Logo fallback (same /logday_logo.png used in Admin Sidebar)
    candidates = [
        os.path.join(base_dir, "uploads", "logos", "logday_logo.png"),
        os.path.abspath(os.path.join(base_dir, "..", "attendence_inofice__admin_desk", "public", "logday_logo.png")),
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return None


def _number_to_inr_words(amount: float) -> str:
    """Convert a numeric INR amount into formal Indian Rupee words (e.g. 'Rupees Fifty-Two Thousand Fifty Only')."""
    n = int(round(max(0.0, float(amount or 0.0))))
    if n == 0:
        return "Rupees Zero Only"

    ones = [
        "", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
        "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
        "Seventeen", "Eighteen", "Nineteen"
    ]
    tens = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]

    def two_digits(num: int) -> str:
        if num < 20:
            return ones[num]
        return tens[num // 10] + ("-" + ones[num % 10] if num % 10 != 0 else "")

    def three_digits(num: int) -> str:
        h = num // 100
        rem = num % 100
        if h and rem:
            return f"{ones[h]} Hundred {two_digits(rem)}"
        elif h:
            return f"{ones[h]} Hundred"
        return two_digits(rem)

    parts = []
    crore = n // 10000000
    n %= 10000000
    lakh = n // 100000
    n %= 100000
    thousand = n // 1000
    n %= 1000
    rest = n

    if crore:
        parts.append(f"{three_digits(crore)} Crore")
    if lakh:
        parts.append(f"{two_digits(lakh)} Lakh")
    if thousand:
        parts.append(f"{two_digits(thousand)} Thousand")
    if rest:
        parts.append(three_digits(rest))

    return f"Rupees {' '.join(parts)} Only"


def generate_payslip_pdf_bytes(p: dict) -> bytes:
    """Build a simple, clean ONE-TABLE PDF salary slip with the root Technula logo."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=45, leftMargin=45, topMargin=40, bottomMargin=40)
    elements = []

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "PayslipTitle",
        parent=styles["Heading1"],
        fontSize=16,
        textColor=colors.HexColor("#004B87"),
        alignment=1,
        spaceAfter=4,
    )
    sub_style = ParagraphStyle(
        "PayslipSub",
        parent=styles["Normal"],
        fontSize=10,
        textColor=colors.HexColor("#64748B"),
        alignment=1,
        spaceAfter=14,
    )
    normal_style = styles["Normal"]
    right_style = ParagraphStyle("RightCell", parent=normal_style, alignment=2)

    logo_source = _resolve_company_logo_source(p)
    if logo_source:
        try:
            logo_img = RLImage(logo_source, width=180, height=52)
            logo_img.hAlign = "CENTER"
            elements.append(logo_img)
            elements.append(Spacer(1, 6))
        except Exception:
            pass

    elements.append(Paragraph(f"<b>SALARY SLIP — {p.get('payroll_month', 'N/A')}</b>", title_style))
    elements.append(Paragraph(f"Login Email: {p.get('employee_email', 'N/A')}", sub_style))

    monthly_base = float(p.get("monthly_salary") or (float(p.get("ctc", 0.0)) / 12.0))
    working_days = p.get("working_days", 30)
    present_days = p.get("present_days", 0)
    paid_leaves = p.get("paid_leave_days", p.get("leave_days", 0))
    lop_days = p.get("lop_days", p.get("absent_days", 0))
    lop_deduction = float(p.get("lop_deduction", 0.0))
    attendance_salary = float(p.get("attendance_salary", max(0.0, monthly_base - lop_deduction)))
    additions = float(p.get("bonus", 0.0)) + float(p.get("overtime_amount", 0.0)) + float(p.get("other_earnings", 0.0)) + float(p.get("reimbursements", 0.0))
    deductions = float(p.get("total_deductions", 0.0))
    admin_adj = float(p.get("admin_adjustment", 0.0) or 0.0)
    net_salary = float(p.get("net_salary", 0.0))

    table_rows = [
        [Paragraph("<font color='#334155'><b>PARTICULARS</b></font>", normal_style), Paragraph("<font color='#334155'><b>DETAILS / AMOUNT (INR)</b></font>", right_style)],
        [Paragraph("Employee Name", normal_style), Paragraph(f"<b>{p.get('employee_name', 'N/A')} ({p.get('employee_id', 'EMP')})</b>", right_style)],
        [Paragraph("Login Email", normal_style), Paragraph(f"<font color='#004B87'><b>{p.get('employee_email', 'N/A')}</b></font>", right_style)],
        [Paragraph("Department & Designation", normal_style), Paragraph(f"<b>{p.get('department', 'General')} — {p.get('designation', 'Employee')}</b>", right_style)],
        [Paragraph("Attendance (Present / Working)", normal_style), Paragraph(f"<b>{present_days} / {working_days} Days  (Paid Leave: {paid_leaves}, LOP: {lop_days})</b>", right_style)],
        [Paragraph("Monthly Base Salary", normal_style), Paragraph(f"<b>Rs. {monthly_base:,.2f}</b>", right_style)],
        [Paragraph("Loss of Pay (LOP) Deduction", normal_style), Paragraph(f"<font color='#E11D48'><b>-Rs. {lop_deduction:,.2f}</b></font>", right_style)],
        [Paragraph("Attendance Pro-Rated Salary", normal_style), Paragraph(f"<b>Rs. {attendance_salary:,.2f}</b>", right_style)],
        [Paragraph("Additions (Bonus / OT / Claims)", normal_style), Paragraph(f"<font color='#059669'><b>+Rs. {additions:,.2f}</b></font>", right_style)],
        [Paragraph("Deductions (PF / Tax / Advance)", normal_style), Paragraph(f"<font color='#E11D48'><b>-Rs. {deductions:,.2f}</b></font>", right_style)],
    ]
    if abs(admin_adj) >= 0.01:
        sign = "+" if admin_adj > 0 else "-"
        col_hex = "#059669" if admin_adj > 0 else "#E11D48"
        table_rows.append([
            Paragraph("Admin Salary Adjustment (Edited)", normal_style),
            Paragraph(f"<font color='{col_hex}'><b>{sign}Rs. {abs(admin_adj):,.2f}</b></font>", right_style),
        ])

    table_rows.append([
        Paragraph("<font size=11 color=white><b>FINAL NET SALARY PAYABLE</b></font>", normal_style),
        Paragraph(f"<font size=13 color=white><b>Rs. {net_salary:,.2f}</b></font>", right_style),
    ])

    t_single = Table(table_rows, colWidths=[260, 260])
    t_single.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
                ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#004B87")),
                ("LINEBELOW", (0, 0), (-1, -2), 0.75, colors.HexColor("#E2E8F0")),
                ("BOX", (0, 0), (-1, -1), 1.5, colors.HexColor("#94A3B8")),
                ("TOPPADDING", (0, 0), (-1, -2), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -2), 9),
                ("TOPPADDING", (0, -1), (-1, -1), 12),
                ("BOTTOMPADDING", (0, -1), (-1, -1), 12),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    elements.append(t_single)

    doc.build(elements)
    buffer.seek(0)
    return buffer.getvalue()


BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"


def _send_brevo_email_sync(cfg: dict, to_email: str, subject: str, html_body: str, pdf_bytes: Optional[bytes], pdf_filename: Optional[str]) -> Tuple[bool, str]:
    """Send a transactional email (optionally with PDF attachment) through the Brevo API."""
    payload = {
        "sender": {"name": cfg["brevo_sender_name"], "email": cfg["brevo_sender_email"]},
        "to": [{"email": to_email}],
        "subject": subject,
        "htmlContent": html_body,
        "textContent": "Please view this email in an HTML-compatible email client. Your PDF salary slip is attached.",
    }
    if pdf_bytes:
        payload["attachment"] = [{
            "name": pdf_filename or "SalarySlip.pdf",
            "content": base64.b64encode(pdf_bytes).decode("ascii"),
        }]

    req = urllib.request.Request(
        BREVO_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "api-key": cfg["brevo_api_key"],
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
            return True, f"Emailed salary slip to {to_email} via Brevo (id: {body.get('messageId', 'n/a')})"
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode("utf-8") or "{}")
            detail = err.get("message") or err.get("code") or str(e)
        except Exception:
            detail = str(e)
        return False, f"Brevo delivery error ({e.code}): {detail}"
    except Exception as e:
        return False, f"Brevo delivery error: {str(e)}"


def _send_email_sync(cfg: dict, to_email: str, subject: str, html_body: str, pdf_bytes: Optional[bytes], pdf_filename: Optional[str]) -> Tuple[bool, str]:
    """Route email through Brevo (preferred) or legacy SMTP."""
    if cfg.get("provider") == "brevo":
        return _send_brevo_email_sync(cfg, to_email, subject, html_body, pdf_bytes, pdf_filename)
    if cfg.get("provider") == "smtp":
        return _send_smtp_email_sync(cfg, to_email, subject, html_body, pdf_bytes, pdf_filename)
    return False, "Email provider is not configured."


def _send_smtp_email_sync(smtp_cfg: dict, to_email: str, subject: str, html_body: str, pdf_bytes: Optional[bytes], pdf_filename: Optional[str]) -> Tuple[bool, str]:
    """Send salary slip email with PDF attachment over SMTP (legacy fallback)."""
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = f"{smtp_cfg['smtp_from_name']} <{smtp_cfg['smtp_from_email']}>"
        msg["To"] = to_email
        msg.set_content("Please view this salary slip in an HTML-compatible email client. Your PDF salary slip is attached.")
        msg.add_alternative(html_body, subtype="html")
        if pdf_bytes:
            msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=pdf_filename or "SalarySlip.pdf")

        host = smtp_cfg["smtp_host"]
        port = int(smtp_cfg["smtp_port"])
        user = smtp_cfg["smtp_user"]
        password = smtp_cfg["smtp_password"]

        if port == 465:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=context, timeout=15) as server:
                server.login(user, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=15) as server:
                server.ehlo()
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
                server.login(user, password)
                server.send_message(msg)
        return True, f"Emailed salary slip to {to_email}"
    except Exception as e:
        return False, f"SMTP delivery error: {str(e)}"


async def _dispatch_single_payslip(p: dict, current_admin: Admin) -> dict:
    """Generate PDF, send email to employee's LogDay login email, and publish to their in-app Payslips."""
    to_email = (p.get("employee_email") or "").strip().lower()
    if not to_email:
        raise HTTPException(status_code=400, detail="Employee login email is missing on this payroll record")

    month_str = p.get("payroll_month", "")
    emp_name = p.get("employee_name", to_email)
    net_amt = float(p.get("net_salary", 0.0))
    gross_amt = float(p.get("gross_salary", 0.0))
    ded_amt = float(p.get("total_deductions", 0.0))
    pres_days = p.get("present_days", 0)
    work_days = p.get("working_days", 0)
    lop_days = p.get("lop_days", p.get("absent_days", 0))

    await _attach_company_logo_to_payroll(p)
    pdf_bytes = await run_in_threadpool(generate_payslip_pdf_bytes, p)
    pdf_filename = f"SalarySlip_{emp_name.replace(' ', '_')}_{month_str}.pdf"
    comp_name = p.get("company_name") or "LogDay"
    comp_logo = p.get("company_logo_url") or ""
    logo_html = f'<img src="{comp_logo}" alt="{comp_name}" style="max-height: 44px; margin-bottom: 8px; display: block;" />' if comp_logo else ""

    html_body = f"""
    <div style="font-family: 'Segoe UI', Arial, sans-serif; max-width: 600px; margin: 0 auto; border: 1px solid #e2e8f0; border-radius: 16px; overflow: hidden; background: #ffffff;">
      <div style="background: #004B87; color: #ffffff; padding: 24px;">
        {logo_html}
        <h2 style="margin: 0; font-size: 20px;">{comp_name} — Official Salary Slip</h2>
        <p style="margin: 4px 0 0; font-size: 13px; opacity: 0.9;">Pay Period: {month_str} • Account: {to_email}</p>
      </div>
      <div style="padding: 24px; color: #1e293b;">
        <p style="font-size: 14px; margin-top: 0;">Hello <b>{emp_name}</b>,</p>
        <p style="font-size: 13px; color: #475569; line-height: 1.5;">
          Your salary slip for <b>{month_str}</b> has been calculated and finalized based on your attendance and salary structure. The certified PDF salary slip is attached to this email and is also available in your LogDay app under <b>Payslips</b>.
        </p>
        <table style="width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 13px;">
          <tr style="background: #f8fafc; border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 10px 12px; color: #64748b;">Attendance Recorded</td>
            <td style="padding: 10px 12px; text-align: right; font-weight: bold;">{pres_days} / {work_days} Days (LOP: {lop_days})</td>
          </tr>
          <tr style="border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 10px 12px; color: #64748b;">Monthly Base Salary</td>
            <td style="padding: 10px 12px; text-align: right; font-weight: bold;">₹{float(p.get('monthly_salary', 0.0)):,.2f}</td>
          </tr>
          <tr style="background: #f8fafc; border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 10px 12px; color: #64748b;">Gross Earnings</td>
            <td style="padding: 10px 12px; text-align: right; font-weight: bold;">₹{gross_amt:,.2f}</td>
          </tr>
          <tr style="border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 10px 12px; color: #64748b;">Total Deductions (PF / Tax / LOP)</td>
            <td style="padding: 10px 12px; text-align: right; font-weight: bold; color: #e11d48;">-₹{ded_amt:,.2f}</td>
          </tr>
          <tr style="background: #ecfdf5; border-top: 2px solid #10b981;">
            <td style="padding: 14px 12px; font-weight: bold; color: #065f46; font-size: 15px;">Final Net Payable Salary</td>
            <td style="padding: 14px 12px; text-align: right; font-weight: 900; color: #059669; font-size: 18px;">₹{net_amt:,.2f}</td>
          </tr>
        </table>
        <p style="font-size: 12px; color: #94a3b8; margin-top: 20px;">
          Log in to your LogDay Mobile or WFH Desktop app using <b>{to_email}</b> to view or download your salary slip anytime.
        </p>
      </div>
    </div>
    """

    org_id = p.get("organization_id") or current_admin.organization_id or "system_org"
    smtp_cfg = await _get_org_smtp_config(org_id)

    email_sent = False
    delivery_error = None
    delivery_note = ""
    if smtp_cfg["configured"]:
        email_sent, delivery_note = await run_in_threadpool(
            _send_email_sync,
            smtp_cfg,
            to_email,
            f"Salary Slip for {month_str} — {emp_name} (Rs. {net_amt:,.2f})",
            html_body,
            pdf_bytes,
            pdf_filename,
        )
        if not email_sent:
            delivery_error = delivery_note
    else:
        delivery_note = f"Published to {to_email}'s LogDay app Payslips hub (Configure Brevo email settings to also send external email)."

    now_utc = datetime.now(timezone.utc)
    new_status = p.get("status")
    if new_status == PayrollStatus.DRAFT:
        new_status = PayrollStatus.APPROVED

    update_doc = {
        "status": new_status,
        "published_in_app": True,
        "email_sent": bool(email_sent),
        "smtp_delivered": bool(email_sent),
        "last_sent_at": now_utc.isoformat(),
        "sent_to_email": to_email,
        "sent_by": current_admin.email,
        "delivery_note": delivery_note,
        "delivery_error": delivery_error,
    }
    if not p.get("approved_by"):
        update_doc["approved_by"] = current_admin.email
        update_doc["approved_at"] = now_utc

    await payrolls_collection.update_one({"_id": ObjectId(str(p["_id"]))}, {"$set": update_doc})

    # Also send an in-app notification to the employee's LogDay account
    try:
        await wfh_alerts_collection.insert_one({
            "employee_email": to_email,
            "employee_name": emp_name,
            "organization_id": org_id,
            "type": "SALARY_SLIP_RELEASED",
            "severity": "low",
            "status": "pending",
            "details": f"Your salary slip for {month_str} (Net Payout: ₹{net_amt:,.2f}) has been released to {to_email} and is ready for download.",
            "timestamp": now_utc,
            "created_at": now_utc,
        })
    except Exception:
        pass

    return {
        "payroll_id": str(p["_id"]),
        "sent_to": to_email,
        "published_in_app": True,
        "email_sent": bool(email_sent),
        "smtp_delivered": bool(email_sent),
        "smtp_configured": smtp_cfg["configured"],
        "provider": smtp_cfg.get("provider", "none"),
        "delivery_error": delivery_error,
        "status": new_status,
        "message": delivery_note,
    }


@router.get("/{id}/download-pdf")
async def admin_download_payslip_pdf(id: str, current_admin: Admin = Depends(get_current_admin)):
    """Download the PDF salary slip from the Admin Panel."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")

    p = await payrolls_collection.find_one({**org_filter, "_id": obj_id})
    if not p:
        raise HTTPException(status_code=404, detail="Payroll record not found")

    await _attach_company_logo_to_payroll(p)
    pdf_bytes = await run_in_threadpool(generate_payslip_pdf_bytes, p)
    filename = f"payslip_{p.get('employee_name', 'employee').replace(' ', '_')}_{p.get('payroll_month', '')}.pdf"
    return StreamingResponse(
        io.BytesIO(pdf_bytes),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/{id}/send")
async def send_payroll_to_employee(id: str, current_admin: Admin = Depends(get_current_admin)):
    """Send the calculated/adjusted salary slip to the employee's LogDay login email and release in-app."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")

    p = await payrolls_collection.find_one({**org_filter, "_id": obj_id})
    if not p:
        raise HTTPException(status_code=404, detail="Payroll record not found")

    result = await _dispatch_single_payslip(p, current_admin)
    return result


@router.post("/bulk-send")
async def bulk_send_payrolls_to_employees(payload: Any = Body(...), current_admin: Admin = Depends(get_current_admin)):
    """Send multiple salary slips at once to employees' LogDay login emails."""
    if isinstance(payload, dict):
        ids = payload.get("ids") or []
    elif isinstance(payload, list):
        ids = payload
    else:
        ids = []

    org_filter = get_org_filter(current_admin)
    obj_ids = []
    for sid in ids:
        try:
            obj_ids.append(ObjectId(sid))
        except Exception:
            pass

    cursor = payrolls_collection.find({"_id": {"$in": obj_ids}, **org_filter})
    records = await cursor.to_list(length=500)

    sent_count = 0
    smtp_count = 0
    failed_count = 0
    results = []
    for rec in records:
        res = await _dispatch_single_payslip(rec, current_admin)
        sent_count += 1
        if res.get("smtp_delivered"):
            smtp_count += 1
        elif res.get("smtp_configured"):
            failed_count += 1
        results.append(res)

    msg = f"Processed {sent_count} salary slips ({smtp_count} emailed via Brevo, {sent_count} published in-app"
    if failed_count:
        msg += f", {failed_count} email delivery failed"
    msg += ")."

    return {
        "message": msg,
        "sent_count": sent_count,
        "smtp_delivered_count": smtp_count,
        "failed_count": failed_count,
        "results": results,
    }



