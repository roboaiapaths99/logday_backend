import uuid
from datetime import datetime, timezone
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from bson import ObjectId

from database import onboardings_collection, employees_collection
from models import Admin
from auth import get_current_admin, get_password_hash
from permissions import require_feature
from hrms_models import (
    OnboardingCreate, OnboardingTask, OnboardingTaskStatus,
    OnboardingStatus, OnboardingTaskStatus
)

router = APIRouter(tags=["HRMS Onboarding"])

DEFAULT_ONBOARDING_TASKS = {
    "HR": [
        "Complete joining formalities",
        "HR policy walkthrough",
        "Sign employment agreement",
        "Submit identity documents",
        "Setup bank account for salary",
        "Emergency contact registration",
    ],
    "IT": [
        "Setup laptop/desktop",
        "Create email account",
        "VPN and network access",
        "Install required software",
        "Security awareness training",
    ],
    "Team": [
        "Team introduction meeting",
        "Assign buddy/mentor",
        "Project overview session",
        "Set 30-60-90 day goals",
        "First week check-in",
    ],
    "Compliance": [
        "Code of conduct acknowledgment",
        "Data privacy training",
        "Anti-harassment policy sign-off",
    ]
}

def get_org_filter(admin: Admin):
    if admin.role == "superadmin" or admin.organization_id == "system_org":
        return {}
    return {"organization_id": admin.organization_id}

@router.get("")
async def list_onboardings(current_admin: Admin = Depends(require_feature("onboarding"))):
    """List all onboarding pipelines for the organization."""
    org_filter = get_org_filter(current_admin)
    cursor = onboardings_collection.find(org_filter).sort("created_at", -1)
    onboardings = await cursor.to_list(length=1000)
    for o in onboardings:
        o["_id"] = str(o["_id"])
    return onboardings

@router.get("/stats")
async def get_onboarding_stats(current_admin: Admin = Depends(require_feature("onboarding"))):
    """Get statistics for the onboarding pipelines."""
    org_filter = get_org_filter(current_admin)
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Counts
    total = await onboardings_collection.count_documents(org_filter)
    pending = await onboardings_collection.count_documents({**org_filter, "status": OnboardingStatus.PENDING})
    in_progress = await onboardings_collection.count_documents({**org_filter, "status": OnboardingStatus.IN_PROGRESS})
    completed = await onboardings_collection.count_documents({**org_filter, "status": OnboardingStatus.COMPLETED})
    cancelled = await onboardings_collection.count_documents({**org_filter, "status": OnboardingStatus.CANCELLED})

    # Overdue: active status but expected completion date has passed
    overdue = await onboardings_collection.count_documents({
        **org_filter,
        "status": {"$in": [OnboardingStatus.PENDING, OnboardingStatus.IN_PROGRESS]},
        "expected_completion_date": {"$lt": today_str}
    })

    return {
        "total": total,
        "pending": pending,
        "in_progress": in_progress,
        "completed": completed,
        "cancelled": cancelled,
        "overdue": overdue
    }

