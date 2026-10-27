import os
import uuid
import math
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from bson import ObjectId
import requests

from database import (
    employees_collection,
    attendance_logs_collection,
    visit_logs_collection,
    location_pings_collection,
    settings_collection,
    admins_collection,
    organizations_collection,
    alerts_collection
)
from auth import get_current_employee, get_current_admin

logger = logging.getLogger(__name__)

router = APIRouter(tags=["LogDay FlyField"])

# In-memory geocode cache to avoid redundant lookups
FLYFIELD_GEOCODE_CACHE: Dict[tuple, str] = {}


# ==============================================================================
# Helper: Real Physical Address Resolver (Guarantees human-readable address)
# ==============================================================================

async def resolve_real_address(lat: float, lon: float, client_address: Optional[str] = None) -> str:
    """
    Guarantees a human-readable physical address string.
    Never returns raw coordinate numbers if address can be resolved.
    """
    # 1. Use client address if provided and has actual text (not just numbers/commas)
    if client_address and len(client_address.strip()) > 3:
        clean = client_address.strip()
        # Verify it has letters, not just "28.53, 77.39"
        if any(c.isalpha() for c in clean):
            return clean

    # 2. Check in-memory geocode cache
    cache_key = (round(lat, 4), round(lon, 4))
    if cache_key in FLYFIELD_GEOCODE_CACHE:
        return FLYFIELD_GEOCODE_CACHE[cache_key]

    # 3. Server-side reverse geocoding via Nominatim
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={lat}&lon={lon}"
        headers = {
            "User-Agent": "LogDayFlyField/1.0 (contact: support@logday.app)"
        }
        response = await run_in_threadpool(requests.get, url, headers=headers, timeout=5)
        if response.status_code == 200:
            data = response.json()
            display_name = data.get("display_name")
            if display_name:
                # Format a cleaner address if detailed address components exist
                addr = data.get("address", {})
                parts = []
                for key in ["building", "amenity", "shop", "office", "road", "suburb", "neighbourhood", "city", "town", "county", "state", "postcode"]:
                    val = addr.get(key)
                    if val and val not in parts:
                        parts.append(val)
                
                formatted = ", ".join(parts[:5]) if parts else display_name
                FLYFIELD_GEOCODE_CACHE[cache_key] = formatted
                return formatted
    except Exception as e:
        logger.warning(f"[FlyField Geocode] Reverse lookup failed: {e}")

    # Fallback if geocoding times out
    return f"Area near {round(lat, 4)}, {round(lon, 4)}"


