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
    
    # 1. Restore all logs to their original created_at timestamps
    print("Restoring all June logs to their original created_at timestamps...")
    for log in logs:
        created_at = log.get("created_at")
        if created_at:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            # Update in DB
            await attendance_logs_collection.update_one(
                {"_id": log["_id"]},
                {"$set": {"timestamp": created_at}}
            )
            ts = created_at
        else:
            ts = log["timestamp"]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        
        # Group by IST day
        local_ts = ts + tz_offset
        day_str = local_ts.strftime("%Y-%m-%d")
        if day_str not in grouped_logs:
            grouped_logs[day_str] = []
        
        log["timestamp"] = ts
        grouped_logs[day_str].append(log)

    # Days to completely remove attendance for (weekends/unused)
    delete_days = {"2026-06-13", "2026-06-14", "2026-06-15", "2026-06-18", "2026-06-27", "2026-06-28"}

    # Special randomization days: June 16 and 17
    special_randomize_days = {"2026-06-16", "2026-06-17"}

    # Late checkout range: 6:02 PM - 6:10 PM IST.
    # Group dates by their different check-in styles:
    
    # 1. Random check-in before 10:30 (10:00 - 10:25 AM IST) + Late checkout
    early_checkin_days = {"2026-06-29", "2026-06-30"}
    
    # 2. Random check-in before 10:15 (10:00 - 10:15 AM IST) + Late checkout
    checkin_before_1015_days = {"2026-06-24"}
    
    # 3. Specific check-in time + Late checkout
    specific_checkin_days = {
        "2026-06-23": "14:20:00",
        "2026-06-25": "13:50:00"
    }
    
    # 4. Keep original check-in + Late checkout
    late_checkout_only_days = set()

    for day_str, day_logs in grouped_logs.items():
        if day_str in delete_days:
            # Delete logs for this day
            log_ids = [l["_id"] for l in day_logs]
            delete_res = await attendance_logs_collection.delete_many({"_id": {"$in": log_ids}})
            print(f"Deleted {delete_res.deleted_count} logs for date {day_str} (marking as weekend/absent).")
        elif day_str == "2026-06-26":
            # June 26 is handled separately (we delete existing and insert new fixed times)
            log_ids = [l["_id"] for l in day_logs]
            await attendance_logs_collection.delete_many({"_id": {"$in": log_ids}})
            print(f"Cleared existing logs for {day_str} to insert fresh ones.")
        elif day_str == "2026-06-16" or day_str == "2026-06-17":
            # Check-in: random between 10:00 AM and 10:15 AM IST
            # Check-out: random between 3:00 PM and 5:00 PM (15:00 to 17:00) IST
            check_ins = [l for l in day_logs if l.get("type") == "check-in"]
            check_outs = [l for l in day_logs if l.get("type") == "check-out"]

            check_ins.sort(key=lambda x: x["timestamp"])
            check_outs.sort(key=lambda x: x["timestamp"])

            # Set random check-in
            if check_ins:
                rand_min = random.randint(0, 15)
                local_checkin_dt = datetime.strptime(f"{day_str} 10:00:00", "%Y-%m-%d %H:%M:%S") + timedelta(minutes=rand_min)
                utc_checkin_dt = local_checkin_dt - tz_offset
                await attendance_logs_collection.update_one(
                    {"_id": check_ins[0]["_id"]},
                    {"$set": {"timestamp": utc_checkin_dt}}
                )
                print(f"Updated check-in for {day_str} to random local time {local_checkin_dt.strftime('%H:%M')}.")

            # Set random check-out
            if check_outs:
                rand_min = random.randint(0, 120)
                local_checkout_dt = datetime.strptime(f"{day_str} 15:00:00", "%Y-%m-%d %H:%M:%S") + timedelta(minutes=rand_min)
                utc_checkout_dt = local_checkout_dt - tz_offset
                await attendance_logs_collection.update_one(
                    {"_id": check_outs[-1]["_id"]},
                    {"$set": {"timestamp": utc_checkout_dt}}
                )
                print(f"Updated check-out for {day_str} to random local time {local_checkout_dt.strftime('%H:%M')}.")
        elif day_str in early_checkin_days or day_str in checkin_before_1015_days or day_str in specific_checkin_days or day_str in late_checkout_only_days:
            # All of these days get a late checkout randomized between 6:02 PM and 6:10 PM IST
            check_ins = [l for l in day_logs if l.get("type") == "check-in"]
            check_outs = [l for l in day_logs if l.get("type") == "check-out"]
            
            check_ins.sort(key=lambda x: x["timestamp"])
            check_outs.sort(key=lambda x: x["timestamp"])

            # Handle Check-in Updates
            if check_ins:
                if day_str in early_checkin_days:
                    # Random time between 10:00 AM and 10:25 AM IST (0 to 25 minutes past 10:00)
                    rand_min = random.randint(0, 25)
                    local_checkin_dt = datetime.strptime(f"{day_str} 10:00:00", "%Y-%m-%d %H:%M:%S") + timedelta(minutes=rand_min)
                    utc_checkin_dt = local_checkin_dt - tz_offset
                    await attendance_logs_collection.update_one(
                        {"_id": check_ins[0]["_id"]},
                        {"$set": {"timestamp": utc_checkin_dt}}
                    )
                    print(f"Updated check-in for {day_str} (early) to {local_checkin_dt.strftime('%H:%M')}.")
                elif day_str in checkin_before_1015_days:
                    # Random time between 10:00 AM and 10:15 AM IST (0 to 15 minutes past 10:00)
                    rand_min = random.randint(0, 15)
                    local_checkin_dt = datetime.strptime(f"{day_str} 10:00:00", "%Y-%m-%d %H:%M:%S") + timedelta(minutes=rand_min)
                    utc_checkin_dt = local_checkin_dt - tz_offset
                    await attendance_logs_collection.update_one(
                        {"_id": check_ins[0]["_id"]},
                        {"$set": {"timestamp": utc_checkin_dt}}
                    )
                    print(f"Updated check-in for {day_str} (before 10:15) to {local_checkin_dt.strftime('%H:%M')}.")
                elif day_str in specific_checkin_days:
                    # Set exact check-in time (e.g. 14:20:00 or 13:50:00)
                    time_str = specific_checkin_days[day_str]
                    local_checkin_dt = datetime.strptime(f"{day_str} {time_str}", "%Y-%m-%d %H:%M:%S")
                    utc_checkin_dt = local_checkin_dt - tz_offset
                    await attendance_logs_collection.update_one(
                        {"_id": check_ins[0]["_id"]},
                        {"$set": {"timestamp": utc_checkin_dt}}
                    )
                    print(f"Updated check-in for {day_str} (specific) to {time_str}.")

            # Handle Check-out Updates
            if check_outs:
                rand_min = random.randint(0, 8)
                local_checkout_dt = datetime.strptime(f"{day_str} 18:02:00", "%Y-%m-%d %H:%M:%S") + timedelta(minutes=rand_min)
                utc_checkout_dt = local_checkout_dt - tz_offset
                await attendance_logs_collection.update_one(
                    {"_id": check_outs[-1]["_id"]},
                    {"$set": {"timestamp": utc_checkout_dt}}
                )
                print(f"Updated check-out for {day_str} to random late time {local_checkout_dt.strftime('%H:%M')}.")
        else:
            # All other days are left completely untouched at their restored created_at values
            print(f"Preserving original restored data for {day_str}.")

    # 3. Handle June 26, 2026: Check-in at 10:10 AM, Check-out at 6:12 PM (18:12)
    target_day = "2026-06-26"
    in_dt_local = datetime.strptime(f"{target_day} 10:10:00", "%Y-%m-%d %H:%M:%S")
    out_dt_local = datetime.strptime(f"{target_day} 18:12:00", "%Y-%m-%d %H:%M:%S")
    in_dt_utc = in_dt_local - tz_offset
    out_dt_utc = out_dt_local - tz_offset

    await attendance_logs_collection.delete_many({"user_id": user_id, "timestamp": {"$gte": in_dt_utc - timedelta(hours=12), "$lt": out_dt_utc + timedelta(hours=12)}})

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
