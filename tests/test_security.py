import asyncio
from datetime import datetime, timedelta, timezone

from bson import ObjectId
import jwt
import pytest

import auth
from invitations import invitation_digest


def account(db, kind="employee", email="same@example.test", role="hr", features=(), org=None):
    record = {"_id": ObjectId(), "email": email, "full_name": "Test User",
              "organization_id": org or str(ObjectId()), "role": role,
              "allowed_features": list(features), "status": "Active", "employee_type": "field",
              "hashed_password": auth.get_password_hash("correct-password"),
              "created_at": datetime.now(timezone.utc)}
    db[f"{'admins' if kind == 'admin' else 'employees'}_collection"].insert_one(record)
    return record


def bearer(record, kind="employee"):
    token = asyncio.run(auth.issue_access_token(record, kind))
    return {"Authorization": f"Bearer {token}"}


def registration(**overrides):
    return {"full_name": "Invited Employee", "email": "new@example.test", "password": "new-password",
            "employee_id": "client-chosen", "designation": "client-chosen", "department": "client-chosen",
            "organization_id": "attacker-chosen", "employee_type": "desk", "face_image": "test-image",
            **overrides}


def invitation(db, **overrides):
    token = "test-invitation-secret"
    record = {"_id": invitation_digest(token), "email": "new@example.test", "employee_id": "INVITED-01",
              "designation": "Field Executive", "department": "Sales", "employee_type": "field",
              "organization_id": str(ObjectId()), "consumed": False,
              "expires_at": datetime.now(timezone.utc) + timedelta(hours=1), **overrides}
    db.employee_invitations_collection.insert_one(record)
    return token, record


def test_employee_token_cannot_become_admin_with_same_email(client, db):
    employee = account(db)
    account(db, "admin", role="superadmin")
    assert client.get("/admin/me", headers=bearer(employee)).status_code == 401


def test_admin_token_cannot_become_employee(client, db):
    account(db)
    admin = account(db, "admin", role="admin")
    assert client.get("/api/employee/profile", headers=bearer(admin, "admin")).status_code == 401


