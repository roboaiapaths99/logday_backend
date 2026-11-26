"""Explicit operator command. Never creates or resets an admin during API startup."""
import argparse
import asyncio
import getpass
from datetime import datetime, timezone

from auth import get_password_hash_async
from database import admins_collection, client
from permissions import ALL_FEATURE_IDS


async def bootstrap(email, full_name, password):
    email = email.strip().lower()
    if "@" not in email or len(password) < 14:
        raise ValueError("A valid email and a password of at least 14 characters are required")
    # Fail if legacy duplicates exist; do not silently select or overwrite an account.
    await admins_collection.create_index("email", unique=True)
    if await admins_collection.find_one({"email": email}):
        raise ValueError("Admin already exists; bootstrap never overwrites accounts")
    await admins_collection.insert_one({
        "email": email, "full_name": full_name, "role": "superadmin",
        "organization_id": "system_org", "allowed_features": ALL_FEATURE_IDS,
        "hashed_password": await get_password_hash_async(password),
        "is_active": True, "created_at": datetime.now(timezone.utc),
    })


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    password = getpass.getpass("New superadmin password: ")
    if password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords do not match")
    try:
        asyncio.run(bootstrap(args.email, args.name, password))
        print("Database-backed superadmin created.")
    finally:
        client.close()
