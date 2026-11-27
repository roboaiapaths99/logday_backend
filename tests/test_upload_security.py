import asyncio
import base64
import io
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import jwt
from PIL import Image
import pytest

from test_security import account, bearer
import auth
import upload_security as uploads


@pytest.fixture(autouse=True)
def isolated_upload_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(uploads, "UPLOAD_ROOT", tmp_path)
    from services import common_helpers
    monkeypatch.setattr(common_helpers, "UPLOAD_DIR", str(tmp_path))


def png_payload():
    output = io.BytesIO()
    Image.new("RGB", (3, 3), "green").save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


def saved_image(db, employee, purpose="screenshot"):
    url = asyncio.run(uploads.save_local_image(png_payload(), employee, purpose, "site_photos"))
    return db.uploads_collection.find_one({"url": url})


def approve_device(db, employee):
    db.wfh_device_info_collection.insert_one({"employee_id": str(employee["_id"]),
                                              "organization_id": employee["organization_id"],
                                              "device_id": "test-device", "status": "approved"})


def test_private_files_require_auth_even_outside_production(client, db):
    employee = account(db)
    record = saved_image(db, employee)
    assert client.get(record["url"]).status_code == 401
    assert client.get(record["url"], headers=bearer(employee)).status_code == 200


def test_other_employee_and_other_tenant_cannot_read_upload(client, db):
    employee = account(db)
    colleague = account(db, email="colleague@example.test", org=employee["organization_id"])
    outsider = account(db, "admin", email="outside@example.test", role="owner")
    record = saved_image(db, employee)
    for headers in [bearer(colleague), bearer(outsider, "admin")]:
        assert client.get(record["url"], headers=headers).status_code == 404
        assert client.get(f"/api/uploads/{record['_id']}/download-link", headers=headers).status_code == 404


def test_admin_needs_file_purpose_permission(client, db):
    employee = account(db)
    admin = account(db, "admin", org=employee["organization_id"], features=["reports"])
    record = saved_image(db, employee)
    headers = bearer(admin, "admin")
    assert client.get(record["url"], headers=headers).status_code == 403
    db.admins_collection.update_one({"_id": admin["_id"]}, {"$set": {"allowed_features": ["wfh_monitoring"]}})
    assert client.get(record["url"], headers=headers).status_code == 200


def test_grant_is_file_scoped_and_revoked_with_session(client, db):
    employee = account(db)
    record = saved_image(db, employee)
    other = saved_image(db, employee)
    headers = bearer(employee)
    response = client.get(f"/api/uploads/{record['_id']}/download-link", headers=headers)
    assert response.status_code == 200
    link = response.json()["url"]
    assert client.get(link).status_code == 200
    assert client.get(link.replace(record["_id"], other["_id"])).status_code == 403
    grant = parse_qs(urlparse(link).query)["grant"][0]
    assert client.get("/api/employee/profile", headers={"Authorization": f"Bearer {grant}"}).status_code == 401
    assert client.post("/auth/logout", headers=headers).status_code == 200
    assert client.get(link).status_code == 401


def test_grant_expiry_and_permission_changes_are_enforced(client, db):
    employee = account(db)
    admin = account(db, "admin", org=employee["organization_id"], features=["wfh_monitoring"])
    record = saved_image(db, employee)
    link = client.get(f"/api/uploads/{record['_id']}/download-link", headers=bearer(admin, "admin")).json()["url"]
    claims = jwt.decode(parse_qs(urlparse(link).query)["grant"][0], options={"verify_signature": False})
    claims["exp"] = 1
    expired = jwt.encode(claims, auth.SECRET_KEY, algorithm=auth.ALGORITHM)
    assert client.get(f"/api/uploads/{record['_id']}/content", params={"grant": expired}).status_code == 401
    db.admins_collection.update_one({"_id": admin["_id"]}, {"$set": {"allowed_features": []}})
    assert client.get(link).status_code == 403


def test_unregistered_legacy_file_is_denied(client, db):
    employee = account(db)
    (uploads.UPLOAD_ROOT / "legacy.txt").write_text("private")
    assert client.get("/uploads/legacy.txt", headers=bearer(employee)).status_code == 404


@pytest.mark.parametrize("path", ["/uploads/../secret", "/uploads/%2e%2e/secret", "/uploads/C:/secret",
                                  "/uploads/..\\secret", "/uploads/%2fetc/passwd", "/uploads/private?a=1"])
def test_traversal_and_ambiguous_paths_rejected(path):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        uploads.safe_local_path(path)


@pytest.mark.parametrize("value", ["http://127.0.0.1/private", "http://169.254.169.254/metadata", "/uploads/someone-elses.png"])
def test_screenshot_rejects_urls_and_paths(client, db, value):
    employee = account(db)
    approve_device(db, employee)
    response = client.post("/api/wfh/screenshot", headers=bearer(employee),
                           json={"device_id": "test-device", "image_url": value})
    assert response.status_code == 400
    assert db.uploads_collection.count_documents({}) == 0


