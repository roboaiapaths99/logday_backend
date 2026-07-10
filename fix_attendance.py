import asyncio
import random
from datetime import datetime, timezone, timedelta
from bson import ObjectId
from database import employees_collection, attendance_logs_collection, settings_collection

async def main():
    email = "bb@gmail.com"
    print(f"Starting attendance update for {email}...")

    # 1. Fetch user
    user = await employees_collection.find_one({"email": email})
    if not user:
        print(f"Error: User {email} not found in database.")
        return
    user_id = str(user["_id"])
    org_id = user.get("organization_id")
    print(f"Found employee: {user.get('full_name')} (ID: {user_id})")

    # Fetch default location from settings or fallback
    lat = 28.4145947
    lon = 77.354079
    if org_id:
        org_doc = await settings_collection.find_one({"organization_id": str(org_id)})
        if org_doc:
            lat = org_doc.get("office_lat", lat)
            lon = org_doc.get("office_long", lon)

    # Date range for June 2026
    start_date = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end_date = datetime(2026, 7, 1, tzinfo=timezone.utc)

    # 2. Fetch all logs for June 2026
    logs = await attendance_logs_collection.find({
        "user_id": user_id,
        "timestamp": {"$gte": start_date, "$lt": end_date}
    }).sort("timestamp", 1).to_list(length=1000)
    print(f"Retrieved {len(logs)} logs for June 2026.")

    # Group logs by localized IST day
    tz_offset = timedelta(hours=5, minutes=30)
    grouped_logs = {}
    for log in logs:
        ts = log["timestamp"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        
        # Localize to IST
        local_ts = ts + tz_offset
        day_str = local_ts.strftime("%Y-%m-%d")
        if day_str not in grouped_logs:
            grouped_logs[day_str] = []
        grouped_logs[day_str].append(log)

    # Days to completely remove attendance for
    delete_days = {"2026-06-13", "2026-06-15", "2026-06-18"}

    for day_str, day_logs in grouped_logs.items():
        if day_str in delete_days:
            # Delete logs for this day
            log_ids = [l["_id"] for l in day_logs]
            delete_res = await attendance_logs_collection.delete_many({"_id": {"$in": log_ids}})
            print(f"Deleted {delete_res.deleted_count} logs for date {day_str}.")
        elif day_str == "2026-06-26":
            # June 26 is handled separately (we delete existing and insert new fixed times)
            log_ids = [l["_id"] for l in day_logs]
            await attendance_logs_collection.delete_many({"_id": {"$in": log_ids}})
            print(f"Cleared existing logs for {day_str} to insert fresh ones.")
        else:
            # Randomize times between 10:00 AM and 6:00 PM
            # We want check-in to be btw 10:00 AM and 11:15 AM
            # We want check-out to be btw 5:00 PM and 6:00 PM
            # This ensures they differ each day but remain within the general shift.
            check_ins = [l for l in day_logs if l.get("type") == "check-in"]
            check_outs = [l for l in day_logs if l.get("type") == "check-out"]

            # Sort check-ins and check-outs by timestamp
            check_ins.sort(key=lambda x: x["timestamp"])
            check_outs.sort(key=lambda x: x["timestamp"])

            # Update first check-in
            if check_ins:
                # Random time between 10:00 and 11:15 IST
                rand_min = random.randint(0, 75)
                local_checkin_dt = datetime.strptime(f"{day_str} 10:00:00", "%Y-%m-%d %H:%M:%S") + timedelta(minutes=rand_min)
                utc_checkin_dt = local_checkin_dt - tz_offset
                
                await attendance_logs_collection.update_one(
                    {"_id": check_ins[0]["_id"]},
                    {"$set": {"timestamp": utc_checkin_dt}}
                )
                print(f"Updated check-in for {day_str} to random local time {local_checkin_dt.strftime('%H:%M')}.")

            # Update last check-out
            if check_outs:
                # Random time between 17:00 and 18:00 IST (5:00 PM and 6:00 PM)
                rand_min = random.randint(0, 60)
                local_checkout_dt = datetime.strptime(f"{day_str} 17:00:00", "%Y-%m-%d %H:%M:%S") + timedelta(minutes=rand_min)
                utc_checkout_dt = local_checkout_dt - tz_offset
                
                await attendance_logs_collection.update_one(
                    {"_id": check_outs[-1]["_id"]},
                    {"$set": {"timestamp": utc_checkout_dt}}
                )
                print(f"Updated check-out for {day_str} to random local time {local_checkout_dt.strftime('%H:%M')}.")

    # 3. Handle June 26, 2026: Check-in at 10:10 AM, Check-out at 6:12 PM (18:12)
    # Check-in: 10:10 IST -> 04:40 UTC
    # Check-out: 18:12 IST -> 12:42 UTC
    target_day = "2026-06-26"
    in_dt_local = datetime.strptime(f"{target_day} 10:10:00", "%Y-%m-%d %H:%M:%S")
    out_dt_local = datetime.strptime(f"{target_day} 18:12:00", "%Y-%m-%d %H:%M:%S")
    in_dt_utc = in_dt_local - tz_offset
    out_dt_utc = out_dt_local - tz_offset

    checkin_doc = {
        "user_id": user_id,
        "email": email,
        "organization_id": org_id,
        "timestamp": in_dt_utc,
        "type": "check-in",
        "status": "SUCCESS",
        "location": {"lat": lat, "long": lon},
        "location_name": "Office Core Zone",
        "check_in_method": "manual",
        "wifi_confidence": 100,
        "selfie_verified": True,
        "device_id": "manual-update-script",
        "created_at": in_dt_utc
    }
    
    checkout_doc = {
        "user_id": user_id,
        "email": email,
        "organization_id": org_id,
        "timestamp": out_dt_utc,
        "type": "check-out",
        "status": "SUCCESS",
        "location": {"lat": lat, "long": lon},
        "location_name": "Office Core Zone",
        "check_in_method": "manual",
        "wifi_confidence": 100,
        "selfie_verified": True,
        "device_id": "manual-update-script",
        "created_at": out_dt_utc
    }

    await attendance_logs_collection.insert_one(checkin_doc)
    await attendance_logs_collection.insert_one(checkout_doc)
    print(f"Successfully marked attendance on {target_day} with check-in at 10:10 AM IST and check-out at 6:12 PM IST.")
    print("Done!")

if __name__ == "__main__":
    asyncio.run(main())
