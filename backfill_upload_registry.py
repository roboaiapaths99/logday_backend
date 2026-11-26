"""Validate an operator-reviewed ownership manifest. Dry-run unless --apply is passed.

No ownership is inferred from filenames, client-submitted URLs, or ambiguous legacy references.
"""
import argparse
import asyncio
import hashlib
import json
import mimetypes
from pathlib import Path

from bson import ObjectId
from fastapi.concurrency import run_in_threadpool

from database import employees_collection, uploads_collection, client
from upload_security import PURPOSE_FEATURES, storage_location, read_upload_bytes, register_upload


async def prepare_manifest(entries):
    if not isinstance(entries, list):
        raise ValueError("Manifest must be a JSON array")
    prepared = []
    identities = {}
    for entry in entries:
        url, owner_id, org_id, purpose = (entry[key] for key in ("url", "owner_id", "organization_id", "purpose"))
        identity = (owner_id, org_id, purpose)
        if url in identities:
            if identities[url] != identity:
                raise ValueError("Conflicting ownership claims; review the manifest before applying")
            continue
        identities[url] = identity
        if purpose not in PURPOSE_FEATURES or not ObjectId.is_valid(owner_id):
            raise ValueError("Invalid owner or purpose")
        owner = await employees_collection.find_one({"_id": ObjectId(owner_id), "organization_id": org_id})
        if not owner:
            raise ValueError("Owner does not belong to the stated organization")
        existing = await uploads_collection.find_one({"url": url})
        if existing:
            if (existing["owner_id"], existing["organization_id"], existing["purpose"]) != identity:
                raise ValueError("Manifest conflicts with registered ownership")
            continue
        backend, key = storage_location(url)
        data = await run_in_threadpool(read_upload_bytes, {"storage_backend": backend, "storage_key": key})
        if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            raise ValueError("File content does not match the reviewed SHA-256 digest")
        media_type = mimetypes.guess_type(url)[0] or "application/octet-stream"
        prepared.append((url, owner, purpose, media_type, len(data)))
    return prepared


async def backfill(entries, apply=False):
    prepared = await prepare_manifest(entries)
    if apply:
        # Never suppress uniqueness failures; resolve legacy duplicate metadata first.
        await uploads_collection.create_index("url", unique=True)
        for args in prepared:
            await register_upload(*args)
    return {"mode": "apply" if apply else "dry-run", "validated_new_uploads": len(prepared)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        entries = json.loads(args.manifest.read_text(encoding="utf-8"))
        print(json.dumps(asyncio.run(backfill(entries, args.apply))))
    finally:
        client.close()
