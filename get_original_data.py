import asyncio
from datetime import datetime, timezone, timedelta
from database import employees_collection, attendance_logs_collection

async def main():
    email = "bb@gmail.com"
    user = await employees_collection.find_one({"email": email})
    if not user:
        print(f"User {email} not found.")
        return
    user_id = str(user["_id"])
    
    start_date = datetime(2026, 6, 1, tzinfo=timezone.utc)
    end_date = datetime(2026, 7, 1, tzinfo=timezone.utc)
    
    logs = await attendance_logs_collection.find({
        "user_id": user_id,
        "timestamp": {"$gte": start_date, "$lt": end_date}
    }).sort("timestamp", 1).to_list(length=1000)
    
    tz_offset = timedelta(hours=5, minutes=30)
    
    print("=== LOG RECOVERY DATA ===")
    for log in logs:
        ts = log["timestamp"]
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        ist_ts = ts + tz_offset
        
        created_at = log.get("created_at")
        if created_at:
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            ist_created = created_at + tz_offset
            created_str = ist_created.strftime("%Y-%m-%d %I:%M:%S %p")
        else:
            created_str = "N/A"
            
        date_str = ist_ts.strftime("%Y-%m-%d")
        time_str = ist_ts.strftime("%I:%M:%S %p")
        print(f"{date_str} | Type: {log.get('type'):<10} | Current: {time_str} | Created At: {created_str}")

if __name__ == "__main__":
    asyncio.run(main())
