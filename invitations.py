"""Employee invitations: secrets are returned once and only hashes are stored."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from pymongo import ReturnDocument

from database import employee_invitations_collection, organizations_collection
from models import Admin, EmployeeType
from permissions import require_feature

router = APIRouter(tags=["Employee invitations"])


class EmployeeInvitationRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    employee_id: str = Field(min_length=1, max_length=100)
    designation: str = Field(min_length=1, max_length=200)
    department: str = Field(min_length=1, max_length=200)
    employee_type: EmployeeType = EmployeeType.DESK
    expires_in_hours: int = Field(default=48, ge=1, le=168)
    organization_id: str | None = None


def invitation_digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@router.post("/admin/employee-invitations", status_code=201)
async def create_employee_invitation(req: EmployeeInvitationRequest,
                                     admin: Admin = Depends(require_feature("employees"))):
    org_id = admin.organization_id
    if req.organization_id:
        if admin.role != "superadmin" and req.organization_id != org_id:
            raise HTTPException(403, "Cannot invite an employee into another organization")
        org_id = req.organization_id
    if not ObjectId.is_valid(org_id) or not await organizations_collection.find_one({"_id": ObjectId(org_id)}):
        raise HTTPException(400, "A valid organization is required")
    email = req.email.strip().lower()
    if "@" not in email or any(c.isspace() for c in email):
        raise HTTPException(422, "Invalid email")
    token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=req.expires_in_hours)
    await employee_invitations_collection.insert_one({
        "_id": invitation_digest(token), "email": email, "organization_id": org_id,
        "employee_id": req.employee_id, "designation": req.designation,
        "department": req.department, "employee_type": req.employee_type.value,
        "expires_at": expires_at, "consumed": False, "issued_by": admin.email,
    })
    return {"invitation_token": token, "email": email, "expires_at": expires_at.isoformat()}


async def validate_invitation(token, email):
    if not token or len(token) > 256:
        raise HTTPException(403, "A valid employee invitation is required")
    query = {"_id": invitation_digest(token), "email": email.strip().lower(),
             "consumed": False, "expires_at": {"$gt": datetime.now(timezone.utc)}}
    invitation = await employee_invitations_collection.find_one(query)
    if not invitation:
        raise HTTPException(403, "Invitation is invalid, expired, or already used")
    return invitation


async def consume_invitation(token, email):
    # Atomic consumption prevents two concurrent registrations using one invitation.
    invitation = await employee_invitations_collection.find_one_and_update(
        {"_id": invitation_digest(token), "email": email.strip().lower(), "consumed": False,
         "expires_at": {"$gt": datetime.now(timezone.utc)}},
        {"$set": {"consumed": True, "consumed_at": datetime.now(timezone.utc)}},
        return_document=ReturnDocument.AFTER,
    )
    if not invitation:
        raise HTTPException(403, "Invitation is invalid, expired, or already used")
    return invitation
