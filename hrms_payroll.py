import calendar
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from bson import ObjectId

from database import (
    payrolls_collection, employees_collection,
    attendance_logs_collection, leave_requests_collection,
    km_reimbursements_collection, expense_claims_collection,
    holidays_collection
)
from models import Admin
from auth import get_current_admin
from hrms_models import PayrollStatus, PayrollCreate, PayrollRunRequest, DaysOffSaveRequest
from permissions import require_feature

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

def calculate_payslip_fields(ctc: float, working_days: int, present_days: int, leave_days: int, 
                             overtime_hours: float = 0.0, bonus: float = 0.0, other_earnings: float = 0.0,
                             reimbursements: float = 0.0,
                             loan_deductions: float = 0.0, advance_deductions: float = 0.0, other_deductions: float = 0.0,
                             salary_structure: dict = None):
    """Core salary engine."""
    monthly_salary = ctc / 12.0
    
    if salary_structure and salary_structure.get("basic_salary"):
        basic_salary = salary_structure["basic_salary"]
    else:
        basic_salary = monthly_salary * 0.40  # Default 40%
        
    if salary_structure and salary_structure.get("hra"):
        hra = salary_structure["hra"]
    else:
        hra = basic_salary * 0.50  # Default 50% of basic
        
    special_allowance = monthly_salary - basic_salary - hra
    if special_allowance < 0:
        special_allowance = 0.0
        
    # Calculate absent days
    absent_days = max(0, working_days - present_days - leave_days)
    
    # Loss of Pay deduction
    daily_wage = monthly_salary / max(1, working_days)
    lop_deduction = daily_wage * absent_days
    
    # Net attendance salary
    attendance_salary = monthly_salary - lop_deduction
    if attendance_salary < 0:
        attendance_salary = 0.0
        
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
    net_salary = gross_salary - total_deductions + reimbursements
    if net_salary < 0:
        net_salary = 0.0
        
    return {
        "ctc": ctc,
        "monthly_salary": round(monthly_salary, 2),
        "basic_salary": round(basic_salary, 2),
        "hra": round(hra, 2),
        "special_allowance": round(special_allowance, 2),
        "daily_wage": round(daily_wage, 2),
        "absent_days": absent_days,
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
    """Fetch working days, present days, and leave days for the month, accounting for paid days off."""
    emp = await employees_collection.find_one({"email": employee_email, "organization_id": org_id})
    if not emp:
        return 0, 0, 0, 0
        
    emp_id = str(emp["_id"])
    year, month = map(int, payroll_month.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    
    # 1. Total working days is full calendar days (since we pay for holidays/weekends)
    working_days = num_days
    
    # Fetch configured days off (holidays/weekends)
    days_off_doc = await holidays_collection.find_one({
        "organization_id": org_id,
        "payroll_month": payroll_month
    })
    
    if days_off_doc:
        days_off = days_off_doc.get("days_off", [])
    else:
        # Default: Sundays are off
        days_off = []
        for day in range(1, num_days + 1):
            dt = datetime(year, month, day)
            if dt.weekday() == 6: # Sunday
                days_off.append(day)
                
    # Dates for querying logs (UTC boundaries)
    start_dt = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end_dt = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end_dt = datetime(year, month + 1, 1, tzinfo=timezone.utc)

    # 2. Present days count on actual work days
    cursor = attendance_logs_collection.find({
        "user_id": emp_id,
        "type": "check-in",
        "timestamp": {"$gte": start_dt, "$lt": end_dt}
    })
    logs = await cursor.to_list(length=100)
    
    present_dates = set()
    for log in logs:
        ts = log["timestamp"]
        ist_ts = ts + timedelta(hours=5, minutes=30)
        present_dates.add(ist_ts.day)

    # 3. Approved leave days overlapping this month
    cursor_leaves = leave_requests_collection.find({
        "employee_id": employee_email,
        "organization_id": org_id,
        "status": "approved"
    })
    leaves = await cursor_leaves.to_list(length=100)
    
    leave_dates = set()
    start_month_str = start_dt.strftime("%Y-%m-%d")
    end_month_str = (end_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    
    for l in leaves:
        l_start = l["start_date"]
        l_end = l["end_date"]
        overlap_start = max(start_month_str, l_start)
        overlap_end = min(end_month_str, l_end)
        
        if overlap_start <= overlap_end:
            cur_dt = datetime.strptime(overlap_start, "%Y-%m-%d")
            end_dt_loop = datetime.strptime(overlap_end, "%Y-%m-%d")
            while cur_dt <= end_dt_loop:
                leave_dates.add(cur_dt.day)
                cur_dt += timedelta(days=1)
                
    # Present days = check-ins on working days + days off (paid)
    check_ins_on_work_days = sum(1 for d in present_dates if d not in days_off)
    present_days = check_ins_on_work_days + len(days_off)
    
    # Leave days = approved leaves on working days
    leave_days = sum(1 for d in leave_dates if d not in days_off)
    
    # Absent days = actual working days where employee did not check in and had no leave
    actual_work_days = num_days - len(days_off)
    absent_days = max(0, actual_work_days - check_ins_on_work_days - leave_days)
    
    return working_days, present_days, leave_days, absent_days

@router.get("")
async def list_payrolls(payroll_month: Optional[str] = None, department: Optional[str] = None, status: Optional[str] = None, current_admin: Admin = Depends(get_current_admin)):
    """List payroll records for a month and/or department."""
    org_filter = get_org_filter(current_admin)
    query = {**org_filter}
    
    if payroll_month:
        query["payroll_month"] = payroll_month
    if department:
        query["department"] = department
    if status:
        query["status"] = status
        
    cursor = payrolls_collection.find(query).sort("net_salary", -1)
    payrolls = await cursor.to_list(length=1000)
    for p in payrolls:
        p["_id"] = str(p["_id"])
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
        
    doc = await holidays_collection.find_one({"organization_id": org_id, "payroll_month": payroll_month})
    if doc:
        return {"days_off": doc.get("days_off", [])}
    
    # Return default Sundays
    year, month = map(int, payroll_month.split("-"))
    num_days = calendar.monthrange(year, month)[1]
    days_off = []
    for day in range(1, num_days + 1):
        dt = datetime(year, month, day)
        if dt.weekday() == 6: # Sunday
            days_off.append(day)
    return {"days_off": days_off}

@router.post("/days-off")
async def save_days_off(req: DaysOffSaveRequest, current_admin: Admin = Depends(get_current_admin)):
    """Save custom days off configuration for the month."""
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
        {"organization_id": org_id, "payroll_month": req.payroll_month},
        {"$set": {"days_off": req.days_off}},
        upsert=True
    )
    return {"message": "Days off saved successfully", "days_off": req.days_off}

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
    
    if w_days is None or p_days is None or l_days is None:
        fetched_w, fetched_p, fetched_l, fetched_a = await fetch_attendance_data(email_clean, org_id, payload.payroll_month)
        w_days = w_days if w_days is not None else fetched_w
        p_days = p_days if p_days is not None else fetched_p
        l_days = l_days if l_days is not None else fetched_l

    # Fetch travel reimbursements and expenses
    travel_reimb, exp_claims = await fetch_approved_claims_and_reimbursements(email_clean, org_id, payload.payroll_month)
    reimbursements = travel_reimb + exp_claims

    # Run calculations
    results = calculate_payslip_fields(
        ctc=salary_structure.get("ctc", 0.0),
        working_days=w_days,
        present_days=p_days,
        leave_days=l_days,
        overtime_hours=payload.overtime_hours,
        bonus=payload.bonus,
        other_earnings=payload.other_earnings,
        reimbursements=reimbursements,
        loan_deductions=payload.loan_deductions,
        advance_deductions=payload.advance_deductions,
        other_deductions=payload.other_deductions,
        salary_structure=salary_structure
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
        "absent_days": results["absent_days"],
        
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
    """Run bulk payroll calculations for all employees configured with CTC."""
    org_id = current_admin.organization_id
    
    # Query matching employees
    emp_query = {"status": "Active", "salary_structure": {"$ne": None}}
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
            emp_org_id = emp["organization_id"]
            # Check if payroll already exists for this employee/month
            existing = await payrolls_collection.find_one({
                "employee_email": emp["email"],
                "organization_id": emp_org_id,
                "payroll_month": payload.payroll_month
            })
            if existing and existing["status"] != PayrollStatus.DRAFT:
                continue # Skip approved/paid records
                
            w_days, p_days, l_days, a_days = await fetch_attendance_data(emp["email"], emp_org_id, payload.payroll_month)
            salary_structure = emp["salary_structure"]
            
            # Fetch travel reimbursements and expenses
            travel_reimb, exp_claims = await fetch_approved_claims_and_reimbursements(emp["email"], emp_org_id, payload.payroll_month)
            reimbursements = travel_reimb + exp_claims

            results = calculate_payslip_fields(
                ctc=salary_structure.get("ctc", 0.0),
                working_days=w_days,
                present_days=p_days,
                leave_days=l_days,
                other_earnings=0.0,
                reimbursements=reimbursements,
                salary_structure=salary_structure
            )
            
            payroll_doc = {
                "organization_id": emp_org_id,
                "employee_email": emp["email"],
                "employee_name": emp["full_name"],
                "employee_id": emp.get("employee_id", "EMP"),
                "department": emp.get("department", "General"),
                "designation": emp.get("designation", "Employee"),
                "payroll_month": payload.payroll_month,
                
                "working_days": w_days,
                "present_days": p_days,
                "leave_days": l_days,
                "absent_days": results["absent_days"],
                
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
                
                "net_salary": results["net_salary"],
                
                "status": PayrollStatus.APPROVED if payload.auto_approve else PayrollStatus.DRAFT,
                "approved_by": current_admin.email if payload.auto_approve else None,
                "approved_at": datetime.now(timezone.utc) if payload.auto_approve else None,
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
            errors.append(f"Error processing {emp['email']}: {str(e)}")

            
    return {
        "message": f"Successfully processed monthly payroll for {processed_count} employees.",
        "processed_count": processed_count,
        "errors": errors
    }

@router.put("/{id}")
async def edit_payroll_draft(id: str, updates: dict, current_admin: Admin = Depends(get_current_admin)):
    """Edit draft payroll adjustments (bonus, overtime, deductions)."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")
        
    p = await payrolls_collection.find_one({**org_filter, "_id": obj_id})
    if not p:
        raise HTTPException(status_code=404, detail="Payroll record not found")
        
    if p["status"] != PayrollStatus.DRAFT:
        raise HTTPException(status_code=400, detail="Can only edit draft payroll records")
        
    # Apply manual values
    bonus = updates.get("bonus", p.get("bonus", 0.0))
    other_earnings = updates.get("other_earnings", p.get("other_earnings", 0.0))
    overtime_hours = updates.get("overtime_hours", p.get("overtime_hours", 0.0))
    loan_deductions = updates.get("loan_deductions", p.get("loan_deductions", 0.0))
    advance_deductions = updates.get("advance_deductions", p.get("advance_deductions", 0.0))
    other_deductions = updates.get("other_deductions", p.get("other_deductions", 0.0))
    
    reimbursements = p.get("travel_reimbursement", 0.0) + p.get("expense_claims", 0.0)

    # Recalculate using existing parameters
    results = calculate_payslip_fields(
        ctc=p["ctc"],
        working_days=p["working_days"],
        present_days=p["present_days"],
        leave_days=p["leave_days"],
        overtime_hours=overtime_hours,
        bonus=bonus,
        other_earnings=other_earnings,
        reimbursements=reimbursements,
        loan_deductions=loan_deductions,
        advance_deductions=advance_deductions,
        other_deductions=other_deductions,
        salary_structure={
            "pf_enabled": p.get("pf_employee", 0.0) > 0,
            "esi_enabled": p.get("esi_employee", 0.0) > 0,
            "professional_tax_enabled": p.get("professional_tax", 0.0) > 0,
            "basic_salary": p.get("basic_salary"),
            "hra": p.get("hra")
        }
    )
    
    updated_fields = {
        "bonus": bonus,
        "other_earnings": other_earnings,
        "overtime_hours": overtime_hours,
        "overtime_amount": results["overtime_amount"],
        "loan_deductions": loan_deductions,
        "advance_deductions": advance_deductions,
        "other_deductions": other_deductions,
        "gross_salary": results["gross_salary"],
        "pf_employee": results["pf_employee"],
        "pf_employer": results["pf_employer"],
        "esi_employee": results["esi_employee"],
        "esi_employer": results["esi_employer"],
        "professional_tax": results["professional_tax"],
        "income_tax_tds": results["income_tax_tds"],
        "total_deductions": results["total_deductions"],
        "net_salary": results["net_salary"],
        "updated_at": datetime.now(timezone.utc),
        "updated_by": current_admin.email
    }
    
    await payrolls_collection.update_one({"_id": obj_id}, {"$set": updated_fields})
    
    p_updated = await payrolls_collection.find_one({"_id": obj_id})
    p_updated["_id"] = str(p_updated["_id"])
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
async def bulk_approve_payrolls(ids: List[str], current_admin: Admin = Depends(get_current_admin)):
    """Approve multiple draft payroll records at once."""
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
    return p