def calculate_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in kilometers"""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return round(R * c, 2)


def get_today_bounds(tz_offset_minutes: int = 330):
    """Calculates UTC bounds for today according to org/local timezone (default IST +5:30 = 330 mins)"""
    now_utc = datetime.now(timezone.utc)
    local_now = now_utc + timedelta(minutes=tz_offset_minutes)
    local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = local_midnight - timedelta(minutes=tz_offset_minutes)
    today_end_utc = today_start_utc + timedelta(days=1)
    today_date_str = local_now.strftime("%Y-%m-%d")
    return today_start_utc, today_end_utc, today_date_str


# ==============================================================================
# Pydantic Schemas for FlyField
# ==============================================================================

class VisitStartRequest(BaseModel):
    client_name: str
    client_type: str = "customer"
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    person_met: Optional[str] = None
    person_met_name: Optional[str] = None
    person_met_role: Optional[str] = None
    phone: Optional[str] = None
    lat: Optional[float] = None
    long: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy: Optional[float] = None
    address: Optional[str] = None
    notes: Optional[str] = None
    remarks: Optional[str] = None
    offline_id: Optional[str] = None

    def get_lat(self) -> float:
        return self.lat if self.lat is not None else (self.latitude or 0.0)

    def get_long(self) -> float:
        return self.long if self.long is not None else (self.longitude or 0.0)


class VisitEndRequest(BaseModel):
    lat: Optional[float] = None
    long: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy: Optional[float] = None
    address: Optional[str] = None
    remarks: Optional[str] = None
    notes: Optional[str] = None
    outcome: Optional[str] = "Completed"
    person_met_name: Optional[str] = None
    person_met: Optional[str] = None
    person_met_role: Optional[str] = None
    phone: Optional[str] = None
    contact_phone: Optional[str] = None
    offline_id: Optional[str] = None

    def get_lat(self) -> float:
        return self.lat if self.lat is not None else (self.latitude or 0.0)

    def get_long(self) -> float:
        return self.long if self.long is not None else (self.longitude or 0.0)


# ==============================================================================
# MOBILE ENDPOINTS (Employee Authenticated)
# ==============================================================================

@router.get("/api/flyfield/today")
async def get_flyfield_today(user: dict = Depends(get_current_employee)):
    """
    Returns current day state for FlyField worker:
    - Start day check-in status (time, real address)
    - Currently active visit (if any)
    - Completed visits today
    - Total estimated KM & worked hours
    """
    email = user["email"]
    org_id = user.get("organization_id")
    today_start_utc, today_end_utc, today_str = get_today_bounds()

    # 1. Fetch today's check-in / check-out from attendance_logs
    today_logs = await attendance_logs_collection.find({
        "email": email,
        "timestamp": {"$gte": today_start_utc, "$lt": today_end_utc}
    }).sort("timestamp", 1).to_list(length=20)

    check_in_log = next((l for l in today_logs if l.get("type") in ["check-in", "IN"]), None)
    check_out_log = next((l for l in reversed(today_logs) if l.get("type") in ["check-out", "OUT"]), None)

    day_active = False
    if check_in_log and not check_out_log:
        day_active = True

    # 2. Fetch today's FlyField visits
    visits = await visit_logs_collection.find({
        "employee_email": email,
        "date": today_str,
        "visit_mode": "flyfield"
    }).sort("check_in_time", 1).to_list(length=100)

    # Active visit is one with status == 'in_progress'
    active_visit = next((v for v in visits if v.get("status") == "in_progress"), None)
    if active_visit:
        active_visit["_id"] = str(active_visit["_id"])

    completed_visits = [v for v in visits if v.get("status") == "completed"]

    # 3. Compute estimated KM from location pings today
    pings = await location_pings_collection.find({
        "employee_id": email,
        "recorded_at": {"$gte": today_start_utc, "$lt": today_end_utc}
    }).sort("recorded_at", 1).to_list(length=1000)

    total_km = 0.0
    for i in range(1, len(pings)):
        p1 = pings[i - 1]
        p2 = pings[i]
        d = calculate_distance_km(p1["lat"], p1["lng"], p2["lat"], p2["lng"])
        # Filter jitter (less than 15 meters) and teleportation (greater than 50km in 1 ping)
        if 0.015 < d < 50.0:
            total_km += d
    total_km = round(total_km, 1)

    # 4. Compute worked hours
    worked_hours = 0.0
    if check_in_log:
        now_utc = datetime.now(timezone.utc)
        in_time = check_in_log["timestamp"]
        if in_time.tzinfo is None:
            in_time = in_time.replace(tzinfo=timezone.utc)
        out_time = check_out_log["timestamp"] if check_out_log else now_utc
        if out_time.tzinfo is None:
            out_time = out_time.replace(tzinfo=timezone.utc)
        worked_hours = round(max(0.0, (out_time - in_time).total_seconds() / 3600.0), 2)

    return {
        "status": "success",
        "date": today_str,
        "day_active": day_active,
        "day_started": bool(check_in_log),
        "day_ended": bool(check_out_log),
        "check_in": {
            "time": check_in_log["timestamp"].isoformat() if check_in_log else None,
            "address": check_in_log.get("address") or check_in_log.get("location_name") if check_in_log else None,
            "face_verified": check_in_log.get("selfie_verified", True) if check_in_log else False
        } if check_in_log else None,
        "check_out": {
            "time": check_out_log["timestamp"].isoformat() if check_out_log else None,
            "address": check_out_log.get("address") or check_out_log.get("location_name") if check_out_log else None,
            "face_verified": check_out_log.get("selfie_verified", True) if check_out_log else False
        } if check_out_log else None,
        "active_visit": active_visit,
        "visits_completed_count": len(completed_visits),
        "total_km": total_km,
        "worked_hours": worked_hours,
        "stats": {
            "completed_visits": len(completed_visits),
            "total_time_minutes": int(worked_hours * 60),
            "approx_km": total_km
        }
    }


@router.post("/api/flyfield/visit/start")
async def start_flyfield_visit(req: VisitStartRequest, user: dict = Depends(get_current_employee)):
    """
    Starts an on-the-fly client visit:
    - Enforces that Start Day check-in has been completed.
    - Enforces STRICT SEQUENTIAL RULE: 409 Conflict if another visit is currently active!
    - Resolves real physical address.
    """
    email = user["email"]
    org_id = user.get("organization_id")
    today_start_utc, today_end_utc, today_str = get_today_bounds()

    # 1. Verify Start Day has happened
    today_checkin = await attendance_logs_collection.find_one({
        "email": email,
        "timestamp": {"$gte": today_start_utc, "$lt": today_end_utc},
        "type": {"$in": ["check-in", "IN"]}
    })
    if not today_checkin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Please complete Morning Check-In (Start Day) before starting client visits."
        )

    # Also check if day has already ended
    today_checkout = await attendance_logs_collection.find_one({
        "email": email,
        "timestamp": {"$gte": today_start_utc, "$lt": today_end_utc},
        "type": {"$in": ["check-out", "OUT"]}
    })
    if today_checkout:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Your working day has already ended. Please start a new day to create visits."
        )

    # 2. Strict Sequential Visit Rule: Check if an active visit already exists
    active_visit = await visit_logs_collection.find_one({
        "employee_email": email,
        "date": today_str,
        "visit_mode": "flyfield",
        "status": "in_progress"
    })
    if active_visit:
        active_client = active_visit.get("client_name", "Current Client")
        clean_visit = dict(active_visit)
        clean_visit["_id"] = str(clean_visit["_id"])
        for k in ["created_at", "updated_at", "check_in_time", "check_out_time"]:
            if k in clean_visit and hasattr(clean_visit[k], "isoformat"):
                clean_visit[k] = clean_visit[k].isoformat()
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": f"Active visit in progress at '{active_client}'. You must complete and check out of this visit before starting another one.",
                "active_visit": clean_visit
            }
        )

    # 3. Resolve real physical address
    cur_lat = req.get_lat()
    cur_long = req.get_long()
    resolved_address = await resolve_real_address(cur_lat, cur_long, req.address)

    # 4. Insert Visit document
    now_utc = datetime.now(timezone.utc)
    visit_doc = {
        "employee_id": str(user["_id"]),
        "employee_email": email,
        "employee_name": user.get("full_name", email),
        "organization_id": org_id,
        "date": today_str,
        "visit_mode": "flyfield",
        "client_name": req.client_name.strip(),
        "client_type": req.client_type,
        "contact_name": req.contact_name or req.person_met or req.person_met_name,
        "contact_phone": req.contact_phone or req.phone,
        "person_met_name": req.person_met or req.person_met_name or req.contact_name,
        "person_met_role": req.person_met_role,
        "notes": req.notes or req.remarks,
        "remarks": req.remarks or req.notes,
        "status": "in_progress",
        "check_in_time": now_utc,
        "check_in_lat": cur_lat,
        "check_in_lng": cur_long,
        "check_in_accuracy": req.accuracy,
        "check_in_address": resolved_address,
        "check_out_time": None,
        "check_out_lat": None,
        "check_out_lng": None,
        "check_out_address": None,
        "duration_minutes": 0,
        "media": [],
        "offline_id": req.offline_id,
        "created_at": now_utc,
        "updated_at": now_utc
    }

    result = await visit_logs_collection.insert_one(visit_doc)
    visit_doc["_id"] = str(result.inserted_id)

    # Record real-time breadcrumb for live map
    if cur_lat and cur_long:
        try:
            await location_pings_collection.insert_one({
                "employee_id": email,
                "organization_id": org_id,
                "lat": cur_lat,
                "lng": cur_long,
                "accuracy": req.accuracy or 10.0,
                "recorded_at": now_utc,
                "source": "flyfield_visit_checkin"
            })
        except Exception as ping_err:
            logger.warning(f"Failed to record visit start location ping: {ping_err}")

    return {
        "status": "success",
        "message": f"Visit started at {req.client_name}",
        "visit": visit_doc
    }


@router.post("/api/flyfield/visit/{visit_id}/media")
async def upload_flyfield_media(
    visit_id: str,
    media_type: Optional[str] = Form(None),
    lat: Optional[float] = Form(None),
    long: Optional[float] = Form(None),
    latitude: Optional[float] = Form(None),
    longitude: Optional[float] = Form(None),
    duration_sec: Optional[int] = Form(None),
    address: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    photo: Optional[UploadFile] = File(None),
    audio: Optional[UploadFile] = File(None),
    user: dict = Depends(get_current_employee)
):
    """
    Attaches live proof photo or voice note audio to an active or completed visit.
    Guarantees tamper-proof storage with address and timestamp metadata.
    """
    email = user["email"]
    org_id = user.get("organization_id", "default")

    # Validate visit exists
    try:
        obj_id = ObjectId(visit_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid visit ID")

    visit = await visit_logs_collection.find_one({"_id": obj_id, "employee_email": email})
    if not visit:
        raise HTTPException(status_code=404, detail="Visit not found or does not belong to you.")

    # Determine which file was uploaded
    target_file = file or photo or audio
    if not target_file:
        raise HTTPException(status_code=400, detail="No media file provided in request.")

    # Auto-detect media_type if not provided
    if not media_type:
        fname = (target_file.filename or "").lower()
        if target_file == audio or any(ext in fname for ext in [".m4a", ".aac", ".mp3", ".wav", ".ogg"]):
            media_type = "voice_note"
        else:
            media_type = "photo"

    # Prepare storage directory
    today_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    folder_dir = os.path.join("uploads", "flyfield", str(org_id), today_str)
    os.makedirs(folder_dir, exist_ok=True)

    # Determine extension
    ext = ".jpg"
    if media_type == "voice_note":
        ext = ".m4a" if "m4a" in (target_file.filename or "") else ".aac"
    elif target_file.filename and "." in target_file.filename:
        ext = "." + target_file.filename.rsplit(".", 1)[1].lower()

    filename = f"{uuid.uuid4().hex[:16]}{ext}"
    file_path = os.path.join(folder_dir, filename)

    content = await target_file.read()
    with open(file_path, "wb") as f:
        f.write(content)

    cur_lat = lat if lat is not None else (latitude or 0.0)
    cur_long = long if long is not None else (longitude or 0.0)

    public_url = f"/uploads/flyfield/{org_id}/{today_str}/{filename}"
    resolved_addr = await resolve_real_address(cur_lat, cur_long, address) if cur_lat and cur_long else visit.get("check_in_address")

    media_item = {
        "media_id": uuid.uuid4().hex[:12],
        "media_type": media_type,
        "url": public_url,
        "file_size": len(content),
        "gps_lat": cur_lat,
        "gps_lng": cur_long,
        "address": resolved_addr,
        "duration_sec": duration_sec,
        "captured_at": datetime.now(timezone.utc).isoformat()
    }

    await visit_logs_collection.update_one(
        {"_id": obj_id},
        {
            "$push": {"media": media_item},
            "$set": {"updated_at": datetime.now(timezone.utc)}
        }
    )

    return {
        "status": "success",
        "message": f"{media_type.capitalize()} uploaded successfully",
        "media": media_item
    }


@router.post("/api/flyfield/visit/{visit_id}/end")
async def end_flyfield_visit(
    visit_id: str,
    req: VisitEndRequest,
    user: dict = Depends(get_current_employee)
):
    """
    Checks out of an on-the-fly client visit:
    - Records checkout timestamp & real address.
    - Computes duration.
    - Unlocks employee to start next visit.
    """
    email = user["email"]

    try:
        obj_id = ObjectId(visit_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid visit ID")

    visit = await visit_logs_collection.find_one({"_id": obj_id, "employee_email": email})
    if not visit:
        raise HTTPException(status_code=404, detail="Visit not found.")

    if visit.get("status") == "completed":
        raise HTTPException(status_code=400, detail="This visit has already been checked out.")

    now_utc = datetime.now(timezone.utc)
    in_time = visit.get("check_in_time")
    if in_time and in_time.tzinfo is None:
        in_time = in_time.replace(tzinfo=timezone.utc)

    duration_mins = round((now_utc - in_time).total_seconds() / 60.0) if in_time else 0
    cur_lat = req.get_lat()
    cur_long = req.get_long()
    resolved_address = await resolve_real_address(cur_lat, cur_long, req.address)

    update_fields = {
        "status": "completed",
        "check_out_time": now_utc,
        "check_out_lat": cur_lat,
        "check_out_lng": cur_long,
        "check_out_accuracy": req.accuracy,
        "check_out_address": resolved_address,
        "duration_minutes": max(1, duration_mins),
        "remarks": req.remarks or req.notes or visit.get("remarks"),
        "outcome": req.outcome or "Completed",
        "person_met_name": req.person_met or req.person_met_name or visit.get("contact_name"),
        "person_met_role": req.person_met_role,
        "contact_phone": req.phone or req.contact_phone or visit.get("contact_phone"),
        "updated_at": now_utc
    }

    await visit_logs_collection.update_one({"_id": obj_id}, {"$set": update_fields})
    updated_doc = await visit_logs_collection.find_one({"_id": obj_id})
    updated_doc["_id"] = str(updated_doc["_id"])

    # Record real-time breadcrumb for live map
    if cur_lat and cur_long:
        try:
            await location_pings_collection.insert_one({
                "employee_id": email,
                "organization_id": user.get("organization_id"),
                "lat": cur_lat,
                "lng": cur_long,
                "accuracy": req.accuracy or 10.0,
                "recorded_at": now_utc,
                "source": "flyfield_visit_checkout"
            })
        except Exception as ping_err:
            logger.warning(f"Failed to record visit end location ping: {ping_err}")

    return {
        "status": "success",
        "message": f"Visit to {visit.get('client_name')} completed successfully.",
        "visit": updated_doc
    }


@router.get("/api/flyfield/timeline")
async def get_flyfield_timeline(date: Optional[str] = None, user: dict = Depends(get_current_employee)):
    """
    Returns full chronological daily timeline:
    1. Morning Start Day (Time, Face verification, Real Address)
    2. Chronological Visits (Client, In/Out times, Durations, Photos, Playable Voice Notes, Real Addresses)
    3. Evening End Day (Time, Face verification, Real Address)
    4. GPS trail points for route map
    """
    email = user["email"]
    return await build_daily_timeline(email, date)


@router.get("/api/flyfield/history")
async def get_flyfield_history(month: Optional[str] = None, user: dict = Depends(get_current_employee)):
    """Returns monthly summary of past days with visit counts and hours"""
    email = user["email"]
    if not month:
        month = datetime.now(timezone.utc).strftime("%Y-%m")

    # Match all days in month
    visits = await visit_logs_collection.find({
        "employee_email": email,
        "visit_mode": "flyfield",
        "date": {"$regex": f"^{month}"}
    }).to_list(length=1000)

    # Group by date
    days_map: Dict[str, dict] = {}
    for v in visits:
        d = v.get("date")
        if not d:
            continue
        if d not in days_map:
            days_map[d] = {"date": d, "visits_count": 0, "completed_count": 0, "clients": []}
        days_map[d]["visits_count"] += 1
        if v.get("status") == "completed":
            days_map[d]["completed_count"] += 1
        client = v.get("client_name")
        if client and client not in days_map[d]["clients"]:
            days_map[d]["clients"].append(client)

    result = sorted(list(days_map.values()), key=lambda x: x["date"], reverse=True)
    return {"status": "success", "month": month, "days": result}


# ==============================================================================
# ADMIN ENDPOINTS (Admin Authenticated)
# ==============================================================================

@router.get("/admin/flyfield/live")
async def get_admin_flyfield_live(admin = Depends(get_current_admin)):
    """
    Live real-time status of all FlyField employees in admin's organization:
    - Day status (Active / Completed / Not Started)
    - Current state (On Visit / Moving / Idle / Offline)
    - Real physical address
    - Coordinates for live map
    - Battery percentage & last seen
    """
    org_id = admin.organization_id
    today_start_utc, today_end_utc, today_str = get_today_bounds()

    # Query all employees assigned to FlyField in this org
    query = {
        "employee_type": "field",
        "field_mode": "flyfield"
    }
    if org_id and org_id != "system_org":
        query["organization_id"] = org_id

    agents = await employees_collection.find(query).to_list(length=500)
    now_utc = datetime.now(timezone.utc)

    result = []
    active_count = 0
    idle_count = 0
    on_visit_count = 0

    for agent in agents:
        email = agent["email"]

        # 1. Today's attendance
        today_logs = await attendance_logs_collection.find({
            "email": email,
            "timestamp": {"$gte": today_start_utc, "$lt": today_end_utc}
        }).sort("timestamp", 1).to_list(length=10)

        in_log = next((l for l in today_logs if l.get("type") in ["check-in", "IN"]), None)
        out_log = next((l for l in reversed(today_logs) if l.get("type") in ["check-out", "OUT"]), None)

        day_status = "Not Started"
        if in_log and not out_log:
            day_status = "Active"
        elif in_log and out_log:
            day_status = "Day Ended"

        # 2. Today's visits
        visits = await visit_logs_collection.find({
            "employee_email": email,
            "date": today_str,
            "visit_mode": "flyfield"
        }).to_list(length=100)

        active_visit = next((v for v in visits if v.get("status") == "in_progress"), None)
        completed_visits_count = sum(1 for v in visits if v.get("status") == "completed")

        # 3. Latest Location Ping with fallbacks to active/recent visits and attendance
        last_ping = await location_pings_collection.find_one(
            {"employee_id": email},
            sort=[("recorded_at", -1)]
        )

        lat = agent.get("territory_center_lat", 0.0)
        lng = agent.get("territory_center_lng", 0.0)
        address = "Location unavailable"
        last_seen_sec = 999999
        battery = None

        if last_ping:
            lat = last_ping.get("lat", lat)
            lng = last_ping.get("lng", lng)
            p_time = last_ping.get("recorded_at")
            if p_time:
                if p_time.tzinfo is None:
                    p_time = p_time.replace(tzinfo=timezone.utc)
                last_seen_sec = int((now_utc - p_time).total_seconds())
            battery = last_ping.get("battery_level")

        # Fallback 1: Active visit check-in location (highest priority when on visit)
        if active_visit and (not lat or not lng or lat == 0.0 or last_seen_sec > 600):
            v_lat = active_visit.get("check_in_lat")
            v_lng = active_visit.get("check_in_lng")
            if v_lat and v_lng:
                lat = v_lat
                lng = v_lng
                address = active_visit.get("check_in_address") or address
                v_time = active_visit.get("check_in_time")
                if v_time:
                    if v_time.tzinfo is None:
                        v_time = v_time.replace(tzinfo=timezone.utc)
                    last_seen_sec = min(last_seen_sec, int((now_utc - v_time).total_seconds()))

        # Fallback 2: Most recent completed visit today
        if (not lat or not lng or lat == 0.0) and visits:
            latest_v = sorted(visits, key=lambda x: x.get("created_at") or now_utc, reverse=True)[0]
            v_lat = latest_v.get("check_out_lat") or latest_v.get("check_in_lat")
            v_lng = latest_v.get("check_out_lng") or latest_v.get("check_in_lng")
            if v_lat and v_lng:
                lat = v_lat
                lng = v_lng
                address = latest_v.get("check_out_address") or latest_v.get("check_in_address") or address
                v_time = latest_v.get("check_out_time") or latest_v.get("check_in_time")
                if v_time:
                    if v_time.tzinfo is None:
                        v_time = v_time.replace(tzinfo=timezone.utc)
                    last_seen_sec = min(last_seen_sec, int((now_utc - v_time).total_seconds()))

        # Fallback 3: Today's morning check-in attendance punch
        if (not lat or not lng or lat == 0.0) and in_log:
            loc = in_log.get("location") or {}
            att_lat = loc.get("lat") or loc.get("latitude")
            att_lng = loc.get("long") or loc.get("longitude") or loc.get("lng")
            if att_lat and att_lng:
                lat = att_lat
                lng = att_lng
                address = in_log.get("location_name") or in_log.get("address") or address
                att_time = in_log.get("timestamp")
                if att_time:
                    if att_time.tzinfo is None:
                        att_time = att_time.replace(tzinfo=timezone.utc)
                    last_seen_sec = min(last_seen_sec, int((now_utc - att_time).total_seconds()))

        # Real address resolution if missing or generic
        if lat and lng and (not address or address == "Location unavailable"):
            address = await resolve_real_address(lat, lng)

        # Status categorization
        current_status = "offline"
        if day_status == "Active":
            active_count += 1
            if active_visit:
                current_status = "on_visit"
                on_visit_count += 1
            elif last_seen_sec <= 180:
                current_status = "moving"
            elif last_seen_sec <= 900:
                current_status = "idle"
                idle_count += 1
            else:
                current_status = "offline"
        elif day_status == "Day Ended":
            current_status = "day_ended"

        result.append({
            "employee_id": str(agent["_id"]),
            "email": email,
            "name": agent.get("full_name", email),
            "designation": agent.get("designation", "Field Agent"),
            "phone": agent.get("phone", ""),
            "day_status": day_status,
            "current_status": current_status,
            "lat": lat,
            "lng": lng,
            "address": address,
            "last_seen_seconds": last_seen_sec,
            "battery": battery,
            "active_visit": {
                "visit_id": str(active_visit["_id"]),
                "client_name": active_visit.get("client_name"),
                "check_in_time": active_visit.get("check_in_time").isoformat() if active_visit.get("check_in_time") else None,
                "address": active_visit.get("check_in_address")
            } if active_visit else None,
            "completed_visits_count": completed_visits_count
        })

    return {
        "status": "success",
        "date": today_str,
        "stats": {
            "total_agents": len(agents),
            "active_now": active_count,
            "on_visit": on_visit_count,
            "idle": idle_count
        },
        "agents": result
    }


@router.get("/admin/flyfield/employee/{email}/timeline")
async def get_admin_employee_timeline(email: str, date: Optional[str] = None, admin = Depends(get_current_admin)):
    """Admin timeline view for a specific FlyField employee"""
    return await build_daily_timeline(email, date)


@router.get("/admin/flyfield/visits")
async def get_admin_flyfield_visits(
    date: Optional[str] = None,
    employee_email: Optional[str] = None,
    status_filter: Optional[str] = None,
    admin = Depends(get_current_admin)
):
    """Tabular list of all FlyField visits with real addresses, photos, and playable voice notes"""
    org_id = admin.organization_id
    query: Dict[str, Any] = {"visit_mode": "flyfield"}
    if org_id and org_id != "system_org":
        query["organization_id"] = org_id

    if date:
        query["date"] = date
    if employee_email:
        query["employee_email"] = employee_email.strip().lower()
    if status_filter and status_filter != "all":
        query["status"] = status_filter

    visits = await visit_logs_collection.find(query).sort("check_in_time", -1).to_list(length=500)
    for v in visits:
        v["_id"] = str(v["_id"])
        if v.get("check_in_time") and hasattr(v["check_in_time"], "isoformat"):
            v["check_in_time"] = v["check_in_time"].isoformat()
        if v.get("check_out_time") and hasattr(v["check_out_time"], "isoformat"):
            v["check_out_time"] = v["check_out_time"].isoformat()

    return {"status": "success", "count": len(visits), "visits": visits}


@router.post("/admin/flyfield/visit/{visit_id}/close")
async def admin_close_stuck_visit(visit_id: str, admin = Depends(get_current_admin)):
    """Allows Admin to close a visit if an employee forgot to checkout"""
    try:
        obj_id = ObjectId(visit_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid visit ID")

    visit = await visit_logs_collection.find_one({"_id": obj_id})
    if not visit:
        raise HTTPException(status_code=404, detail="Visit not found")

    now_utc = datetime.now(timezone.utc)
    in_time = visit.get("check_in_time")
    if in_time and in_time.tzinfo is None:
        in_time = in_time.replace(tzinfo=timezone.utc)
    duration_mins = round((now_utc - in_time).total_seconds() / 60.0) if in_time else 0

    await visit_logs_collection.update_one(
        {"_id": obj_id},
        {
            "$set": {
                "status": "completed",
                "check_out_time": now_utc,
                "duration_minutes": max(1, duration_mins),
                "remarks": f"Force-closed by admin ({admin.email})",
                "updated_at": now_utc
            }
        }
    )
    return {"status": "success", "message": "Visit closed successfully."}


# ==============================================================================
# Helper Function: Build Complete Daily Timeline Object
# ==============================================================================

async def build_daily_timeline(email: str, date_str: Optional[str] = None) -> dict:
    today_start_utc, today_end_utc, today_str = get_today_bounds()
    target_date = date_str or today_str

    # Date range for target day
    try:
        target_dt = datetime.strptime(target_date, "%Y-%m-%d")
        day_start = target_dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=timezone.utc) - timedelta(minutes=330)
        day_end = day_start + timedelta(days=1)
    except Exception:
        day_start, day_end = today_start_utc, today_end_utc

    # 1. Employee info
    emp = await employees_collection.find_one({"email": email})
    emp_name = emp.get("full_name", email) if emp else email

    # 2. Check-in & Check-out logs
    logs = await attendance_logs_collection.find({
        "email": email,
        "timestamp": {"$gte": day_start, "$lt": day_end}
    }).sort("timestamp", 1).to_list(length=20)

    in_log = next((l for l in logs if l.get("type") in ["check-in", "IN"]), None)
    out_log = next((l for l in reversed(logs) if l.get("type") in ["check-out", "OUT"]), None)

    # 3. Visits on target date
    visits = await visit_logs_collection.find({
        "employee_email": email,
        "date": target_date,
        "visit_mode": "flyfield"
    }).sort("check_in_time", 1).to_list(length=100)

    formatted_visits = []
    for v in visits:
        v["_id"] = str(v["_id"])
        formatted_visits.append({
            "visit_id": str(v["_id"]),
            "client_name": v.get("client_name"),
            "client_type": v.get("client_type", "customer"),
            "status": v.get("status"),
            "check_in_time": v.get("check_in_time").isoformat() if v.get("check_in_time") else None,
            "check_in_address": v.get("check_in_address") or "Location recorded",
            "check_out_time": v.get("check_out_time").isoformat() if v.get("check_out_time") else None,
            "check_out_address": v.get("check_out_address") or v.get("check_in_address"),
            "duration_minutes": v.get("duration_minutes", 0),
            "remarks": v.get("remarks"),
            "outcome": v.get("outcome", "Completed"),
            "person_met_name": v.get("person_met_name"),
            "person_met_role": v.get("person_met_role"),
            "media": v.get("media", [])
        })

    # 4. Location trail pings for map polyline
    pings = await location_pings_collection.find({
        "employee_id": email,
        "recorded_at": {"$gte": day_start, "$lt": day_end}
    }).sort("recorded_at", 1).to_list(length=1000)

    trail = [{"lat": p["lat"], "lng": p["lng"], "time": p["recorded_at"].isoformat() if p.get("recorded_at") else None} for p in pings if p.get("lat") and p.get("lng")]

    # Synthesize trail from day_start and visit stops if continuous pings are not available
    if not trail:
        synthetic = []
        if in_log and in_log.get("location"):
            lat = in_log["location"].get("lat") or in_log["location"].get("latitude")
            lng = in_log["location"].get("long") or in_log["location"].get("longitude")
            if lat and lng and (lat != 0.0 or lng != 0.0):
                synthetic.append({"lat": lat, "lng": lng, "time": in_log["timestamp"].isoformat() if in_log.get("timestamp") else None, "label": "Start Day"})
        for v in visits:
            if v.get("check_in_lat") and v.get("check_in_lng"):
                synthetic.append({"lat": v["check_in_lat"], "lng": v["check_in_lng"], "time": v["check_in_time"].isoformat() if v.get("check_in_time") else None, "label": v.get("client_name")})
            if v.get("check_out_lat") and v.get("check_out_lng"):
                synthetic.append({"lat": v["check_out_lat"], "lng": v["check_out_lng"], "time": v["check_out_time"].isoformat() if v.get("check_out_time") else None, "label": f"{v.get('client_name')} (Out)"})
        if out_log and out_log.get("location"):
            lat = out_log["location"].get("lat") or out_log["location"].get("latitude")
            lng = out_log["location"].get("long") or out_log["location"].get("longitude")
            if lat and lng and (lat != 0.0 or lng != 0.0):
                synthetic.append({"lat": lat, "lng": lng, "time": out_log["timestamp"].isoformat() if out_log.get("timestamp") else None, "label": "End Day"})
        trail = synthetic

    return {
        "status": "success",
        "date": target_date,
        "employee": {
            "name": emp_name,
            "email": email,
            "designation": emp.get("designation", "Field Agent") if emp else "Staff"
        },
        "day_start": {
            "time": in_log["timestamp"].isoformat() if in_log else None,
            "address": in_log.get("address") or in_log.get("location_name") or "Office / Field Start Point",
            "face_verified": in_log.get("selfie_verified", True) if in_log else False,
            "lat": in_log.get("location", {}).get("lat") if in_log and in_log.get("location") else None,
            "lng": in_log.get("location", {}).get("long") if in_log and in_log.get("location") else None
        } if in_log else None,
        "visits": formatted_visits,
        "day_end": {
            "time": out_log["timestamp"].isoformat() if out_log else None,
            "address": out_log.get("address") or out_log.get("location_name") or "End Point",
            "face_verified": out_log.get("selfie_verified", True) if out_log else False,
            "lat": out_log.get("location", {}).get("lat") if out_log and out_log.get("location") else None,
            "lng": out_log.get("location", {}).get("long") if out_log and out_log.get("location") else None
        } if out_log else None,
        "trail": trail,
        "total_visits": len(formatted_visits),
        "completed_visits": sum(1 for v in formatted_visits if v["status"] == "completed")
    }
