"""Opaque, expiring, single-use state for DigiLocker callbacks."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from database import oauth_states_collection


async def create_oauth_state(employee_id, organization_id, code_verifier):
    state = secrets.token_urlsafe(32)
    await oauth_states_collection.insert_one({
        "_id": hashlib.sha256(state.encode()).hexdigest(), "employee_id": employee_id,
        "organization_id": organization_id, "code_verifier": code_verifier,
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10),
    })
    return state


async def consume_oauth_state(state):
    if not state or len(state) > 256:
        raise HTTPException(400, "Invalid OAuth state")
    record = await oauth_states_collection.find_one_and_delete({
        "_id": hashlib.sha256(state.encode()).hexdigest(),
        "expires_at": {"$gt": datetime.now(timezone.utc)},
    })
    if not record:
        raise HTTPException(400, "OAuth state is invalid, expired, or already used")
    return record
