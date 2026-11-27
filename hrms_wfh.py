from upload_security import SecureMediaRoute
from fastapi import APIRouter, Depends, HTTPException, status
from typing import List, Optional
from datetime import datetime, timezone, timedelta
from bson import ObjectId

from database import wfh_requests_collection, employees_collection, settings_collection
from auth import get_current_employee, get_current_admin
from models import Admin, WFHStatus, EmployeeType
from permissions import require_feature
from services.common_helpers import get_employee_filter, get_scoped_employee_emails

router = APIRouter(route_class=SecureMediaRoute, tags=["WFH Management"])

# ─── Employee Endpoints ─────────────────────────────────────────

@router.post("/wfh/request")
async def create_wfh_request(req_data: dict, employee=Depends(get_current_employee)):
    """
    Submit WFH requests for one or more dates.
    Accepts: {"date": "YYYY-MM-DD", "reason": "..."} or {"dates": ["YYYY-MM-DD"], "reason": "..."}
    """
    reason = req_data.get("reason", "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="Reason is required")

    dates = []
    if "dates" in req_data:
        dates = req_data["dates"]
    elif "date" in req_data:
        dates = [req_data["date"]]

    if not dates:
        raise HTTPException(status_code=400, detail="At least one date is required")

    employee_id = str(employee["_id"])
    employee_email = employee["email"].strip().lower()
    employee_name = employee.get("full_name", employee.get("employee_name", ""))
    organization_id = employee.get("organization_id")

    if not organization_id:
        raise HTTPException(status_code=400, detail="Employee organization ID is missing")

    inserted_ids = []
    skipped_dates = []

    for date_str in dates:
        # Validate date format YYYY-MM-DD
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid date format: {date_str}. Must be YYYY-MM-DD.")

        # Check if a request already exists for this date
        existing = await wfh_requests_collection.find_one({
            "employee_email": employee_email,
            "date": date_str,
            "status": {"$in": ["pending", "approved"]}
        })
        if existing:
            skipped_dates.append(date_str)
            continue

        wfh_request = {
            "employee_id": employee_id,
            "employee_email": employee_email,
            "employee_name": employee_name,
            "organization_id": organization_id,
            "date": date_str,
            "reason": reason,
            "status": "pending",
            "approved_by": None,
            "approved_at": None,
            "rejection_reason": None,
            "created_at": datetime.now(timezone.utc)
        }
        res = await wfh_requests_collection.insert_one(wfh_request)
        inserted_ids.append(str(res.inserted_id))

    return {
        "status": "success",
        "message": f"Submitted WFH requests for {len(inserted_ids)} dates.",
        "inserted_ids": inserted_ids,
        "skipped_dates": skipped_dates
    }


@router.get("/wfh/my-requests")
async def get_my_wfh_requests(employee=Depends(get_current_employee)):
    """Fetch WFH requests for the logged-in employee."""
    email = employee["email"].strip().lower()
    org_id = employee.get("organization_id")
    query = {"employee_email": email}
    if org_id:
        query["organization_id"] = org_id
    requests = await wfh_requests_collection.find(query).sort("date", -1).to_list(length=100)
    for r in requests:
        r["_id"] = str(r["_id"])
        if r.get("created_at"):
            r["created_at"] = r["created_at"].isoformat()
        if r.get("approved_at"):
            r["approved_at"] = r["approved_at"].isoformat()
    return requests


@router.delete("/wfh/request/{request_id}")
async def cancel_wfh_request(request_id: str, employee=Depends(get_current_employee)):
    """Cancel a pending WFH request."""
    try:
        obj_id = ObjectId(request_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request ID")

    email = employee["email"].strip().lower()
    org_id = employee.get("organization_id")
    query = {"_id": obj_id, "employee_email": email}
    if org_id:
        query["organization_id"] = org_id
    req = await wfh_requests_collection.find_one(query)
    if not req:
        raise HTTPException(status_code=404, detail="WFH request not found")

    if req["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Cannot cancel a request that is already {req['status']}")

    await wfh_requests_collection.delete_one({"_id": obj_id})
    return {"status": "success", "message": "WFH request cancelled successfully"}


# ─── Admin Endpoints ────────────────────────────────────────────

async def _build_scoped_wfh_request_query(admin: Admin) -> dict:
    org_id = admin.organization_id
    query: dict = {}
    if org_id and org_id != "system_org" and getattr(admin, "role", "") != "superadmin":
        query["organization_id"] = org_id
    if getattr(admin, "role", "") == "manager":
        scoped_emails = await get_scoped_employee_emails(admin)
        query["employee_email"] = {"$in": [e.strip().lower() for e in scoped_emails if e]}
    return query


@router.get("/admin/wfh/requests")
async def get_wfh_requests(
    status: Optional[str] = None,
    month: Optional[str] = None,
    admin: Admin = Depends(require_feature("wfh_management"))
):
    """List WFH requests for the admin's organization/team, with filters."""
    query = await _build_scoped_wfh_request_query(admin)

    if status:
        query["status"] = status

    if month:
        # Match YYYY-MM prefix in YYYY-MM-DD
        query["date"] = {"$regex": f"^{month}"}

    requests = await wfh_requests_collection.find(query).sort("date", -1).to_list(length=500)
    for r in requests:
        r["_id"] = str(r["_id"])
        if r.get("created_at"):
            r["created_at"] = r["created_at"].isoformat()
        if r.get("approved_at"):
            r["approved_at"] = r["approved_at"].isoformat()
    return requests


@router.put("/admin/wfh/requests/{request_id}/approve")
async def approve_wfh_request(request_id: str, admin: Admin = Depends(require_feature("wfh_management"))):
    """Approve a WFH request."""
    try:
        obj_id = ObjectId(request_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request ID")

    scope_query = await _build_scoped_wfh_request_query(admin)
    req = await wfh_requests_collection.find_one({"_id": obj_id, **scope_query})
    if not req:
        raise HTTPException(status_code=404, detail="WFH request not found")

    if req["status"] != "pending":
         raise HTTPException(status_code=400, detail=f"Request is already {req['status']}")

    await wfh_requests_collection.update_one(
        {"_id": obj_id},
        {
            "$set": {
                "status": "approved",
                "approved_by": admin.email,
                "approved_at": datetime.now(timezone.utc)
            }
        }
    )

    return {"status": "success", "message": "WFH request approved successfully"}


@router.put("/admin/wfh/requests/{request_id}/reject")
async def reject_wfh_request(
    request_id: str,
    req_body: dict,
    admin: Admin = Depends(require_feature("wfh_management"))
):
    """Reject a WFH request with reason."""
    try:
        obj_id = ObjectId(request_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid request ID")

    reason = req_body.get("reason", "").strip()
    if not reason:
         raise HTTPException(status_code=400, detail="Rejection reason is required")

    scope_query = await _build_scoped_wfh_request_query(admin)
    req = await wfh_requests_collection.find_one({"_id": obj_id, **scope_query})
    if not req:
        raise HTTPException(status_code=404, detail="WFH request not found")

    if req["status"] != "pending":
         raise HTTPException(status_code=400, detail=f"Request is already {req['status']}")

    await wfh_requests_collection.update_one(
        {"_id": obj_id},
        {
            "$set": {
                "status": "rejected",
                "rejection_reason": reason,
                "approved_by": admin.email,
                "approved_at": datetime.now(timezone.utc)
            }
        }
    )
    return {"status": "success", "message": "WFH request rejected"}


@router.get("/admin/wfh/calendar")
async def get_wfh_calendar(
    month: str,  # YYYY-MM
    admin: Admin = Depends(require_feature("wfh_management"))
):
    """
    Get WFH calendar data showing who is approved/pending WFH for each day of the month.
    """
    scope_query = await _build_scoped_wfh_request_query(admin)
    query = {
        **scope_query,
        "date": {"$regex": f"^{month}"},
        "status": {"$in": ["approved", "pending"]}
    }
    requests = await wfh_requests_collection.find(query).to_list(length=1000)

    # Group by date
    calendar_data = {}
    for r in requests:
        date_str = r["date"]
        if date_str not in calendar_data:
            calendar_data[date_str] = []
        calendar_data[date_str].append({
            "request_id": str(r["_id"]),
            "employee_email": r["employee_email"],
            "employee_name": r["employee_name"],
            "status": r["status"]
        })

    return calendar_data


@router.get("/admin/wfh/request-stats")
@router.get("/admin/wfh-requests/stats")
async def get_wfh_stats(admin: Admin = Depends(require_feature("wfh_management"))):
    """Get dashboard stats for WFH management."""
    org_id = admin.organization_id
    s_doc = await settings_collection.find_one({"$or": [{"organization_id": org_id}, {"id": "config"}]}) if org_id else None
    tz_offset = int((s_doc or {}).get("timezone_offset", 330))
    today_str = (datetime.now(timezone.utc) + timedelta(minutes=tz_offset)).strftime("%Y-%m-%d")

    scope_query = await _build_scoped_wfh_request_query(admin)
    emp_filter = get_employee_filter(admin)

    # Counts
    today_wfh_approved = await wfh_requests_collection.count_documents({
        **scope_query,
        "date": today_str,
        "status": "approved"
    })

    pending_requests = await wfh_requests_collection.count_documents({
        **scope_query,
        "status": "pending"
    })

    # Permanent WFH count
    permanent_wfh = await employees_collection.count_documents({
        **emp_filter,
        "employee_type": "wfh"
    })

    # Approved this month (matching current YYYY-MM)
    current_month_str = today_str[:7]
    approved_this_month = await wfh_requests_collection.count_documents({
        **scope_query,
        "date": {"$regex": f"^{current_month_str}"},
        "status": "approved"
    })

    # Get list of today's WFH employees (including permanent and approved requests)
    today_wfh_list = []
    # 1. Approved requests
    approved_reqs = await wfh_requests_collection.find({
        **scope_query,
        "date": today_str,
        "status": "approved"
    }).to_list(length=100)
    
    for r in approved_reqs:
        today_wfh_list.append({
            "email": r["employee_email"],
            "name": r["employee_name"],
            "wfh_type": "approved_request"
        })

    # 2. Permanent WFH employees
    perm_emps = await employees_collection.find({
        **emp_filter,
        "employee_type": "wfh"
    }, {"email": 1, "full_name": 1}).to_list(length=100)

    # Avoid duplicates
    existing_emails = {x["email"] for x in today_wfh_list}
    for e in perm_emps:
        if e["email"] not in existing_emails:
            today_wfh_list.append({
                "email": e["email"],
                "name": e.get("full_name", ""),
                "wfh_type": "permanent"
            })

    return {
        "today_wfh_count": len(today_wfh_list),
        "pending_requests_count": pending_requests,
        "permanent_wfh_count": permanent_wfh,
        "approved_this_month_count": approved_this_month,
        "today_wfh_list": today_wfh_list
    }


@router.get("/admin/wfh/policies")
async def get_wfh_policies(admin: Admin = Depends(require_feature("wfh_management"))):
    """List employees and their WFH policy settings."""
    employees = await employees_collection.find(
        get_employee_filter(admin),
        {"email": 1, "full_name": 1, "employee_type": 1, "allowed_wfh_days_per_month": 1, "department": 1, "designation": 1}
    ).to_list(length=1000)

    policy_list = []
    for emp in employees:
        policy_list.append({
            "employee_id": str(emp["_id"]),
            "email": emp["email"],
            "name": emp.get("full_name", ""),
            "department": emp.get("department", ""),
            "designation": emp.get("designation", ""),
            "wfh_type": emp.get("employee_type", "office"), # Default to office
            "allowed_days_per_month": emp.get("allowed_wfh_days_per_month", 8)
        })
    return policy_list


@router.put("/admin/wfh/policies/{employee_email}")
async def update_wfh_policy(
    employee_email: str,
    req_body: dict,
    admin: Admin = Depends(require_feature("wfh_management"))
):
    """Update employee's WFH type/policy."""
    email = employee_email.strip().lower()

    emp = await employees_collection.find_one({"email": email, **get_employee_filter(admin)})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    wfh_type = req_body.get("wfh_type")
    allowed_days = req_body.get("allowed_days_per_month", 8)

    if wfh_type not in ["office", "hybrid", "wfh", "field"]:
        raise HTTPException(status_code=400, detail="Invalid wfh_type. Must be office, hybrid, wfh, or field.")

    await employees_collection.update_one(
        {"_id": emp["_id"]},
        {
            "$set": {
                "employee_type": wfh_type,
                "allowed_wfh_days_per_month": allowed_days
            }
        }
    )

    return {"status": "success", "message": f"WFH policy updated for {email}"}