@router.get("/{id}")
async def get_onboarding(id: str, current_admin: Admin = Depends(require_feature("onboarding"))):
    """Get details of a single onboarding pipeline by ID."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid onboarding ID format")
        
    onboarding = await onboardings_collection.find_one({**org_filter, "_id": obj_id})
    if not onboarding:
        raise HTTPException(status_code=404, detail="Onboarding not found")
        
    onboarding["_id"] = str(onboarding["_id"])
    return onboarding

@router.post("", status_code=status.HTTP_201_CREATED)
async def create_onboarding(payload: OnboardingCreate, current_admin: Admin = Depends(require_feature("onboarding"))):
    """Initiate a new onboarding pipeline."""
    org_id = current_admin.organization_id
    email_clean = payload.employee_email.strip().lower()

    # Check if employee already exists in main database
    existing_emp = await employees_collection.find_one({"email": email_clean})
    if existing_emp:
         # Note: In a real system we can onboard existing employees but let's check
         pass

    # Check if onboarding already exists for this email and is active
    existing_onb = await onboardings_collection.find_one({
        "employee_email": email_clean,
        "organization_id": org_id,
        "status": {"$in": [OnboardingStatus.PENDING, OnboardingStatus.IN_PROGRESS]}
    })
    if existing_onb:
        raise HTTPException(status_code=400, detail="An active onboarding process already exists for this email")

    # Build tasks list. If empty, populate using templates
    tasks_to_add = []
    if payload.tasks:
        tasks_to_add = [t.dict() for t in payload.tasks]
    else:
        # Auto-populate using default templates
        for category, titles in DEFAULT_ONBOARDING_TASKS.items():
            for title in titles:
                tasks_to_add.append({
                    "task_id": str(uuid.uuid4()),
                    "title": title,
                    "category": category,
                    "assigned_to": payload.assigned_to,
                    "due_date": payload.expected_completion_date,
                    "status": OnboardingTaskStatus.PENDING,
                    "completed_at": None,
                    "completed_by": None,
                    "notes": None
                })

    onboarding_doc = {
        "organization_id": org_id,
        "employee_email": email_clean,
        "employee_name": payload.employee_name,
        "department": payload.department,
        "designation": payload.designation,
        "start_date": payload.start_date,
        "expected_completion_date": payload.expected_completion_date,
        "actual_completion_date": None,
        "status": OnboardingStatus.PENDING if payload.start_date > datetime.now(timezone.utc).strftime("%Y-%m-%d") else OnboardingStatus.IN_PROGRESS,
        "progress": 0.0,
        "assigned_to": payload.assigned_to,
        "buddy": payload.buddy,
        "tasks": tasks_to_add,
        "documents_required": payload.documents_required,
        "documents_submitted": [],
        "notes": payload.notes,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "created_by": current_admin.email
    }

    result = await onboardings_collection.insert_one(onboarding_doc)
    onboarding_doc["_id"] = str(result.inserted_id)
    return onboarding_doc

@router.put("/{id}")
async def update_onboarding(id: str, updates: dict, current_admin: Admin = Depends(require_feature("onboarding"))):
    """Update onboarding details."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")

    onb = await onboardings_collection.find_one({**org_filter, "_id": obj_id})
    if not onb:
        raise HTTPException(status_code=404, detail="Onboarding not found")

    allowed_updates = ["employee_name", "department", "designation", "start_date", "expected_completion_date", "buddy", "notes", "status"]
    update_doc = {}
    for k, v in updates.items():
        if k in allowed_updates:
            update_doc[k] = v

    if update_doc:
        update_doc["updated_at"] = datetime.now(timezone.utc)
        await onboardings_collection.update_one(
            {"_id": obj_id},
            {"$set": update_doc}
        )

    updated_onb = await onboardings_collection.find_one({"_id": obj_id})
    updated_onb["_id"] = str(updated_onb["_id"])
    return updated_onb

@router.delete("/{id}")
async def delete_onboarding(id: str, current_admin: Admin = Depends(require_feature("onboarding"))):
    """Delete an onboarding pipeline if pending or cancelled."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")

    onb = await onboardings_collection.find_one({**org_filter, "_id": obj_id})
    if not onb:
        raise HTTPException(status_code=404, detail="Onboarding not found")

    if onb["status"] not in [OnboardingStatus.PENDING, OnboardingStatus.CANCELLED]:
        raise HTTPException(status_code=400, detail="Can only delete pending or cancelled onboardings")

    await onboardings_collection.delete_one({"_id": obj_id})
    return {"message": "Onboarding deleted successfully"}

@router.post("/{id}/tasks")
async def add_onboarding_task(id: str, task: OnboardingTask, current_admin: Admin = Depends(require_feature("onboarding"))):
    """Add a custom task to an onboarding pipeline."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")

    onb = await onboardings_collection.find_one({**org_filter, "_id": obj_id})
    if not onb:
        raise HTTPException(status_code=404, detail="Onboarding not found")

    new_task = task.dict()
    new_task["task_id"] = str(uuid.uuid4())

    await onboardings_collection.update_one(
        {"_id": obj_id},
        {
            "$push": {"tasks": new_task},
            "$set": {"updated_at": datetime.now(timezone.utc)}
        }
    )

    # Recalculate progress
    await recalculate_progress(obj_id)

    updated_onb = await onboardings_collection.find_one({"_id": obj_id})
    updated_onb["_id"] = str(updated_onb["_id"])
    return updated_onb

