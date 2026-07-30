from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from bson import ObjectId

from database import exit_managements_collection, employees_collection
from models import Admin
from auth import get_current_admin
from hrms_models import ExitStatus, ClearanceStatus, ExitCreate, ClearanceUpdate
from permissions import require_feature

router = APIRouter(
    prefix="/admin/exit-management",
    tags=["HRMS Exit Management"],
    dependencies=[Depends(require_feature("exit_management"))]
)

def get_org_filter(admin: Admin):
    if admin.role == "superadmin" or admin.organization_id == "system_org":
        return {}
    return {"organization_id": admin.organization_id}

DEFAULT_CLEARANCES = {
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
            {"item": "VPN access revoked", "done": False},
            {"item": "Software licenses deactivated", "done": False}
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
            {"item": "Parking pass returned", "done": False},
            {"item": "Company phone returned", "done": False},
            {"item": "Other assets returned", "done": False}
        ],
        "cleared_by": None,
        "cleared_at": None,
        "notes": ""
    }
}

@router.get("")
async def list_exits(status: Optional[str] = None, current_admin: Admin = Depends(get_current_admin)):
    """List all exit processes for the organization."""
    org_filter = get_org_filter(current_admin)
    query = {**org_filter}
    if status:
        query["status"] = status
        
    cursor = exit_managements_collection.find(query).sort("created_at", -1)
    exits = await cursor.to_list(length=1000)
    for e in exits:
        e["_id"] = str(e["_id"])
    return exits

@router.get("/stats")
async def get_exit_stats(current_admin: Admin = Depends(get_current_admin)):
    """Get counts of exits in different states."""
    org_filter = get_org_filter(current_admin)
    
    total = await exit_managements_collection.count_documents(org_filter)
    pending = await exit_managements_collection.count_documents({**org_filter, "status": ExitStatus.PENDING})
    in_progress = await exit_managements_collection.count_documents({**org_filter, "status": ExitStatus.IN_PROGRESS})
    completed = await exit_managements_collection.count_documents({**org_filter, "status": ExitStatus.COMPLETED})
    cancelled = await exit_managements_collection.count_documents({**org_filter, "status": ExitStatus.CANCELLED})
    
    return {
        "total": total,
        "pending": pending,
        "in_progress": in_progress,
        "completed": completed,
        "cancelled": cancelled
    }

@router.get("/{id}")
async def get_exit(id: str, current_admin: Admin = Depends(get_current_admin)):
    """Get single exit details by ID."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")
        
    exit_doc = await exit_managements_collection.find_one({**org_filter, "_id": obj_id})
    if not exit_doc:
        raise HTTPException(status_code=404, detail="Exit process not found")
        
    exit_doc["_id"] = str(exit_doc["_id"])
    return exit_doc

@router.post("", status_code=status.HTTP_201_CREATED)
async def initiate_exit(payload: ExitCreate, current_admin: Admin = Depends(get_current_admin)):
    """Initiate exit process for an employee."""
    org_id = current_admin.organization_id
    email_clean = payload.employee_email.strip().lower()
    
    emp = await employees_collection.find_one({"email": email_clean, "organization_id": org_id})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found in organization")
        
    # Check if there is already an active exit process
    existing = await exit_managements_collection.find_one({
        "employee_email": email_clean,
        "organization_id": org_id,
        "status": {"$in": [ExitStatus.PENDING, ExitStatus.IN_PROGRESS]}
    })
    if existing:
        raise HTTPException(status_code=400, detail="An active exit process already exists for this employee")
        
    exit_doc = {
        "organization_id": org_id,
        "employee_email": email_clean,
        "employee_name": emp["full_name"],
        "employee_id": emp.get("employee_id", "EMP"),
        "department": emp.get("department", "General"),
        "designation": emp.get("designation", "Employee"),
        
        "exit_reason": payload.exit_reason,
        "resignation_date": payload.resignation_date,
        "last_working_day": payload.last_working_day,
        "notice_period_days": payload.notice_period_days,
        "notice_served_days": payload.notice_period_days,  # Default to notice period
        
        "status": ExitStatus.IN_PROGRESS,
        "progress": 0.0,
        "assigned_to": payload.assigned_to,
        
        "clearances": DEFAULT_CLEARANCES,
        
        "exit_interview": {
            "conducted": False,
            "interviewer": payload.exit_interviewer,
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
        "updated_at": datetime.now(timezone.utc),
        "created_by": current_admin.email
    }
    
    result = await exit_managements_collection.insert_one(exit_doc)
    exit_doc["_id"] = str(result.inserted_id)
    return exit_doc

@router.put("/{id}")
async def update_exit(id: str, updates: dict, current_admin: Admin = Depends(get_current_admin)):
    """Update general exit details."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")
        
    exit_doc = await exit_managements_collection.find_one({**org_filter, "_id": obj_id})
    if not exit_doc:
        raise HTTPException(status_code=404, detail="Exit process not found")
        
    allowed_updates = ["last_working_day", "notice_period_days", "notice_served_days", "status", "assigned_to"]
    update_doc = {}
    for k, v in updates.items():
        if k in allowed_updates:
            update_doc[k] = v
            
    if update_doc:
        update_doc["updated_at"] = datetime.now(timezone.utc)
        await exit_managements_collection.update_one({"_id": obj_id}, {"$set": update_doc})
        
    updated = await exit_managements_collection.find_one({"_id": obj_id})
    updated["_id"] = str(updated["_id"])
    return updated

