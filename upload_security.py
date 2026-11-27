"""Owned upload metadata, bounded image decoding, and short-lived download grants."""
import base64
import binascii
import io
import json
import mimetypes
import os
from pathlib import Path
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, unquote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import Response
from fastapi.routing import APIRoute
import jwt
from jwt.exceptions import PyJWTError as JWTError
from PIL import Image, UnidentifiedImageError

from auth import (SECRET_KEY, ALGORITHM, TOKEN_ISSUER, employee_oauth2_scheme,
                  resolve_access_token, decode_access_token, resolve_session_claims)
from database import uploads_collection, employees_collection

UPLOAD_ROOT = Path(os.getenv("UPLOAD_DIR", "uploads")).resolve()
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
DOWNLOAD_AUDIENCE = "logday-file-download"
DOWNLOAD_SECONDS = 120
PURPOSE_FEATURES = {
    "screenshot": {"wfh_monitoring"}, "face_check": {"wfh_monitoring"},
    "onboarding": {"document_verification", "onboarding"},
    "field_media": {"war_room", "flyfield"}, "leave_proof": {"leaves"},
    "expense_receipt": {"expenses"},
}


def safe_local_path(url):
    if not isinstance(url, str) or not url.startswith("/uploads/"):
        raise HTTPException(400, "Invalid local upload path")
    if "?" in url or "#" in url or "\\" in url or "\x00" in url:
        raise HTTPException(400, "Invalid local upload path")
    relative = unquote(url[len("/uploads/"):])
    if not relative or "\\" in relative or ":" in relative or "\x00" in relative or ".." in relative.split("/"):
        raise HTTPException(400, "Invalid local upload path")
    target = (UPLOAD_ROOT / relative).resolve()
    if not target.is_relative_to(UPLOAD_ROOT) or target == UPLOAD_ROOT:
        raise HTTPException(403, "Upload path escapes storage")
    return target


def storage_location(url):
    if url.startswith("/uploads/"):
        safe_local_path(url)
        return "local", url[len("/uploads/"):]
    # Only our configured object store is permitted; never fetch an arbitrary URL.
    bucket = os.getenv("S3_BUCKET_NAME") or os.getenv("AWS_STORAGE_BUCKET_NAME", "logday-wfh-screenshots")
    prefixes = [(os.getenv("S3_PUBLIC_URL_PREFIX") or os.getenv("R2_PUBLIC_URL_PREFIX", "")).rstrip("/")]
    endpoint = os.getenv("S3_ENDPOINT_URL", "").rstrip("/")
    if endpoint and bucket:
        prefixes.append(f"{endpoint}/{bucket}")
    if bucket:
        prefixes.append(f"https://{bucket}.s3.amazonaws.com")
    for prefix in prefixes:
        if prefix and url.startswith(prefix + "/"):
            key = url[len(prefix) + 1:]
            if not key or any(x in key for x in ("..", "\\", "?", "#", "%", "\x00")):
                break
            return "s3", key
    raise HTTPException(400, "Untrusted upload storage location")


def validate_image_bytes(data):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(413, "Image exceeds the 10 MB limit")
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise HTTPException(415, "Only JPEG, PNG, and WebP images are supported")
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise HTTPException(413, "Image dimensions exceed the limit")
            media_type = Image.MIME[image.format]
            image.verify()
        # Ensure pixel data can actually be decoded, not just the file header.
        with Image.open(io.BytesIO(data)) as image:
            image.load()
        return media_type
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise HTTPException(400, "Invalid image data")


def decode_image_payload(value):
    if not isinstance(value, str) or len(value) > ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 128:
        raise HTTPException(413, "Image exceeds the 10 MB limit")
    if value.startswith("data:"):
        if not value.startswith("data:image/") or ";base64," not in value:
            raise HTTPException(400, "Invalid image encoding")
        value = value.split(",", 1)[1]
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(400, "Invalid base64 image")
    return data, validate_image_bytes(data)