def test_legacy_token_rejected(client, db):
    admin = account(db, "admin", role="admin")
    token = jwt.encode({"sub": admin["email"], "exp": datetime.now(timezone.utc) + timedelta(hours=1)},
                       auth.SECRET_KEY, algorithm=auth.ALGORITHM)
    assert client.get("/admin/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


@pytest.mark.parametrize("claim,value", [("iss", "other"), ("aud", "other"), ("sid", "unknown"),
                                         ("exp", 1), ("sub", "not-an-object-id"), ("account_type", "employee")])
def test_invalid_claims_rejected(client, db, claim, value):
    admin = account(db, "admin", role="admin")
    token = bearer(admin, "admin")["Authorization"].split()[1]
    claims = jwt.decode(token, options={"verify_signature": False})
    claims[claim] = value
    token = jwt.encode(claims, auth.SECRET_KEY, algorithm=auth.ALGORITHM)
    assert client.get("/admin/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_current_account_status_and_session_scope_are_checked(client, db):
    admin = account(db, "admin", role="admin")
    headers = bearer(admin, "admin")
    assert client.get("/admin/me", headers=headers).status_code == 200
    db.admins_collection.update_one({"_id": admin["_id"]}, {"$set": {"is_active": False}})
    assert client.get("/admin/me", headers=headers).status_code == 403
    db.admins_collection.update_one({"_id": admin["_id"]}, {"$set": {"is_active": True, "organization_id": str(ObjectId())}})
    assert client.get("/admin/me", headers=headers).status_code == 401


def test_report_permission_denied_then_granted_without_new_token(client, db):
    admin = account(db, "admin")
    headers = bearer(admin, "admin")
    assert client.get("/admin/reports/attendance", headers=headers).status_code == 403
    db.admins_collection.update_one({"_id": admin["_id"]}, {"$set": {"allowed_features": ["reports"]}})
    assert client.get("/admin/reports/attendance", headers=headers).status_code == 200


def test_report_is_tenant_scoped(client, db):
    owner = account(db, "admin", role="owner")
    employee = account(db, org=owner["organization_id"])
    other = account(db, email="other@example.test")
    for person in [employee, other]:
        db.attendance_logs_collection.insert_one({"user_id": str(person["_id"]), "type": "check-in",
                                                   "timestamp": datetime.now(timezone.utc)})
    response = client.get("/admin/reports/attendance", headers=bearer(owner, "admin"))
    assert response.status_code == 200
    assert [row["user_id"] for row in response.json()["records"]] == [str(employee["_id"])]


def test_logout_revokes_session(client, db):
    admin = account(db, "admin", role="admin")
    headers = bearer(admin, "admin")
    assert client.post("/auth/logout", headers=headers).status_code == 200
    assert client.get("/admin/me", headers=headers).status_code == 401


def test_registration_requires_invitation(client, db):
    assert client.post("/register", json=registration()).status_code == 403
    assert db.employees_collection.count_documents({}) == 0


@pytest.mark.parametrize("override", [{"consumed": True}, {"email": "someone-else@example.test"},
                                      {"expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc)}])
def test_invalid_invitation_rejected(client, db, override):
    token, _ = invitation(db, **override)
    assert client.post("/register", json=registration(invitation_token=token)).status_code == 403


def test_invitation_controls_membership_and_is_single_use(client, db):
    token, invite = invitation(db)
    response = client.post("/register", json=registration(invitation_token=token))
    assert response.status_code == 200, response.text
    employee = db.employees_collection.find_one({"email": "new@example.test"})
    for field in ["organization_id", "employee_id", "department", "designation", "employee_type"]:
        assert employee[field] == invite[field]
    assert db.employee_invitations_collection.find_one({"_id": invite["_id"]})["consumed"]
    assert client.post("/register", json=registration(invitation_token=token)).status_code == 403


def test_employee_login_does_not_try_admin_password(client, db):
    account(db)
    admin = account(db, "admin", role="admin")
    db.admins_collection.update_one({"_id": admin["_id"]}, {"$set": {"hashed_password": auth.get_password_hash("admin-password")}})
    assert client.post("/login", json={"email": admin["email"], "password": "admin-password"}).status_code == 401
    assert client.post("/login", json={"email": admin["email"], "password": "correct-password"}).status_code == 200


def test_environment_admin_password_is_not_a_login(client, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAIL", "env-admin@example.test")
    monkeypatch.setenv("ADMIN_PASSWORD", "environment-password")
    body = {"email": "env-admin@example.test", "password": "environment-password"}
    assert client.post("/admin/login", json=body).status_code == 401
    assert client.post("/login", json=body).status_code == 401


def test_owner_cannot_reset_another_organizations_password(client, db):
    owner = account(db, "admin", role="owner")
    employee = account(db)
    response = client.post(f"/admin/employees/{employee['email']}/reset-password",
                           json={"password": "replacement-password"}, headers=bearer(owner, "admin"))
    assert response.status_code == 404
    assert db.employees_collection.find_one({"_id": employee["_id"]})["hashed_password"] == employee["hashed_password"]


def test_attendance_requires_authentication(client):
    assert client.post("/smart-attendance", json={"email": "any@example.test"}).status_code == 401
    assert client.post("/verify-presence", json={"email": "any@example.test"}).status_code == 401


def test_cors_rejects_unlisted_origin(client):
    response = client.options("/login", headers={"Origin": "https://evil.example.test", "Access-Control-Request-Method": "POST"})
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers
    response = client.options("/login", headers={"Origin": "https://admin.example.test", "Access-Control-Request-Method": "POST"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://admin.example.test"


def test_all_admin_dependencies_have_explicit_policies():
    from main import app
    from fastapi.routing import APIRoute

    def uses_admin(dependant):
        return dependant.call is auth.get_current_admin or any(uses_admin(d) for d in dependant.dependencies)

    unprotected = []
    for route in app.routes:
        if isinstance(route, APIRoute) and uses_admin(route.dependant):
            if not auth._has_explicit_admin_policy(route.dependant):
                unprotected.append(route.path)
    assert not unprotected


def test_undeclared_admin_route_fails_closed(db):
    from fastapi import FastAPI, Depends
    from fastapi.testclient import TestClient
    app = FastAPI()

    @app.get("/missing-policy")
    async def missing_policy(admin=Depends(auth.get_current_admin)):
        return {"allowed": True}

    admin = account(db, "admin", role="superadmin")
    assert TestClient(app).get("/missing-policy", headers=bearer(admin, "admin")).status_code == 403


def test_owner_cannot_create_or_promote_superadmin(client, db):
    owner = account(db, "admin", role="owner")
    subordinate = account(db, "admin", email="subordinate@example.test", org=owner["organization_id"])
    headers = bearer(owner, "admin")
    response = client.post("/admin/sub-admins", headers=headers, json={
        "email": "escalation@example.test", "password": "some-password", "full_name": "Escalation",
        "role": "superadmin", "organization_id": owner["organization_id"],
    })
    assert response.status_code == 403, response.text
    response = client.put(f"/admin/sub-admins/{subordinate['email']}/permissions", headers=headers,
                          json={"role": "superadmin", "allowed_features": []})
    assert response.status_code == 403
    assert db.admins_collection.find_one({"_id": subordinate["_id"]})["role"] == "hr"


def test_admin_login_issues_usable_account_id_token(client, db):
    admin = account(db, "admin", role="admin")
    response = client.post("/admin/login", json={"email": admin["email"], "password": "correct-password"})
    assert response.status_code == 200
    token = response.json()["access_token"]
    claims = auth.decode_access_token(token)
    assert claims["sub"] == str(admin["_id"])
    assert claims["account_type"] == "admin"
    assert client.get("/admin/me", headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_invitation_issuance_and_cross_org_restriction(client, db):
    org_id = ObjectId()
    db.organizations_collection.insert_one({"_id": org_id, "name": "Test Org"})
    owner = account(db, "admin", role="owner", org=str(org_id))
    headers = bearer(owner, "admin")
    body = {"email": "new@example.test", "employee_id": "EMP-01", "designation": "Executive", "department": "Sales"}
    response = client.post("/admin/employee-invitations", headers=headers, json=body)
    assert response.status_code == 201, response.text
    token = response.json()["invitation_token"]
    stored = db.employee_invitations_collection.find_one({"_id": invitation_digest(token)})
    assert stored["organization_id"] == str(org_id)
    assert token not in str(stored)
    body["organization_id"] = str(ObjectId())
    assert client.post("/admin/employee-invitations", headers=headers, json=body).status_code == 403


def test_invitation_concurrent_consumption_has_one_winner(db):
    from invitations import consume_invitation
    from fastapi import HTTPException
    token, _ = invitation(db)

    async def race():
        return await asyncio.gather(*(consume_invitation(token, "new@example.test") for _ in range(2)),
                                    return_exceptions=True)

    outcomes = asyncio.run(race())
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert sum(isinstance(item, HTTPException) for item in outcomes) == 1


def test_all_admin_urls_require_admin_identity():
    from main import app
    from fastapi.routing import APIRoute

    def uses_admin(dependant):
        return dependant.call is auth.get_current_admin or any(uses_admin(d) for d in dependant.dependencies)

    # OAuth callbacks authenticate via opaque single-use state, not a bearer header.
    explicitly_public = {"/admin/login", "/admin/register-organization", "/admin/onboarding/digilocker/callback"}
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/admin/") and route.path not in explicitly_public:
            assert uses_admin(route.dependant), route.path


def test_weak_production_secret_stops_startup():
    import os
    import subprocess
    import sys
    from pathlib import Path
    env = dict(os.environ, APP_ENV="production", JWT_SECRET="supersecretkey")
    result = subprocess.run([sys.executable, "-c", "import auth"],
                            cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert "JWT_SECRET must be" in result.stderr


@pytest.mark.parametrize("path", ["/admin/onboarding/digilocker/callback",
                                  "/api/employee/onboarding/digilocker/callback",
                                  "/hrms/onboarding/digilocker/callback"])
def test_oauth_callback_rejects_forged_employee_state(client, path):
    response = client.get(path, params={"code": "forged", "state": f"{ObjectId()}:org:nonce"})
    assert response.status_code == 400


def test_oauth_state_is_one_time_and_cancellation_is_escaped(client):
    from oauth_state import create_oauth_state
    state = asyncio.run(create_oauth_state("test@example.test", "org", "private-verifier"))
    params = {"code": "cancelled", "state": state, "error": "<script>alert(1)</script>"}
    response = client.get("/api/employee/onboarding/digilocker/callback", params=params)
    assert response.status_code == 200
    assert "<script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert client.get("/api/employee/onboarding/digilocker/callback", params=params).status_code == 400


def test_oauth_callback_cannot_cross_tenants(client, db):
    from oauth_state import create_oauth_state
    onboarding_id = ObjectId()
    db.onboardings_collection.insert_one({"_id": onboarding_id, "organization_id": "other-org"})
    state = asyncio.run(create_oauth_state(str(onboarding_id), "requesting-org", "private-verifier"))
    response = client.get("/admin/onboarding/digilocker/callback", params={"code": "unused", "state": state})
    assert response.status_code == 404


def test_expired_oauth_state_is_rejected(client, db):
    from oauth_state import create_oauth_state
    state = asyncio.run(create_oauth_state("test@example.test", "org", "private-verifier"))
    db.oauth_states_collection.update_many({}, {"$set": {"expires_at": datetime(2000, 1, 1, tzinfo=timezone.utc)}})
    response = client.get("/api/employee/onboarding/digilocker/callback", params={"code": "unused", "state": state})
    assert response.status_code == 400


def test_cross_tenant_wfh_admin_commands_rejected(client, db):
    org_a_admin = account(db, "admin", email="admin_a@example.test", role="owner")
    org_b_employee = account(db, "employee", email="emp_b@example.test", org=str(ObjectId()))
    db.employees_collection.update_one({"_id": org_b_employee["_id"]}, {"$set": {"employee_type": "wfh"}})
    headers = bearer(org_a_admin, "admin")
    email = org_b_employee["email"]

    assert client.post(f"/admin/wfh/employee/{email}/trigger-screenshot", headers=headers).status_code == 404
    assert client.post(f"/admin/wfh/employee/{email}/force-end", headers=headers, json={"reason": "test"}).status_code == 404
    assert client.post(f"/admin/wfh/employee/{email}/force-logout", headers=headers, json={"reason": "test"}).status_code == 404
    assert client.get(f"/admin/wfh/employee/{email}/screenshots", headers=headers).status_code == 404
    assert client.get(f"/admin/wfh/employee/{email}/activity", headers=headers).status_code == 404
    assert client.get(f"/admin/wfh/employee/{email}/apps", headers=headers).status_code == 404
    assert client.get(f"/admin/wfh/employee/{email}/productivity", headers=headers).status_code == 404
    assert client.get(f"/admin/wfh/employee/{email}/timeline", headers=headers).status_code == 404
    assert client.get(f"/admin/wfh/employee/{email}/tasks", headers=headers).status_code == 404


def test_wfh_face_check_verifies_on_server(client, db, monkeypatch):
    import base64
    import io
    from PIL import Image
    import routers.wfh_employee as wfh_emp

    emp = account(db, "employee", email="wfh_face@example.test")
    db.employees_collection.update_one(
        {"_id": emp["_id"]},
        {"$set": {"employee_type": "wfh", "face_embedding": [0.1, 0.2, 0.3]}}
    )
    session_id = ObjectId()
    db.wfh_sessions_collection.insert_one({
        "_id": session_id,
        "employee_id": str(emp["_id"]),
        "email": emp["email"],
        "organization_id": emp["organization_id"],
        "status": "active",
    })
    monkeypatch.setattr(wfh_emp, "verify_face", lambda img, emb: (False, 0.99))

    buf = io.BytesIO()
    Image.new("RGB", (3, 3), "green").save(buf, format="PNG")
    valid_png_b64 = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()

    response = client.post(
        "/api/wfh/face-check",
        headers=bearer(emp, "employee"),
        json={
            "session_id": str(session_id),
            "passed": True,
            "face_score": 0.01,
            "image_base64": valid_png_b64,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["passed"] is False
    saved = db.wfh_face_checks_collection.find_one({"session_id": str(session_id)})
    assert saved["passed"] is False
    assert db.wfh_alerts_collection.find_one({"employee_id": str(emp["_id"]), "type": "WFH_FACE_CHECK_FAILED"}) is not None


def test_password_change_and_reset_invalidate_existing_sessions(client, db):
    owner = account(db, "admin", email="owner_pw@example.test", role="owner")
    emp = account(db, "employee", email="emp_pw@example.test", org=owner["organization_id"])
    emp_headers = bearer(emp, "employee")

    assert client.get("/api/employee/profile", headers=emp_headers).status_code == 200
    change_res = client.post(
        "/api/me/change-password",
        headers=emp_headers,
        json={"old_password": "correct-password", "new_password": "new-password-123"},
    )
    assert change_res.status_code == 200, change_res.text
    assert client.get("/api/employee/profile", headers=emp_headers).status_code == 401

    updated_emp = db.employees_collection.find_one({"_id": emp["_id"]})
    emp_headers_2 = bearer(updated_emp, "employee")
    assert client.get("/api/employee/profile", headers=emp_headers_2).status_code == 200

    reset_res = client.post(
        f"/admin/employees/{emp['email']}/reset-password",
        headers=bearer(owner, "admin"),
        json={"password": "admin-reset-password-456"},
    )
    assert reset_res.status_code == 200, reset_res.text
    assert client.get("/api/employee/profile", headers=emp_headers_2).status_code == 401


def test_absence_alert_expiry_and_atomic_idempotent_deduction(client, db, monkeypatch):
    import absence_alerts
    org_id = str(ObjectId())
    emp = account(db, "employee", email="absent_emp@example.test", org=org_id)

    expired_token = "expired-alert-token"
    db.absence_alerts_collection.insert_one({
        "token": expired_token,
        "employee_id": str(emp["_id"]),
        "employee_email": emp["email"],
        "employee_name": emp["full_name"],
        "organization_id": org_id,
        "date": "2026-11-10",
        "status": "pending",
        "created_at": datetime.now(timezone.utc) - timedelta(days=5),
        "token_expires_at": datetime.now(timezone.utc) - timedelta(days=2),
    })
    expired_res = client.post("/api/absence-alert/action", data={"token": expired_token, "action": "send_mail"})
    assert expired_res.status_code == 200
    assert "Alert Link Expired" in expired_res.text
    assert db.leave_requests_collection.count_documents({"email": emp["email"], "start_date": "2026-11-10"}) == 0

    valid_token = "valid-alert-token"
    db.absence_alerts_collection.insert_one({
        "token": valid_token,
        "employee_id": str(emp["_id"]),
        "employee_email": emp["email"],
        "employee_name": emp["full_name"],
        "organization_id": org_id,
        "date": "2026-11-11",
        "status": "pending",
        "created_at": datetime.now(timezone.utc),
        "token_expires_at": datetime.now(timezone.utc) + timedelta(hours=72),
    })

    calls = {"mail": 0}
    def fake_send_email_sync(smtp_cfg, to_email, subject, html, *args, **kwargs):
        calls["mail"] += 1
        if calls["mail"] == 1:
            return (False, "SMTP temporary failure")
        return (True, "Delivered")

    monkeypatch.setattr(absence_alerts, "_send_email_sync", fake_send_email_sync)

    first_res = client.post("/api/absence-alert/action", data={"token": valid_token, "action": "send_mail"})
    assert first_res.status_code == 200
    assert "Email Delivery Failed" in first_res.text
    assert db.leave_requests_collection.count_documents({"email": emp["email"], "start_date": "2026-11-11"}) == 1

    second_res = client.post("/api/absence-alert/action", data={"token": valid_token, "action": "send_mail"})
    assert second_res.status_code == 200
    assert "Warning Email Sent to Employee" in second_res.text
    assert db.leave_requests_collection.count_documents({"email": emp["email"], "start_date": "2026-11-11"}) == 1

    third_res = client.post("/api/absence-alert/action", data={"token": valid_token, "action": "send_mail"})
    assert third_res.status_code == 200
    assert "Already Sent" in third_res.text
    assert db.leave_requests_collection.count_documents({"email": emp["email"], "start_date": "2026-11-11"}) == 1


def test_field_offline_sync_idempotency_and_ownership(client, db):
    emp = account(db, "employee", email="field_sync@example.test")
    headers = bearer(emp, "employee")
    batch_payload = {
        "attendance_logs": [
            {
                "offline_id": "att-1",
                "offline_owner": emp["email"],
                "type": "check-in",
                "latitude": 12.97,
                "longitude": 77.59,
                "timestamp": "2026-11-10T09:00:00Z",
            },
            {
                "offline_id": "att-foreign",
                "offline_owner": "other_user@example.test",
                "type": "check-in",
                "latitude": 12.97,
                "longitude": 77.59,
                "timestamp": "2026-11-10T09:05:00Z",
            },
        ],
        "pings": [
            {
                "offline_id": "loc-1",
                "offline_owner": emp["email"],
                "lat": 12.97,
                "lng": 77.59,
                "recorded_at": "2026-11-10T09:10:00Z",
            }
        ],
    }
    res1 = client.post("/api/field/sync/batch", headers=headers, json=batch_payload)
    assert res1.status_code == 200
    ack1 = res1.json()["acknowledged_ids"]
    assert ack1["attendance"] == ["att-1"]
    assert ack1["pings"] == ["loc-1"]
    assert db.attendance_logs_collection.count_documents({"offline_id": "att-1"}) == 1
    assert db.attendance_logs_collection.count_documents({"offline_id": "att-foreign"}) == 0

    res2 = client.post("/api/field/sync/batch", headers=headers, json=batch_payload)
    assert res2.status_code == 200
    assert db.attendance_logs_collection.count_documents({"offline_id": "att-1"}) == 1
    assert db.location_pings_collection.count_documents({"offline_id": "loc-1"}) == 1


def test_health_and_ready_return_503_when_db_down(client, monkeypatch):
    import database

    class FailingAdmin:
        async def command(self, cmd):
            raise RuntimeError("mongo unreachable")

    class FailingClient:
        admin = FailingAdmin()

    monkeypatch.setattr(database, "client", FailingClient())
    assert client.get("/live").status_code == 200
    assert client.get("/ready").status_code == 503
    assert client.get("/health").status_code == 503

