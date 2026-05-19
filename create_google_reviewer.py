import asyncio
import os
from database import employees_collection
from auth import ph
from datetime import datetime, timezone

async def create_google_reviewer():
    email = "google.review@logday.app"
    password = "Test@1234"
    employee_id = "EMP-TEST-001"
    org_id = "69a030d6346b7fcb4ef8a39a" # Assuming Field Force V2 Enterprise based on your system
    
    # Check if user already exists
    existing = await employees_collection.find_one({"employee_id": employee_id})
    if existing:
        print(f"User {employee_id} already exists. Updating credentials for review...")
        hashed_password = ph.hash(password)
        await employees_collection.update_one(
            {"employee_id": employee_id},
            {"$set": {
                "hashed_password": hashed_password,
                "organization_id": org_id,
                "employee_type": "field",
                "face_embedding": None, # Unbind face
                "device_id": None       # Unbind device
            }}
        )
        print("Google Reviewer credentials refreshed and un-bound.")
        return

    print(f"Creating Google Reviewer user: {employee_id}")
    hashed_password = ph.hash(password)
    
    demo_user = {
        "full_name": "Google Play Reviewer",
        "email": email,
        "employee_id": employee_id,
        "designation": "Play Store Reviewer",
        "department": "QA",
        "organization_id": org_id,
        "employee_type": "field",
        "hashed_password": hashed_password,
        "face_embedding": None,  # Allow reviewer to enroll their face if they test check-in
        "profile_image": None,
        "device_id": None,       # Will bind on first login by reviewer
        "created_at": datetime.now(timezone.utc),
        "status": "Active",
        "territory_type": "radius",
        "territory_radius_meters": 5000, # Large radius so they don't get blocked
        "gps_otp_fallback_enabled": True # Easy fallback
    }
    
    await employees_collection.insert_one(demo_user)
    print("Google Reviewer account created successfully!")
    print(f"Organization ID: {org_id}")
    print(f"Employee ID: {employee_id}")
    print(f"Password: {password}")

if __name__ == "__main__":
    asyncio.run(create_google_reviewer())
