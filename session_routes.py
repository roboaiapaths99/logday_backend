from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends
from auth import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_access_token,
    decode_access_token,
    employee_oauth2_scheme,
    resolve_access_token,
)
from database import auth_sessions_collection

router = APIRouter(tags=["Sessions"])


@router.post("/auth/logout")
async def logout(token: str = Depends(employee_oauth2_scheme)):
    await resolve_access_token(token)
    claims = decode_access_token(token)
    await auth_sessions_collection.update_one({"_id": claims["sid"]}, {"$set": {"revoked": True}})
    return {"status": "success"}


@router.post("/auth/refresh")
async def refresh_session(token: str = Depends(employee_oauth2_scheme)):
    """Extend an active, unrevoked session and issue a refreshed access token for long shifts."""
    account, kind = await resolve_access_token(token)
    claims = decode_access_token(token)
    sid = claims["sid"]
    new_expires_at = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    await auth_sessions_collection.update_one(
        {"_id": sid, "revoked": False},
        {"$set": {"expires_at": new_expires_at}},
    )
    new_token = create_access_token({
        "sub": str(account["_id"]),
        "account_type": kind,
        "sid": sid,
        "ver": account.get("auth_version", 0),
    })
    return {
        "access_token": new_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }
