import asyncio
import os
from database import employees_collection
from auth import ph
from datetime import datetime, timezone

async def create_demo_user():
    email = "guest@logday.app"
    password = "Guest@1234"
    
    # Check if user already exists
    existing = await employees_collection.find_one({"email": email})
    if existing:
        print(f"User {email} already exists. Updating details...")
        hashed_password = ph.hash(password)
        await employees_collection.update_one(
            {"email": email},
            {"$set": {
                "hashed_password": hashed_password,
                "organization_id": "69a030d6346b7fcb4ef8a39a",
                "employee_type": "field",
                "face_embedding": None
            }}
        )
        print("User details updated.")
        return

    print(f"Creating demo user: {email}")
    hashed_password = ph.hash(password)
    
    demo_user = {
        "full_name": "Demo Guest User",
        "email": email,
        "employee_id": "GUEST-001",
        "designation": "App Reviewer",
        "department": "QA",
        "organization_id": "69a030d6346b7fcb4ef8a39a", # Field Force V2 Enterprise
        "employee_type": "field",
        "hashed_password": hashed_password,
        "face_embedding": None,  # Allow reviewer to enroll their face
        "profile_image": None,
        "device_id": None,       # Will bind on first login
        "created_at": datetime.now(timezone.utc),
        "status": "Active",
        "territory_type": "radius",
        "territory_radius_meters": 5000, # Large radius for testing
        "gps_otp_fallback_enabled": True
    }
    
    await employees_collection.insert_one(demo_user)
    print("Demo user created successfully!")

if __name__ == "__main__":
    asyncio.run(create_demo_user())
