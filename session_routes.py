from fastapi import APIRouter, Depends
from auth import employee_oauth2_scheme, resolve_access_token, decode_access_token
from database import auth_sessions_collection

router = APIRouter(tags=["Sessions"])


@router.post("/auth/logout")
async def logout(token: str = Depends(employee_oauth2_scheme)):
    await resolve_access_token(token)
    claims = decode_access_token(token)
    await auth_sessions_collection.update_one({"_id": claims["sid"]}, {"$set": {"revoked": True}})
    return {"status": "success"}