@router.put("/{id}/clearance")
async def update_department_clearance(id: str, payload: ClearanceUpdate, current_admin: Admin = Depends(get_current_admin)):
    """Update clearance status & checklist items for a department (HR, IT, Finance, Assets)."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")
        
    exit_doc = await exit_managements_collection.find_one({**org_filter, "_id": obj_id})
    if not exit_doc:
        raise HTTPException(status_code=404, detail="Exit process not found")
        
    dept = payload.department.lower()
    if dept not in ["hr", "it", "finance", "assets"]:
        raise HTTPException(status_code=400, detail="Invalid department name. Must be one of hr, it, finance, assets")
        
    clearances = exit_doc.get("clearances", {})
    if dept not in clearances:
        clearances[dept] = DEFAULT_CLEARANCES[dept].copy()
        
    # Update status, cleared_by, notes
    clearances[dept]["status"] = payload.status
    clearances[dept]["notes"] = payload.notes or clearances[dept].get("notes", "")
    
    if payload.status == ClearanceStatus.COMPLETED:
        clearances[dept]["cleared_by"] = current_admin.email
        clearances[dept]["cleared_at"] = datetime.now(timezone.utc).isoformat()
        # Mark all checklist items for this department as done if they were not
        for item in clearances[dept].get("items", []):
            item["done"] = True
    else:
        clearances[dept]["cleared_by"] = None
        clearances[dept]["cleared_at"] = None

    # Calculate overall progress: count of completed clearances / 4.0
    completed_count = sum(1 for d in clearances.values() if d.get("status") == ClearanceStatus.COMPLETED)
    progress = round((completed_count / 4.0) * 100, 2)
    
    await exit_managements_collection.update_one(
        {"_id": obj_id},
        {"$set": {
            "clearances": clearances,
            "progress": progress,
            "updated_at": datetime.now(timezone.utc)
        }}
    )
    
    updated = await exit_managements_collection.find_one({"_id": obj_id})
    updated["_id"] = str(updated["_id"])
    return updated

@router.put("/{id}/clearance-items")
async def update_clearance_checklist_items(id: str, req: dict, current_admin: Admin = Depends(get_current_admin)):
    """Update checklist items for a department's clearance."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")
        
    dept = req.get("department")
    items = req.get("items") # Array of {"item": str, "done": bool}
    if not dept or items is None:
         raise HTTPException(status_code=400, detail="Department and checklist items are required")
         
    exit_doc = await exit_managements_collection.find_one({**org_filter, "_id": obj_id})
    if not exit_doc:
        raise HTTPException(status_code=404, detail="Exit process not found")
        
    clearances = exit_doc.get("clearances", {})
    if dept not in clearances:
         raise HTTPException(status_code=400, detail="Department not found in clearances")
         
    clearances[dept]["items"] = items
    
    # Auto-update status if all items are done
    all_done = all(item.get("done", False) for item in items)
    if all_done and clearances[dept]["status"] != ClearanceStatus.COMPLETED:
        clearances[dept]["status"] = ClearanceStatus.COMPLETED
        clearances[dept]["cleared_by"] = current_admin.email
        clearances[dept]["cleared_at"] = datetime.now(timezone.utc).isoformat()
    elif not all_done and clearances[dept]["status"] == ClearanceStatus.COMPLETED:
        clearances[dept]["status"] = ClearanceStatus.IN_PROGRESS
        clearances[dept]["cleared_by"] = None
        clearances[dept]["cleared_at"] = None

    # Calculate overall progress
    completed_count = sum(1 for d in clearances.values() if d.get("status") == ClearanceStatus.COMPLETED)
    progress = round((completed_count / 4.0) * 100, 2)

    await exit_managements_collection.update_one(
        {"_id": obj_id},
        {"$set": {
            "clearances": clearances,
            "progress": progress,
            "updated_at": datetime.now(timezone.utc)
        }}
    )
    
    updated = await exit_managements_collection.find_one({"_id": obj_id})
    updated["_id"] = str(updated["_id"])
    return updated