async def register_upload(url, owner, purpose, media_type=None, size=None):
    if purpose not in PURPOSE_FEATURES or not owner.get("_id") or not owner.get("organization_id"):
        raise HTTPException(400, "Upload ownership is required")
    backend, key = storage_location(url)
    if backend == "local":
        path = safe_local_path(url)
        if not path.is_file():
            raise HTTPException(404, "Upload not found")
        size = path.stat().st_size
    document = {"_id": secrets.token_hex(16), "url": url, "storage_backend": backend,
                "storage_key": key, "owner_id": str(owner["_id"]),
                "organization_id": owner["organization_id"], "purpose": purpose,
                "media_type": media_type or mimetypes.guess_type(url)[0] or "application/octet-stream",
                "size": size, "created_at": datetime.now(timezone.utc), "revoked": False}
    # Called only after server-controlled file creation, never to claim a submitted URL.
    await uploads_collection.insert_one(document)
    return document


async def save_local_bytes(data, owner, purpose, folder, extension, media_type):
    if folder not in {"onboarding_docs", "selfies", "site_photos", "voice_notes", "proofs", "receipts", "flyfield"}:
        raise ValueError("Invalid server upload folder")
    if extension not in {".jpg", ".png", ".webp", ".pdf", ".m4a", ".aac", ".mp3", ".wav", ".ogg"}:
        raise HTTPException(415, "Unsupported file type")
    if not data or len(data) > 15 * 1024 * 1024:
        raise HTTPException(413, "File exceeds the 15 MB limit")
    url = f"/uploads/{folder}/{secrets.token_hex(16)}{extension}"
    path = safe_local_path(url)

    def write():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    await run_in_threadpool(write)
    await register_upload(url, owner, purpose, media_type, len(data))
    return url


async def save_local_image(value, owner, purpose, folder):
    data, media_type = await run_in_threadpool(decode_image_payload, value)
    extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[media_type]
    return await save_local_bytes(data, owner, purpose, folder, extension, media_type)


def decode_binary_payload(value):
    if not isinstance(value, str) or len(value) > 20 * 1024 * 1024 + 128:
        raise HTTPException(413, "File exceeds the 15 MB limit")
    if value.startswith("data:"):
        value = value.split(",", 1)[-1]
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise HTTPException(400, "Invalid base64 file")


async def authorize_upload(record, account, kind):
    if not record or record.get("revoked"):
        raise HTTPException(404, "Upload not found")
    if kind == "admin" and account.get("role") == "superadmin":
        return
    if record["organization_id"] != account.get("organization_id"):
        raise HTTPException(404, "Upload not found")
    if kind == "employee":
        if record["owner_id"] != str(account["_id"]):
            raise HTTPException(404, "Upload not found")
    elif account.get("role") not in {"owner", "admin"}:
        features = PURPOSE_FEATURES.get(record["purpose"], set())
        if not features.intersection(account.get("allowed_features") or []):
            raise HTTPException(403, "Upload access requires the corresponding feature")
        if account.get("role") == "manager":
            from bson import ObjectId
            owner = await employees_collection.find_one({"_id": ObjectId(record["owner_id"]),
                                                          "organization_id": account["organization_id"],
                                                          "manager_id": account["email"]})
            if not owner:
                raise HTTPException(404, "Upload not found")


async def owned_upload(upload_id, account, kind="employee", purpose=None):
    record = await uploads_collection.find_one({"_id": upload_id})
    await authorize_upload(record, account, kind)
    if purpose and record["purpose"] != purpose:
        raise HTTPException(400, "Wrong upload purpose")
    return record


def _s3_client():
    import boto3
    return boto3.client("s3", endpoint_url=os.getenv("S3_ENDPOINT_URL") or None,
                        aws_access_key_id=os.getenv("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID"),
                        aws_secret_access_key=os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY"),
                        region_name=os.getenv("S3_REGION_NAME", "us-east-1"))


def read_upload_bytes(record, maximum=15 * 1024 * 1024):
    if record["storage_backend"] == "local":
        path = safe_local_path("/uploads/" + record["storage_key"])
        if not path.is_file():
            raise HTTPException(404, "Upload not found")
        with path.open("rb") as source:
            data = source.read(maximum + 1)
    elif record["storage_backend"] == "s3":
        # SDK lookup by registered key, never by a URL supplied in a request or legacy record.
        bucket = os.getenv("S3_BUCKET_NAME") or os.getenv("AWS_STORAGE_BUCKET_NAME", "logday-wfh-screenshots")
        source = _s3_client().get_object(Bucket=bucket, Key=record["storage_key"])["Body"]
        try:
            data = source.read(maximum + 1)
        finally:
            source.close()
    else:
        raise HTTPException(400, "Unsupported storage backend")
    if len(data) > maximum:
        raise HTTPException(413, "Upload exceeds download limit")
    return data