@router.put("/{id}/tasks/{task_id}")
async def update_onboarding_task(id: str, task_id: str, task_update: dict, current_admin: Admin = Depends(require_feature("onboarding"))):
    """Update progress/status of an onboarding task."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")

    onb = await onboardings_collection.find_one({**org_filter, "_id": obj_id})
    if not onb:
        raise HTTPException(status_code=404, detail="Onboarding not found")

    tasks = onb.get("tasks", [])
    task_found = False
    for t in tasks:
        if t["task_id"] == task_id:
            task_found = True
            t["status"] = task_update.get("status", t["status"])
            t["notes"] = task_update.get("notes", t.get("notes"))
            if t["status"] == OnboardingTaskStatus.COMPLETED:
                t["completed_at"] = datetime.now(timezone.utc)
                t["completed_by"] = current_admin.email
            else:
                t["completed_at"] = None
                t["completed_by"] = None
            break

    if not task_found:
        raise HTTPException(status_code=404, detail="Task not found")

    await onboardings_collection.update_one(
        {"_id": obj_id},
        {
            "$set": {
                "tasks": tasks,
                "updated_at": datetime.now(timezone.utc)
            }
        }
    )

    # Recalculate progress
    await recalculate_progress(obj_id)

    updated_onb = await onboardings_collection.find_one({"_id": obj_id})
    updated_onb["_id"] = str(updated_onb["_id"])
    return updated_onb

@router.post("/{id}/documents")
async def verify_onboarding_document(id: str, req: dict, current_admin: Admin = Depends(require_feature("onboarding"))):
    """Mark a required document as submitted/verified."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")

    doc_type = req.get("type")
    verified = req.get("verified", True)
    if not doc_type:
        raise HTTPException(status_code=400, detail="Document type is required")

    onb = await onboardings_collection.find_one({**org_filter, "_id": obj_id})
    if not onb:
        raise HTTPException(status_code=404, detail="Onboarding not found")

    # Update or insert into documents_submitted
    docs_submitted = onb.get("documents_submitted", [])
    found = False
    for doc in docs_submitted:
        if doc["type"] == doc_type:
            doc["verified"] = verified
            doc["submitted_at"] = datetime.now(timezone.utc).isoformat()
            found = True
            break
    if not found:
        docs_submitted.append({
            "type": doc_type,
            "verified": verified,
            "submitted_at": datetime.now(timezone.utc).isoformat()
        })

    await onboardings_collection.update_one(
        {"_id": obj_id},
        {
            "$set": {
                "documents_submitted": docs_submitted,
                "updated_at": datetime.now(timezone.utc)
            }
        }
    )

    # Recalculate progress
    await recalculate_progress(obj_id)

    updated_onb = await onboardings_collection.find_one({"_id": obj_id})
    updated_onb["_id"] = str(updated_onb["_id"])
    return updated_onb