def test_screenshot_rejects_invalid_or_oversized_image(client, db, monkeypatch):
    employee = account(db)
    approve_device(db, employee)
    headers = bearer(employee)
    body = {"device_id": "test-device", "image_url": "data:image/png;base64," + base64.b64encode(b"not an image").decode()}
    assert client.post("/api/wfh/screenshot", headers=headers, json=body).status_code == 400
    monkeypatch.setattr(uploads, "MAX_IMAGE_BYTES", 10)
    body["image_url"] = png_payload()
    assert client.post("/api/wfh/screenshot", headers=headers, json=body).status_code == 413


def test_screenshot_round_trip_returns_authorized_display_link(client, db):
    employee = account(db)
    approve_device(db, employee)
    headers = bearer(employee)
    response = client.post("/api/wfh/screenshot", headers=headers,
                           json={"device_id": "test-device", "image_url": png_payload(), "thumbnail_url": "https://evil.example.test"})
    assert response.status_code == 200, response.text
    stored = db.wfh_screenshots_collection.find_one({})
    assert stored["image_url"].startswith("/uploads/")
    assert stored["thumbnail_url"] is None
    response = client.get("/api/wfh/screenshots", headers=headers)
    assert response.status_code == 200, response.text
    link = response.json()["screenshots"][0]["image_url"]
    assert "/api/uploads/" in link and "grant=" in link
    image = client.get(link)
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/png"


def test_screenshot_cannot_reuse_another_employees_upload(client, db):
    employee = account(db)
    other = account(db, email="other@example.test", org=employee["organization_id"])
    record = saved_image(db, other)
    approve_device(db, employee)
    response = client.post("/api/wfh/screenshot", headers=bearer(employee),
                           json={"device_id": "test-device", "upload_id": record["_id"]})
    assert response.status_code == 404


def test_request_limit_covers_declared_and_chunked_bodies():
    from body_limits import RequestBodyLimitMiddleware
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=8)

    @app.post("/")
    async def echo(request: Request):
        return {"size": len(await request.body())}

    client = TestClient(app)
    assert client.post("/", content=b"123456789").status_code == 413
    assert client.post("/", content=iter([b"12345", b"67890"])).status_code == 413
    assert client.post("/", content=b"123").json() == {"size": 3}


def test_backfill_is_dry_run_by_default_and_rejects_conflicting_owners(db):
    import hashlib
    from backfill_upload_registry import backfill
    employee = account(db)
    other = account(db, email="other@example.test", org=employee["organization_id"])
    data, _ = uploads.decode_image_payload(png_payload())
    (uploads.UPLOAD_ROOT / "legacy.png").write_bytes(data)
    entry = {"url": "/uploads/legacy.png", "owner_id": str(employee["_id"]),
             "organization_id": employee["organization_id"], "purpose": "screenshot",
             "sha256": hashlib.sha256(data).hexdigest()}
    assert asyncio.run(backfill([entry]))["validated_new_uploads"] == 1
    assert db.uploads_collection.count_documents({}) == 0
    with pytest.raises(ValueError, match="Conflicting ownership"):
        asyncio.run(backfill([entry, {**entry, "owner_id": str(other["_id"])}], apply=True))
    assert db.uploads_collection.count_documents({}) == 0
    asyncio.run(backfill([entry], apply=True))
    assert db.uploads_collection.count_documents({}) == 1
    assert asyncio.run(backfill([entry], apply=True))["validated_new_uploads"] == 0


def test_backfill_rejects_changed_content_and_wrong_tenant(db):
    from backfill_upload_registry import backfill
    employee = account(db)
    (uploads.UPLOAD_ROOT / "legacy.png").write_bytes(b"changed")
    entry = {"url": "/uploads/legacy.png", "owner_id": str(employee["_id"]),
             "organization_id": employee["organization_id"], "purpose": "screenshot", "sha256": "incorrect"}
    with pytest.raises(ValueError, match="SHA-256"):
        asyncio.run(backfill([entry], apply=True))
    with pytest.raises(ValueError, match="Owner does not belong"):
        asyncio.run(backfill([{**entry, "organization_id": "other"}], apply=True))
    assert db.uploads_collection.count_documents({}) == 0


def test_retention_cannot_delete_unregistered_files_and_revokes_registered_files(db):
    from services.common_helpers import delete_file_from_storage
    employee = account(db)
    record = saved_image(db, employee)
    assert not asyncio.run(delete_file_from_storage("/uploads/../../private-file"))
    assert asyncio.run(delete_file_from_storage(record["url"]))
    assert not uploads.safe_local_path(record["url"]).exists()
    assert db.uploads_collection.find_one({"_id": record["_id"]})["revoked"]


def test_manager_cannot_read_another_team_upload(client, db):
    employee = account(db)
    manager = account(db, "admin", email="manager@example.test", role="manager",
                      org=employee["organization_id"], features=["wfh_monitoring"])
    record = saved_image(db, employee)
    headers = bearer(manager, "admin")
    assert client.get(record["url"], headers=headers).status_code == 404
    db.employees_collection.update_one({"_id": employee["_id"]}, {"$set": {"manager_id": manager["email"]}})
    assert client.get(record["url"], headers=headers).status_code == 200