def download_url(record, claims, request):
    now = datetime.now(timezone.utc)
    grant = jwt.encode({"iss": TOKEN_ISSUER, "aud": DOWNLOAD_AUDIENCE, "iat": now,
                        "exp": now + timedelta(seconds=DOWNLOAD_SECONDS), "upload_id": record["_id"],
                        **{key: claims[key] for key in ("sub", "sid", "account_type", "ver")}},
                       SECRET_KEY, algorithm=ALGORITHM)
    base = os.getenv("PUBLIC_API_URL", str(request.base_url)).rstrip("/")
    return f"{base}/api/uploads/{record['_id']}/content?grant={grant}"


router = APIRouter(tags=["Protected uploads"])


@router.get("/api/uploads/{upload_id}/download-link")
async def issue_download_link(upload_id: str, request: Request, token: str = Depends(employee_oauth2_scheme)):
    account, kind = await resolve_access_token(token)
    record = await owned_upload(upload_id, account, kind)
    return {"url": download_url(record, decode_access_token(token), request), "expires_in": DOWNLOAD_SECONDS}


@router.get("/api/uploads/{upload_id}/content")
async def download_upload(upload_id: str, grant: str):
    try:
        claims = jwt.decode(
            grant,
            SECRET_KEY,
            algorithms=[ALGORITHM],
            issuer=TOKEN_ISSUER,
            audience=DOWNLOAD_AUDIENCE,
            options={"require": ["exp", "iss", "aud"]},
        )
        if claims.get("upload_id") != upload_id:
            raise HTTPException(403, "Download grant does not match this file")
        account, kind = await resolve_session_claims(claims)
    except (JWTError, ValueError, TypeError, KeyError):
        raise HTTPException(401, "Invalid or expired download grant")
    record = await owned_upload(upload_id, account, kind)
    data = await run_in_threadpool(read_upload_bytes, record)
    return Response(data, media_type=record["media_type"], headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox", "Referrer-Policy": "no-referrer",
    })


class SecureMediaRoute(APIRoute):
    """Replace registered media references after response serialization/authorization.

    JSONResponse bodies already exist in memory; streaming responses are not buffered.
    Each unique reference is resolved once per response. Grants never enter the database.
    """
    def get_route_handler(self):
        handler = super().get_route_handler()

        async def secured(request):
            response = await handler(request)
            header = request.headers.get("authorization", "")
            if response.status_code >= 400 or not header.startswith("Bearer ") or not hasattr(response, "body"):
                return response
            if "application/json" not in response.headers.get("content-type", ""):
                return response
            payload = json.loads(response.body)
            refs = set()

            private_media_fields = {"image_url", "thumbnail_url", "file_url", "proof_url", "receipt_url",
                                    "selfie_url", "site_photo_url", "voice_note_url"}

            def collect(value, field=None):
                if isinstance(value, dict):
                    for key, item in value.items(): collect(item, key)
                elif isinstance(value, list):
                    for item in value: collect(item, field)
                elif isinstance(value, str) and (value.startswith("/uploads/") or value.startswith("https://") or value.startswith("http://")):
                    if field in private_media_fields:
                        refs.add(value)
                        return
                    try:
                        storage_location(value)
                        if not value.startswith("/uploads/logos/"): refs.add(value)
                    except HTTPException:
                        pass

            collect(payload)
            if not refs:
                return response
            account, kind = await resolve_access_token(header.split(" ", 1)[1])
            claims = decode_access_token(header.split(" ", 1)[1])
            records = await uploads_collection.find({"url": {"$in": list(refs)}}).to_list(length=len(refs))
            replacements = {url: "" for url in refs}  # Unregistered legacy files fail closed.
            for record in records:
                try:
                    await authorize_upload(record, account, kind)
                    replacements[record["url"]] = download_url(record, claims, request)
                except HTTPException:
                    pass

            def replace(value):
                if isinstance(value, dict): return {key: replace(item) for key, item in value.items()}
                if isinstance(value, list): return [replace(item) for item in value]
                return replacements.get(value, value) if isinstance(value, str) else value

            response.body = json.dumps(replace(payload), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            response.headers["content-length"] = str(len(response.body))
            response.headers["cache-control"] = "private, no-store"
            return response

        return secured
