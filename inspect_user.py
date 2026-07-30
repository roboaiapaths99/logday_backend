import asyncio
from database import employees_collection, attendance_logs_collection
from bson import ObjectId
from datetime import datetime

async def inspect():
    # 1. Find user by email
    user = await employees_collection.find_one({"email": "bb@gmail.com"})
    if not user:
        print("Employee 'bb@gmail.com' not found.")
        # Let's also print all employees to see who exists
        all_emps = await employees_collection.find().to_list(length=100)
        print("Existing employees:")
        for emp in all_emps:
            print(f"- {emp.get('email') or emp.get('name')}: ID={emp['_id']}")
        return
    
    user_id = str(user["_id"])
    print(f"Found employee: {user.get('name')} ({user.get('email')}), ID: {user_id}")
    
    # 2. Get attendance logs for June 2026
    start_date = datetime(2026, 6, 1)
    end_date = datetime(2026, 7, 1)
    
    logs = await attendance_logs_collection.find({
        "user_id": user_id,
        "timestamp": {"$gte": start_date, "$lt": end_date}
    }).sort("timestamp", 1).to_list(length=1000)
    
    print(f"Found {len(logs)} attendance logs for June 2026:")
    for log in logs:
        print(f"- LogID: {log['_id']}, Time: {log.get('timestamp')}, Type: {log.get('type')}, Location: {log.get('location')}, Status: {log.get('status')}")

if __name__ == "__main__":
    asyncio.run(inspect())