@router.post("/{id}/settlement")
async def calculate_settlement(id: str, req: dict, current_admin: Admin = Depends(get_current_admin)):
    """Calculate and save final settlement numbers."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")
        
    exit_doc = await exit_managements_collection.find_one({**org_filter, "_id": obj_id})
    if not exit_doc:
        raise HTTPException(status_code=404, detail="Exit process not found")
        
    # Get employee CTC
    emp = await employees_collection.find_one({"email": exit_doc["employee_email"], "organization_id": exit_doc["organization_id"]})
    if not emp or not emp.get("salary_structure"):
        raise HTTPException(status_code=400, detail="Employee or salary structure not found")
        
    ctc = emp["salary_structure"].get("ctc", 0.0)
    monthly_salary = ctc / 12.0
    daily_wage = monthly_salary / 30.0  # standard monthly divider
    
    # Inputs
    unused_leaves = req.get("unused_leave_days", 0.0)
    remaining_days = req.get("remaining_days_in_month", 0.0)
    bonus = req.get("bonus_due", 0.0)
    other_deductions = req.get("other_deductions", 0.0)
    gratuity_override = req.get("gratuity", 0.0)
    
    # Notice recovery
    notice_days = exit_doc.get("notice_period_days", 30)
    notice_served = exit_doc.get("notice_served_days", 30)
    notice_recovery = 0.0
    if notice_served < notice_days:
        notice_recovery = daily_wage * (notice_days - notice_served)
        
    # Standard calculations
    salary_dues = round(daily_wage * remaining_days, 2)
    leave_encashment = round(daily_wage * unused_leaves, 2)
    
    # Gratuity: standard 15 days of basic salary for each year of service (if service >= 5 years)
    # We let it be passed or calculated
    gratuity = gratuity_override
    
    total_payable = salary_dues + leave_encashment + bonus + gratuity - notice_recovery - other_deductions
    total_payable = round(total_payable, 2)
    
    settlement = {
        "calculated": True,
        "salary_dues": salary_dues,
        "leave_encashment": leave_encashment,
        "bonus_due": bonus,
        "gratuity": gratuity,
        "notice_period_recovery": round(notice_recovery, 2),
        "other_deductions": other_deductions,
        "total_payable": total_payable,
        "processed": req.get("processed", False),
        "processed_at": datetime.now(timezone.utc).isoformat() if req.get("processed") else None
    }
    
    await exit_managements_collection.update_one(
        {"_id": obj_id},
        {"$set": {
            "final_settlement": settlement,
            "updated_at": datetime.now(timezone.utc)
        }}
    )
    
    updated = await exit_managements_collection.find_one({"_id": obj_id})
    updated["_id"] = str(updated["_id"])
    return updated

@router.post("/{id}/interview")
async def record_exit_interview(id: str, req: dict, current_admin: Admin = Depends(get_current_admin)):
    """Record employee exit interview feedback details."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")
        
    exit_doc = await exit_managements_collection.find_one({**org_filter, "_id": obj_id})
    if not exit_doc:
        raise HTTPException(status_code=404, detail="Exit process not found")
        
    interview = {
        "conducted": True,
        "interviewer": req.get("interviewer", current_admin.email),
        "date": req.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d")),
        "reason_for_leaving": req.get("reason_for_leaving"),
        "feedback": req.get("feedback"),
        "would_rejoin": req.get("would_rejoin"),
        "improvement_suggestions": req.get("improvement_suggestions")
    }
    
    await exit_managements_collection.update_one(
        {"_id": obj_id},
        {"$set": {
            "exit_interview": interview,
            "clearances.hr.items.0.done": True,
            "updated_at": datetime.now(timezone.utc)
        }}
    )
    
    updated = await exit_managements_collection.find_one({"_id": obj_id})
    updated["_id"] = str(updated["_id"])
    return updated

@router.post("/{id}/complete")
async def complete_exit(id: str, current_admin: Admin = Depends(get_current_admin)):
    """Complete exit management pipeline, verify clearances, and auto-deactivate employee account."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")
        
    exit_doc = await exit_managements_collection.find_one({**org_filter, "_id": obj_id})
    if not exit_doc:
        raise HTTPException(status_code=404, detail="Exit process not found")
        
    clearances = exit_doc.get("clearances", {})
    # Verify all clearances are completed
    for dept, details in clearances.items():
        if details.get("status") != ClearanceStatus.COMPLETED:
            raise HTTPException(
                status_code=400, 
                detail=f"Cannot complete exit. {dept.upper()} clearance is still {details.get('status').upper()}"
            )
            
    # Set exit status to Completed
    await exit_managements_collection.update_one(
        {"_id": obj_id},
        {"$set": {
            "status": ExitStatus.COMPLETED,
            "progress": 100.0,
            "updated_at": datetime.now(timezone.utc)
        }}
    )
    
    # Deactivate the employee account in database
    email_clean = exit_doc["employee_email"].strip().lower()
    await employees_collection.update_one(
        {"email": email_clean, "organization_id": exit_doc["organization_id"]},
        {"$set": {
            "status": "Inactive",
            "is_active": False,
            "last_working_day": exit_doc["last_working_day"]
        }}
    )
    
    return {
        "message": "Exit process completed successfully. Employee account set to Inactive.",
        "status": "completed"
    }