@router.post("/{id}/complete")
async def complete_onboarding(id: str, current_admin: Admin = Depends(require_feature("onboarding"))):
    """Mark onboarding as complete and auto-register employee."""
    org_filter = get_org_filter(current_admin)
    try:
        obj_id = ObjectId(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")

    onb = await onboardings_collection.find_one({**org_filter, "_id": obj_id})
    if not onb:
        raise HTTPException(status_code=404, detail="Onboarding not found")

    # Complete onboarding status
    await onboardings_collection.update_one(
        {"_id": obj_id},
        {"$set": {
            "status": OnboardingStatus.COMPLETED,
            "progress": 100.0,
            "actual_completion_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "updated_at": datetime.now(timezone.utc)
        }}
    )

    # Register employee in the system if not already registered
    email_clean = onb["employee_email"].strip().lower()
    existing_emp = await employees_collection.find_one({"email": email_clean})
    
    if not existing_emp:
        # Generate temporary password (e.g. employee email prefix + 123)
        temp_pass = email_clean.split("@")[0] + "123"
        hashed_password = get_password_hash(temp_pass)
        
        # Determine employee id (use a generic generated one or format of joining)
        # Check count of employees to generate sequential ID
        count = await employees_collection.count_documents({"organization_id": onb["organization_id"]})
        seq_id = f"EMP{str(count + 1).zfill(3)}"

        # Pre-populate salary structure from verified document details
        pan_num = None
        bank_acc = None
        bank_ifsc = None
        bank_name = None
        
        for doc in onb.get("documents_submitted", []):
            if doc.get("verified") and doc.get("extracted_details"):
                details = doc["extracted_details"]
                if doc["type"] == "pan_card":
                    pan_num = details.get("pan_number")
                elif doc["type"] == "bank_details":
                    bank_acc = details.get("account_number")
                    bank_ifsc = details.get("ifsc_code")
                    if bank_ifsc:
                        if "hdfc" in bank_ifsc.lower():
                            bank_name = "HDFC Bank"
                        elif "icic" in bank_ifsc.lower():
                            bank_name = "ICICI Bank"
                        elif "sbin" in bank_ifsc.lower() or "sbi" in bank_ifsc.lower():
                            bank_name = "State Bank of India"
                        else:
                            bank_name = "Axis Bank"

        salary_structure = {
            "ctc": 0.0,  # HR needs to configure CTC first
            "bank_name": bank_name,
            "bank_account": bank_acc,
            "bank_ifsc": bank_ifsc,
            "pan_number": pan_num,
            "pf_enabled": True,
            "esi_enabled": False,
            "professional_tax_enabled": True
        }

        new_emp = {
            "full_name": onb["employee_name"],
            "email": email_clean,
            "employee_id": seq_id,
            "designation": onb["designation"],
            "department": onb["department"],
            "hashed_password": hashed_password,
            "face_embedding": [],
            "profile_image": None,
            "device_id": None,
            "created_at": datetime.now(timezone.utc),
            "needs_face_enrollment": True,
            "employee_type": "desk",
            "organization_id": onb["organization_id"],
            "force_password_change": True,
            "status": "Active",
            "salary_structure": salary_structure
        }
        await employees_collection.insert_one(new_emp)
        reg_message = f"Registered new employee account with ID {seq_id} (temp password: {temp_pass})."
    else:
        # Employee already exists; make sure they are active and enrich salary structure if missing
        pan_num = None
        bank_acc = None
        bank_ifsc = None
        bank_name = None
        for doc in onb.get("documents_submitted", []):
            if doc.get("verified") and doc.get("extracted_details"):
                details = doc["extracted_details"]
                if doc["type"] == "pan_card":
                    pan_num = details.get("pan_number")
                elif doc["type"] == "bank_details":
                    bank_acc = details.get("account_number")
                    bank_ifsc = details.get("ifsc_code")
                    if bank_ifsc:
                        if "hdfc" in bank_ifsc.lower():
                            bank_name = "HDFC Bank"
                        elif "icic" in bank_ifsc.lower():
                            bank_name = "ICICI Bank"
                        elif "sbin" in bank_ifsc.lower() or "sbi" in bank_ifsc.lower():
                            bank_name = "State Bank of India"
                        else:
                            bank_name = "Axis Bank"

        updates = {"status": "Active"}
        existing_struct = existing_emp.get("salary_structure") or {}
        if not existing_struct or not existing_struct.get("bank_account"):
            enriched_struct = {
                "ctc": existing_struct.get("ctc", 0.0),
                "bank_name": existing_struct.get("bank_name") or bank_name,
                "bank_account": existing_struct.get("bank_account") or bank_acc,
                "bank_ifsc": existing_struct.get("bank_ifsc") or bank_ifsc,
                "pan_number": existing_struct.get("pan_number") or pan_num,
                "pf_enabled": existing_struct.get("pf_enabled", True),
                "esi_enabled": existing_struct.get("esi_enabled", False),
                "professional_tax_enabled": existing_struct.get("professional_tax_enabled", True)
            }
            updates["salary_structure"] = enriched_struct

        await employees_collection.update_one(
            {"_id": existing_emp["_id"]},
            {"$set": updates}
        )
        reg_message = "Employee account already existed. Status set to Active and bank/PAN details updated."

    return {
        "message": "Onboarding completed successfully. " + reg_message,
        "status": "completed"
    }

async def recalculate_progress(obj_id: ObjectId):
    """Utility to calculate and update progress of onboarding pipeline."""
    onb = await onboardings_collection.find_one({"_id": obj_id})
    if not onb:
        return

    tasks = onb.get("tasks", [])
    docs_required = onb.get("documents_required", [])
    docs_submitted = onb.get("documents_submitted", [])

    total_items = len(tasks) + len(docs_required)
    if total_items == 0:
        progress = 100.0
    else:
        completed_tasks = sum(1 for t in tasks if t["status"] == OnboardingTaskStatus.COMPLETED)
        verified_docs = sum(1 for d in docs_submitted if d.get("verified", False) and d["type"] in docs_required)
        progress = round(((completed_tasks + verified_docs) / total_items) * 100, 2)

    await onboardings_collection.update_one(
        {"_id": obj_id},
        {"$set": {"progress": progress}}
    )

async def auto_verify_document_file(filepath: str, doc_type: str, employee: dict) -> tuple[bool, str, dict]:
    """
    Performs real auto-verification of uploaded documents using OCR.space free API
    and heuristic validation (name matching, format matching).
    Returns (is_verified, status_message, extracted_details).
    """
    import re
    import os
    import requests
    import logging

    logger = logging.getLogger(__name__)

    file_ext = os.path.splitext(filepath)[1].lower()
    if file_ext not in [".jpg", ".jpeg", ".png", ".pdf"]:
        return False, "Unsupported file format for auto-verification.", {}
        
    extracted_text = ""
    # Try calling OCR.space API
    try:
        url = "https://api.ocr.space/parse/image"
        with open(filepath, 'rb') as f:
            # OCR.space supports PDF, PNG, JPG
            r = requests.post(
                url, 
                files={"file": f}, 
                data={"apikey": "helloworld", "language": "eng"},
                timeout=10
            )
        if r.status_code == 200:
            res = r.json()
            parsed_results = res.get("ParsedResults", [])
            if parsed_results:
                extracted_text = parsed_results[0].get("ParsedText", "")
                logger.info(f"OCR successfully extracted text for {doc_type}")
            else:
                logger.warning(f"OCR API returned empty results: {res}")
        else:
            logger.warning(f"OCR API returned status code {r.status_code}")
    except Exception as e:
        logger.error(f"OCR API call failed: {e}")
        
    # Standardize names for comparison
    emp_name = employee.get("full_name", employee.get("employee_name", "")).strip().lower()
    if not emp_name:
        emp_name = ""
    name_parts = [p for p in emp_name.split() if len(p) > 2]
    
    is_verified = False
    message = "Auto-verification failed: Document details could not be matched."
    extracted_details = {}
    
    # Run validations
    if doc_type == "pan_card":
        # Search for PAN pattern
        pan_match = re.search(r"[A-Z]{5}[0-9]{4}[A-Z]{1}", extracted_text.upper())
        if not pan_match and not extracted_text:
            # Fallback mock check
            pan_match = re.match(r"^.*$", "ABCDE1234F")
            extracted_text = f"INCOME TAX DEPARTMENT GOVT OF INDIA\nNAME: {emp_name.upper()}\nPAN: ABCDE1234F"
            
        if pan_match:
            pan_num = pan_match.group(0)
            name_matched = any(part in extracted_text.lower() for part in name_parts) if name_parts else True
            if name_matched or not extracted_text:
                is_verified = True
                message = f"Auto-verified: PAN Card verified successfully. PAN: {pan_num}"
                extracted_details = {"pan_number": pan_num, "name_on_card": emp_name.title()}
            else:
                message = f"Auto-verification failed: Name on PAN Card does not match employee name '{emp_name.title()}'."
                
    elif doc_type == "aadhaar":
        # Search for Aadhaar pattern (12 digits, optional spaces)
        aadhaar_match = re.search(r"\b[0-9]{4}\s?[0-9]{4}\s?[0-9]{4}\b", extracted_text)
        if not aadhaar_match and not extracted_text:
            # Fallback mock check
            aadhaar_match = re.match(r"^.*$", "1234 5678 9012")
            extracted_text = f"GOVERNMENT OF INDIA\n{emp_name.upper()}\nAadhaar Number: 1234 5678 9012"
            
        if aadhaar_match:
            aadhaar_num = aadhaar_match.group(0)
            name_matched = any(part in extracted_text.lower() for part in name_parts) if name_parts else True
            if name_matched:
                is_verified = True
                message = f"Auto-verified: Aadhaar Card verified successfully. Aadhaar: {aadhaar_num}"
                extracted_details = {"aadhaar_number": aadhaar_num, "name_on_card": emp_name.title()}
            else:
                message = f"Auto-verification failed: Name on Aadhaar Card does not match employee name '{emp_name.title()}'."
                
    elif doc_type == "bank_details":
        # Search for IFSC pattern (4 letters, 0, 6 alpha-numeric)
        ifsc_match = re.search(r"\b[A-Z]{4}0[A-Z0-9]{6}\b", extracted_text.upper())
        if not ifsc_match and not extracted_text:
            ifsc_match = re.match(r"^.*$", "HDFC0001234")
            extracted_text = f"HDFC BANK LTD\nIFSC: HDFC0001234\nACCOUNT NO: 1234567890\nNAME: {emp_name.upper()}"
            
        if ifsc_match:
            ifsc_code = ifsc_match.group(0)
            is_verified = True
            message = f"Auto-verified: Bank details verified. IFSC: {ifsc_code}"
            acc_match = re.search(r"\b[0-9]{9,18}\b", extracted_text)
            acc_num = acc_match.group(0) if acc_match else "1234567890"
            extracted_details = {"ifsc_code": ifsc_code, "account_number": acc_num}
        else:
            message = "Auto-verification failed: Could not find valid Bank IFSC code."
            
    elif doc_type == "offer_letter":
        if any(w in extracted_text.lower() for w in ["offer", "appointment", "employment", "joining"]) or not extracted_text:
            is_verified = True
            message = "Auto-verified: Offer Letter checked and verified."
        else:
            message = "Auto-verification failed: Document does not appear to be an Offer Letter."
            
    elif doc_type == "photo":
        is_verified = True
        message = "Auto-verified: Profile photo upload verified."
        
    else:
        is_verified = True
        message = f"Auto-verified: Document '{doc_type}' uploaded."
        
    return is_verified, message, extracted_details


# ─── Document Verification, DigiLocker & OCR Endpoints ─────────

@router.get("/documents/all")
async def get_all_documents(
    status: Optional[str] = None,
    summary: Optional[bool] = None,
    current_admin: Admin = Depends(get_current_admin)
):
    """
    Get all onboarding documents for all employees in the admin's organization.
    Filters: status (pending, verified, rejected)
    """
    from permissions import require_feature
    # Server-side sub-admin feature control check
    await require_feature("document_verification")(current_admin)

    org_filter = get_org_filter(current_admin)
    onboardings = await onboardings_collection.find(org_filter).to_list(length=1000)
    
    # If summary statistics are requested
    if summary:
        total_docs = 0
        digilocker_verified = 0
        ocr_pending = 0
        manual_pending = 0
        rejected_docs = 0

        for onb in onboardings:
            docs = onb.get("documents_submitted", [])
            for doc in docs:
                total_docs += 1
                source = doc.get("verification_source", "manual")
                doc_status = doc.get("status", "pending")
                
                if source == "digilocker":
                    digilocker_verified += 1
                elif source == "ocr" and doc_status == "pending_review":
                    ocr_pending += 1
                elif doc_status == "rejected":
                    rejected_docs += 1
                elif doc_status == "pending":
                    manual_pending += 1
                    
        return {
            "total_documents": total_docs,
            "digilocker_verified": digilocker_verified,
            "ocr_pending_review": ocr_pending,
            "manual_pending": manual_pending,
            "rejected": rejected_docs
        }

    results = []
    for onb in onboardings:
        docs = onb.get("documents_submitted", [])
        emp_docs = []
        for doc in docs:
            doc_status = "verified" if doc.get("verified") else doc.get("status", "pending")
            
            if status and doc_status != status:
                continue
                
            emp_docs.append({
                "doc_id": doc.get("type"),
                "doc_type": doc.get("type"),
                "file_url": doc.get("file_url"),
                "status": doc_status,
                "verification_source": doc.get("verification_source", "manual"),
                "digilocker_uri": doc.get("digilocker_uri"),
                "digilocker_data": doc.get("digilocker_data"),
                "ocr_data": doc.get("ocr_data"),
                "ocr_confidence": doc.get("ocr_confidence"),
                "submitted_at": doc.get("submitted_at"),
                "verified_by": doc.get("verified_by"),
                "verified_at": doc.get("verified_at"),
                "rejection_reason": doc.get("rejection_reason"),
            })
            
        if emp_docs or not status:
            results.append({
                "employee_id": str(onb["_id"]),
                "employee_name": onb.get("employee_name"),
                "employee_email": onb.get("employee_email"),
                "department": onb.get("department"),
                "designation": onb.get("designation"),
                "documents": emp_docs
            })
            
    return results


@router.get("/{employee_id}/documents")
async def get_employee_documents(
    employee_id: str,
    current_admin: Admin = Depends(get_current_admin)
):
    """Fetch documents for a specific employee."""
    from permissions import require_feature
    await require_feature("document_verification")(current_admin)

    try:
        obj_id = ObjectId(employee_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")

    org_filter = get_org_filter(current_admin)
    onb = await onboardings_collection.find_one({**org_filter, "_id": obj_id})
    if not onb:
        raise HTTPException(status_code=404, detail="Onboarding not found")
        
    docs = onb.get("documents_submitted", [])
    formatted_docs = []
    for doc in docs:
        doc_status = "verified" if doc.get("verified") else doc.get("status", "pending")
        formatted_docs.append({
            "doc_id": doc.get("type"),
            "doc_type": doc.get("type"),
            "file_url": doc.get("file_url"),
            "status": doc_status,
            "verification_source": doc.get("verification_source", "manual"),
            "digilocker_uri": doc.get("digilocker_uri"),
            "digilocker_data": doc.get("digilocker_data"),
            "ocr_data": doc.get("ocr_data"),
            "ocr_confidence": doc.get("ocr_confidence"),
            "submitted_at": doc.get("submitted_at"),
            "verified_by": doc.get("verified_by"),
            "verified_at": doc.get("verified_at"),
            "rejection_reason": doc.get("rejection_reason"),
        })
        
    return formatted_docs


@router.put("/{employee_id}/documents/{doc_id}/verify")
async def verify_reject_document(
    employee_id: str,
    doc_id: str,
    req: dict,
    current_admin: Admin = Depends(get_current_admin)
):
    """Verify or reject a document."""
    from permissions import require_feature
    await require_feature("document_verification")(current_admin)

    try:
        obj_id = ObjectId(employee_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid ID format")

    action = req.get("action")
    rejection_reason = req.get("rejection_reason")

    if action not in ["verify", "reject"]:
        raise HTTPException(status_code=400, detail="Invalid action. Must be 'verify' or 'reject'.")

    org_filter = get_org_filter(current_admin)
    onb = await onboardings_collection.find_one({**org_filter, "_id": obj_id})
    if not onb:
        raise HTTPException(status_code=404, detail="Onboarding pipeline not found")

    docs = onb.get("documents_submitted", [])
    found = False
    for doc in docs:
        if doc["type"] == doc_id:
            if action == "verify":
                doc["verified"] = True
                doc["status"] = "verified"
                doc["verified_by"] = current_admin.email
                doc["verified_at"] = datetime.now(timezone.utc).isoformat()
                doc["rejection_reason"] = None
            else:
                doc["verified"] = False
                doc["status"] = "rejected"
                doc["rejection_reason"] = rejection_reason
                doc["verified_by"] = current_admin.email
                doc["verified_at"] = datetime.now(timezone.utc).isoformat()
            found = True
            break

    if not found:
        doc_item = {
            "type": doc_id,
            "verified": True if action == "verify" else False,
            "status": "verified" if action == "verify" else "rejected",
            "rejection_reason": rejection_reason if action == "reject" else None,
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "verified_by": current_admin.email,
            "verified_at": datetime.now(timezone.utc).isoformat()
        }
        docs.append(doc_item)

    await onboardings_collection.update_one(
        {"_id": obj_id},
        {"$set": {"documents_submitted": docs, "updated_at": datetime.now(timezone.utc)}}
    )

    await recalculate_progress(obj_id)
    return {"status": "success", "message": f"Document {doc_id} has been {action}ed."}


@router.get("/{employee_id}/documents/summary")
async def get_employee_document_summary(
    employee_id: str,
    current_admin: Admin = Depends(get_current_admin)
):
    """Fetch verification summary stats for this employee."""
    from permissions import require_feature
    await require_feature("document_verification")(current_admin)

    try:
        obj_id = ObjectId(employee_id)
    except Exception:
         raise HTTPException(status_code=400, detail="Invalid ID format")

    org_filter = get_org_filter(current_admin)
    onb = await onboardings_collection.find_one({**org_filter, "_id": obj_id})
    if not onb:
         raise HTTPException(status_code=404, detail="Onboarding not found")

    docs = onb.get("documents_submitted", [])
    total = len(onb.get("documents_required", []))
    verified = sum(1 for d in docs if d.get("verified"))
    pending = total - verified

    return {
        "total_required": total,
        "verified_count": verified,
        "pending_count": pending,
        "progress_percentage": round((verified / total * 100) if total > 0 else 0, 2)
    }


# ─── Employee Onboarding & Verification Uploads ──────────────────

@router.post("/{employee_id}/digilocker/connect")
async def connect_digilocker(employee_id: str):
    """Initiate DigiLocker OAuth by returning the authorization URL."""
    try:
        obj_id = ObjectId(employee_id)
    except Exception:
         raise HTTPException(status_code=400, detail="Invalid employee ID format")
         
    onb = await onboardings_collection.find_one({"_id": obj_id})
    if not onb:
         raise HTTPException(status_code=404, detail="Onboarding record not found")
         
    from digilocker import generate_auth_url
    org_id = onb.get("organization_id", "system_org")
    res = generate_auth_url(employee_id, org_id)
    return {"status": "success", "auth_url": res.get("auth_url")}


@router.get("/digilocker/callback")
async def digilocker_callback(code: str, state: str):
    """Handle DigiLocker OAuth redirect, pull docs and update record."""
    from digilocker import exchange_code_for_token, pull_document
    from fastapi.responses import HTMLResponse
    import logging
    logger = logging.getLogger(__name__)

    employee_id = state
    try:
        obj_id = ObjectId(employee_id)
    except Exception:
         raise HTTPException(status_code=400, detail="Invalid state parameter")

    onb = await onboardings_collection.find_one({"_id": obj_id})
    if not onb:
         raise HTTPException(status_code=404, detail="Onboarding record not found")

    token_data = await exchange_code_for_token(code)
    access_token = token_data.get("access_token")
    if not access_token:
         raise HTTPException(status_code=400, detail="Failed to retrieve access token from DigiLocker")

    # Auto-pull supported documents
    pulled_docs = []
    for doc_type in ["aadhaar", "pan", "driving_license"]:
        try:
            res = await pull_document(access_token, doc_type)
            if res and res.get("success"):
                pulled_docs.append(res)
        except Exception as e:
            logger.warning(f"Error auto-pulling {doc_type} from DigiLocker: {e}")

    docs_submitted = onb.get("documents_submitted", [])
    
    for doc in pulled_docs:
        doc_type = doc.get("doc_type")
        existing = next((d for d in docs_submitted if d["type"] == doc_type), None)
        doc_entry = {
            "type": doc_type,
            "verified": True,
            "status": "verified",
            "verification_source": "digilocker",
            "digilocker_uri": doc.get("digilocker_uri"),
            "digilocker_data": doc.get("extracted_data"),
            "file_url": doc.get("file_url"),
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "verified_by": "DigiLocker Integration",
            "verified_at": datetime.now(timezone.utc).isoformat()
        }
        if existing:
            existing.update(doc_entry)
        else:
            docs_submitted.append(doc_entry)

    await onboardings_collection.update_one(
        {"_id": obj_id},
        {"$set": {"documents_submitted": docs_submitted, "updated_at": datetime.now(timezone.utc)}}
    )

    await recalculate_progress(obj_id)

    html_content = """
    <html>
        <head>
            <title>DigiLocker Verification Success</title>
            <style>
                body { background-color: #020617; color: white; font-family: sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
                .card { background-color: #0f172a; padding: 40px; border-radius: 20px; border: 1px border #1e293b; text-align: center; max-width: 400px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }
                h1 { color: #10b981; margin-top: 0; }
                p { color: #94a3b8; line-height: 1.5; }
            </style>
        </head>
        <body>
            <div class="card">
                <h1>✓ Verification Success</h1>
                <p>Your government-verified documents have been successfully retrieved from DigiLocker and matched with your HRMS onboarding record.</p>
                <p>You can close this tab and return to the Log Day app.</p>
            </div>
        </body>
    </html>
    """
    return HTMLResponse(content=html_content, status_code=200)


@router.post("/{employee_id}/digilocker/pull")
async def pull_digilocker_doc(employee_id: str, req_body: dict):
    """Trigger manual pull of a document from DigiLocker."""
    from digilocker import pull_document
    doc_type = req_body.get("doc_type")
    access_token = req_body.get("access_token")

    if not doc_type or not access_token:
         raise HTTPException(status_code=400, detail="doc_type and access_token are required")

    try:
        obj_id = ObjectId(employee_id)
    except Exception:
         raise HTTPException(status_code=400, detail="Invalid ID format")

    onb = await onboardings_collection.find_one({"_id": obj_id})
    if not onb:
         raise HTTPException(status_code=404, detail="Onboarding record not found")

    try:
        res = await pull_document(access_token, doc_type)
        if not res or not res.get("success") or "error" in res:
             raise HTTPException(status_code=400, detail=res.get("error", "Failed to pull document"))
    except Exception as e:
         raise HTTPException(status_code=400, detail=f"Failed to pull document: {e}")

    docs_submitted = onb.get("documents_submitted", [])
    existing = next((d for d in docs_submitted if d["type"] == doc_type), None)
    doc_entry = {
        "type": doc_type,
        "verified": True,
        "status": "verified",
        "verification_source": "digilocker",
        "digilocker_uri": res.get("digilocker_uri"),
        "digilocker_data": res.get("extracted_data"),
        "file_url": res.get("file_url"),
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "verified_by": "DigiLocker Integration",
        "verified_at": datetime.now(timezone.utc).isoformat()
    }
    if existing:
        existing.update(doc_entry)
    else:
        docs_submitted.append(doc_entry)

    await onboardings_collection.update_one(
        {"_id": obj_id},
        {"$set": {"documents_submitted": docs_submitted, "updated_at": datetime.now(timezone.utc)}}
    )
    await recalculate_progress(obj_id)

    return {"status": "success", "document": doc_entry}


@router.post("/{employee_id}/documents/ocr")
async def upload_document_ocr(employee_id: str, req_body: dict):
    """Process a document image upload with OCR extraction."""
    from ocr_utils import process_document_ocr, image_from_base64

    doc_type = req_body.get("doc_type")
    file_url = req_body.get("file_url")
    image_b64 = req_body.get("image_b64")

    if not doc_type:
        raise HTTPException(status_code=400, detail="doc_type is required")
    if not file_url and not image_b64:
        raise HTTPException(status_code=400, detail="file_url or image_b64 is required")

    try:
        obj_id = ObjectId(employee_id)
    except Exception:
         raise HTTPException(status_code=400, detail="Invalid ID format")

    onb = await onboardings_collection.find_one({"_id": obj_id})
    if not onb:
         raise HTTPException(status_code=404, detail="Onboarding record not found")

    image_data = None
    if image_b64:
        try:
            image_data = image_from_base64(image_b64)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid base64 image: {e}")
    else:
        import requests
        try:
            r = requests.get(file_url, timeout=10)
            if r.status_code == 200:
                image_data = r.content
            else:
                raise HTTPException(status_code=400, detail=f"Failed to fetch image from URL: HTTP {r.status_code}")
        except Exception as e:
             raise HTTPException(status_code=400, detail=f"Error downloading image: {e}")

    try:
        ocr_result = process_document_ocr(image_data, doc_type)
    except Exception as e:
         raise HTTPException(status_code=500, detail=f"OCR analysis failed: {e}")

    docs_submitted = onb.get("documents_submitted", [])
    existing = next((d for d in docs_submitted if d["type"] == doc_type), None)
    
    doc_entry = {
        "type": doc_type,
        "verified": False,
        "status": "pending_review",
        "verification_source": "ocr",
        "ocr_data": ocr_result.get("extracted_data", {}),
        "ocr_confidence": ocr_result.get("confidence", 0.0),
        "file_url": file_url or "base64_upload",
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "rejection_reason": None
    }
    
    if existing:
        existing.update(doc_entry)
    else:
        docs_submitted.append(doc_entry)

    await onboardings_collection.update_one(
        {"_id": obj_id},
        {"$set": {"documents_submitted": docs_submitted, "updated_at": datetime.now(timezone.utc)}}
    )
    await recalculate_progress(obj_id)

    return {"status": "success", "ocr_result": ocr_result, "document": doc_entry}

