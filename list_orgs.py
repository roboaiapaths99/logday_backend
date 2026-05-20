import asyncio
from database import organizations_collection

async def list_orgs():
    orgs = await organizations_collection.find().to_list(100)
    for org in orgs:
        print(f"Name: {org.get('name')}, Slug: {org.get('slug')}, ID: {org.get('_id')}")

if __name__ == "__main__":
    asyncio.run(list_orgs())
