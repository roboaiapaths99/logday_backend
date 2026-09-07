import calendar
from datetime import datetime, timezone, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from bson import ObjectId

from database import (
    payrolls_collection, employees_collection,
    attendance_logs_collection, leave_requests_collection,
    km_reimbursements_collection, expense_claims_collection,
    holidays_collection, leave_policies_collection, settings_collection
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

def calculate_payslip_fields(ctc: float, working_days: int, present_days: float,
                             paid_leave_days: float = 0.0, unpaid_leave_days: float = 0.0,
                             short_leave_penalty_days: float = 0.0, leave_days: Optional[float] = None,
                             overtime_hours: float = 0.0, bonus: float = 0.0, other_earnings: float = 0.0,
                             reimbursements: float = 0.0,
                             loan_deductions: float = 0.0, advance_deductions: float = 0.0, other_deductions: float = 0.0,
                             salary_structure: dict = None):
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

    # Dates for querying logs (UTC boundaries)
    start_dt = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end_dt = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end_dt = datetime(year, month + 1, 1, tzinfo=timezone.utc)

    # Fetch Leave Policy
    # Fetch Leave Policy & System Settings
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
    paid_leave_types = ["sick", "casual", "on_duty", "duty"]
    short_leave_monthly_limit = 2
    short_leave_penalty_rule = "half_day_per_2_excess"
    if policy:
        paid_leave_types = policy.get("paid_leave_types", paid_leave_types)
        short_leave_monthly_limit = int(policy.get("short_leave_monthly_limit", 2))
        short_leave_penalty_rule = policy.get("short_leave_penalty_rule", "half_day_per_2_excess")

    from main import classify_attendance_day

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
    start_month_str = start_dt.strftime("%Y-%m-%d")
    end_month_str = (end_dt - timedelta(days=1)).strftime("%Y-%m-%d")

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

            # Map to individual days for attendance classifier lookup
            cur_dt = max(datetime.strptime(l_start[:10], "%Y-%m-%d"), datetime.strptime(start_month_str, "%Y-%m-%d"))
            end_dt_loop = min(datetime.strptime(l_end[:10], "%Y-%m-%d"), datetime.strptime(end_month_str, "%Y-%m-%d"))
            while cur_dt <= end_dt_loop:
                d_str = cur_dt.strftime("%Y-%m-%d")
                leaves_by_date[d_str] = l
                if dur_type == "short_leave":
                    short_leave_days_set.add(cur_dt.day)
                cur_dt += timedelta(days=1)

            l_type = str(l.get("leave_type", "other")).lower().strip()
            if l.get("is_paid") is not None:
                is_p = bool(l.get("is_paid"))
            else:
                is_p = any(pt in l_type for pt in paid_leave_types)

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

    tz_offset = int((org_settings or {}).get("timezone_offset", 330))
    daily_logs = {}
    for log in logs:
        ts = log["timestamp"]
        if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
        local_ts = ts + timedelta(minutes=tz_offset)
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

    actual_work_days = num_days - len(days_off)
    absent_days = max(0.0, float(actual_work_days) - float(check_ins_presence) - float(paid_leave_days) - float(unpaid_leave_days))
    
    return working_days, present_days, paid_leave_days, unpaid_leave_days, absent_days, short_leave_penalty_days

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
    paid_l_days = 0.0
    unpaid_l_days = 0.0
    short_penalty_days = 0.0
    
    if w_days is None or p_days is None or l_days is None:
        fetched_w, fetched_p, fetched_paid_l, fetched_unpaid_l, fetched_a, fetched_short_pen = await fetch_attendance_data(email_clean, org_id, payload.payroll_month)
        w_days = w_days if w_days is not None else fetched_w
        p_days = p_days if p_days is not None else fetched_p
        paid_l_days = fetched_paid_l
        unpaid_l_days = fetched_unpaid_l
        short_penalty_days = fetched_short_pen
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
                
            w_days, p_days, paid_l, unpaid_l, a_days, short_pen = await fetch_attendance_data(emp["email"], emp_org_id, payload.payroll_month)
            salary_structure = emp["salary_structure"]
            
            # Fetch travel reimbursements and expenses
            travel_reimb, exp_claims = await fetch_approved_claims_and_reimbursements(emp["email"], emp_org_id, payload.payroll_month)
            reimbursements = travel_reimb + exp_claims

            results = calculate_payslip_fields(
                ctc=salary_structure.get("ctc", 0.0),
                working_days=w_days,
                present_days=p_days,
                paid_leave_days=paid_l,
                unpaid_leave_days=unpaid_l,
                short_leave_penalty_days=short_pen,
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
        paid_leave_days=p.get("paid_leave_days", p.get("leave_days", 0.0)),
        unpaid_leave_days=p.get("unpaid_leave_days", 0.0),
        short_leave_penalty_days=p.get("short_leave_penalty_days", 0.0),
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



