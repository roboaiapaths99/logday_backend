import asyncio
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from bson import ObjectId
from fastapi import HTTPException

from routers import field_ops, reports_nudges


class _FakeUpdateResult:
    def __init__(self, matched_count: int):
        self.matched_count = matched_count


class _FakeAggregateCursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length=None):
        return list(self._docs)


class _FakeFindCursor:
    def __init__(self, docs):
        self._docs = docs

    def sort(self, *args, **kwargs):
        return self

    async def to_list(self, length=None):
        return list(self._docs)


def test_visit_plan_approval_and_edit_validate_objectid_and_tenant_scope(monkeypatch):
    async def _run():
        admin = SimpleNamespace(
            email="mgr@org-a.com",
            role="manager",
            organization_id="org_a",
        )

        with pytest.raises(HTTPException) as exc:
            await field_ops.process_visit_plan("not-an-object-id", "approve", admin=admin)
        assert exc.value.status_code == 400

        with pytest.raises(HTTPException) as exc2:
            await field_ops.update_visit_plan("invalid_id", {"stops": []}, admin=admin)
        assert exc2.value.status_code == 400

        captured_queries = []

        async def fake_update_one(query, update):
            captured_queries.append(query)
            return _FakeUpdateResult(matched_count=0)

        monkeypatch.setattr(field_ops, "get_scoped_employee_emails", AsyncMock(return_value=["team1@org-a.com"]))
        monkeypatch.setattr(field_ops, "get_scoped_employee_employee_ids", AsyncMock(return_value=["EMP1"]))
        monkeypatch.setattr(field_ops.visit_plans_collection, "update_one", fake_update_one)

        valid_oid = str(ObjectId())
        with pytest.raises(HTTPException) as exc3:
            await field_ops.process_visit_plan(valid_oid, "approve", admin=admin)
        assert exc3.value.status_code == 404
        assert captured_queries[0]["organization_id"] == "org_a"
        assert set(captured_queries[0]["employee_id"]["$in"]) == {"team1@org-a.com", "EMP1"}

    asyncio.run(_run())


def test_reimbursement_and_expense_approval_enforce_manager_scope_and_valid_id(monkeypatch):
    async def _run():
        manager = SimpleNamespace(
            email="mgr@org-a.com",
            role="manager",
            organization_id="org_a",
        )

        with pytest.raises(HTTPException) as exc:
            await field_ops.process_km_reimbursement("bad-id", "approve", admin=manager)
        assert exc.value.status_code == 400

        with pytest.raises(HTTPException) as exc2:
            await field_ops.admin_update_expense("bad-id", {"action": "approve"}, admin=manager)
        assert exc2.value.status_code == 400

        km_queries = []
        exp_queries = []

        async def fake_km_update(query, update):
            km_queries.append(query)
            return _FakeUpdateResult(matched_count=1)

        async def fake_exp_update(query, update):
            exp_queries.append(query)
            return _FakeUpdateResult(matched_count=1)

        monkeypatch.setattr(field_ops, "get_scoped_employee_emails", AsyncMock(return_value=["rep@org-a.com"]))
        monkeypatch.setattr(field_ops, "get_scoped_employee_employee_ids", AsyncMock(return_value=["EMP-REP"]))
        monkeypatch.setattr(field_ops.km_reimbursements_collection, "update_one", fake_km_update)
        monkeypatch.setattr(field_ops.expense_claims_collection, "update_one", fake_exp_update)

        cid = str(ObjectId())
        await field_ops.process_km_reimbursement(cid, "approve", admin=manager)
        await field_ops.admin_update_expense(cid, {"action": "approve"}, admin=manager)

        assert km_queries[0]["organization_id"] == "org_a"
        assert set(km_queries[0]["employee_id"]["$in"]) == {"rep@org-a.com", "EMP-REP"}
        assert exp_queries[0]["organization_id"] == "org_a"
        assert set(exp_queries[0]["employee_id"]["$in"]) == {"rep@org-a.com", "EMP-REP"}

    asyncio.run(_run())


def test_mark_nudge_read_enforces_tenant_and_recipient_scope(monkeypatch):
    async def _run():
        emp = {"email": "alice@org-a.com", "organization_id": "org_a"}
        captured = []

        async def fake_update_one(query, update):
            captured.append(query)
            return _FakeUpdateResult(matched_count=0)

        monkeypatch.setattr(reports_nudges.nudge_logs_collection, "update_one", fake_update_one)

        nid = str(ObjectId())
        with pytest.raises(HTTPException) as exc:
            await reports_nudges.mark_nudge_read(nid, emp=emp)
        assert exc.value.status_code == 404
        assert captured[0]["organization_id"] == "org_a"
        assert {"recipients": "alice@org-a.com"} in captured[0]["$or"]

    asyncio.run(_run())


def test_agent_performance_report_scopes_km_pipeline_and_batches_employee_lookup(monkeypatch):
    async def _run():
        manager = SimpleNamespace(
            email="mgr@org-a.com",
            role="manager",
            organization_id="org_a",
        )
        monkeypatch.setattr(reports_nudges, "get_scoped_employee_employee_ids", AsyncMock(return_value=["EMP1"]))
        monkeypatch.setattr(reports_nudges, "get_scoped_employee_emails", AsyncMock(return_value=["emp1@org-a.com"]))
        monkeypatch.setattr(reports_nudges, "get_scoped_employee_ids", AsyncMock(return_value=["obj1"]))

        km_pipelines = []

        def fake_visit_agg(pipeline):
            return _FakeAggregateCursor([{"_id": "emp1@org-a.com", "total_visits": 3, "leads": 1, "orders": 1}])

        def fake_km_agg(pipeline):
            km_pipelines.append(pipeline)
            return _FakeAggregateCursor([{"_id": "emp1@org-a.com", "total_km": 42.5}])

        emp_queries = []

        def fake_emp_find(query, projection=None):
            emp_queries.append((query, projection))
            return _FakeFindCursor([{"email": "emp1@org-a.com", "employee_id": "EMP1", "full_name": "Emp One"}])

        monkeypatch.setattr(reports_nudges.visit_logs_collection, "aggregate", fake_visit_agg)
        monkeypatch.setattr(reports_nudges.km_reimbursements_collection, "aggregate", fake_km_agg)
        monkeypatch.setattr(reports_nudges.employees_collection, "find", fake_emp_find)

        res = await reports_nudges.agent_performance_report("2026-11-01", "2026-11-30", admin=manager)
        assert len(res) == 1
        assert res[0]["total_km"] == 42.5
        assert "employee_id" in km_pipelines[0][0]["$match"]
        assert set(km_pipelines[0][0]["$match"]["employee_id"]["$in"]) == {"EMP1", "emp1@org-a.com", "obj1"}
        assert len(emp_queries) == 1
        assert emp_queries[0][0]["organization_id"] == "org_a"

    asyncio.run(_run())


def test_admin_stats_deduplicates_multiple_same_day_checkins(monkeypatch):
    from routers import admin_core

    async def _run():
        admin = SimpleNamespace(
            email="admin@org-a.com",
            role="owner",
            organization_id="org_a",
        )
        monkeypatch.setattr(admin_core.employees_collection, "count_documents", AsyncMock(return_value=3))
        monkeypatch.setattr(
            admin_core.employees_collection,
            "find",
            lambda q, proj=None: _FakeFindCursor([
                {"_id": "id1", "email": "emp1@org-a.com"},
                {"_id": "id2", "email": "emp2@org-a.com"},
                {"_id": "id3", "email": "emp3@org-a.com"},
            ]),
        )
        monkeypatch.setattr(admin_core.settings_collection, "find_one", AsyncMock(return_value={"timezone_offset": 330}))
        monkeypatch.setattr(
            admin_core.attendance_logs_collection,
            "find",
            lambda q: _FakeFindCursor([
                {"user_id": "id1", "email": "emp1@org-a.com", "type": "check-in", "status": "SUCCESS", "is_late": False},
                {"user_id": "id1", "email": "emp1@org-a.com", "type": "check-in", "status": "Present", "is_late": True},
            ]),
        )
        monkeypatch.setattr(admin_core.alerts_collection, "count_documents", AsyncMock(return_value=0))
        monkeypatch.setattr(admin_core.leave_requests_collection, "count_documents", AsyncMock(return_value=0))
        monkeypatch.setattr(admin_core.holidays_collection, "find_one", AsyncMock(return_value=None))

        stats = await admin_core.get_admin_stats(admin=admin)
        assert stats["clocked_in_today"] == 1
        assert stats["late_arrivals_today"] == 0
        assert stats["absent_today"] in (0, 2)

    asyncio.run(_run())


def test_attendance_chart_supports_30_days_and_regularize_enforces_scope(monkeypatch):
    from routers import admin_core

    async def _run():
        admin = SimpleNamespace(
            email="admin@org-a.com",
            role="owner",
            organization_id="org_a",
        )
        monkeypatch.setattr(
            admin_core.employees_collection,
            "find",
            lambda q, proj=None: _FakeFindCursor([
                {"_id": "id1", "email": "emp1@org-a.com"},
            ]),
        )
        monkeypatch.setattr(admin_core.settings_collection, "find_one", AsyncMock(return_value={"timezone_offset": 330}))
        monkeypatch.setattr(
            admin_core.attendance_logs_collection,
            "find",
            lambda q: _FakeFindCursor([]),
        )

        chart = await admin_core.get_attendance_chart(days=30, admin=admin)
        assert len(chart) == 30

        # Regularize attendance for an employee in scope
        monkeypatch.setattr(
            admin_core.employees_collection,
            "find_one",
            AsyncMock(return_value={"_id": "id1", "email": "emp1@org-a.com", "full_name": "Emp One", "organization_id": "org_a", "employee_type": "desk"}),
        )
        monkeypatch.setattr(admin_core, "get_effective_org_settings", AsyncMock(return_value={"timezone_offset": 330}))
        monkeypatch.setattr(admin_core.leave_requests_collection, "find_one", AsyncMock(return_value=None))

        inserted_docs = []

        async def fake_insert_one(doc):
            inserted_docs.append(doc)

        monkeypatch.setattr(admin_core.attendance_logs_collection, "insert_one", fake_insert_one)

        reg_res = await admin_core.admin_regularize_attendance(
            {"email": "emp1@org-a.com", "date": "2026-11-10", "check_in_time": "09:00", "check_out_time": "18:00", "reason": "Forgot checkout"},
            current_admin=admin,
        )
        assert reg_res["status"] == "success"
        assert len(inserted_docs) == 2
        assert {d["type"] for d in inserted_docs} == {"check-in", "check-out"}
        assert all(d["status"] == "SUCCESS" for d in inserted_docs)

    asyncio.run(_run())


def test_import_employees_blocks_cross_tenant_overwrite_and_revokes_sessions(monkeypatch):
    from routers import admin_core
    import database

    async def _run():
        admin_org_a = SimpleNamespace(
            email="admin@org-a.com",
            role="owner",
            organization_id="org_a",
        )
        # CSV tries to overwrite victim@org-b.com (in org_b) and update own@org-a.com (in org_a)
        csv_bytes = (
            b"full_name,email,employee_id,password,designation,department,employee_type\n"
            b"Hijacked Name,victim@org-b.com,EMP999,NewPass123!,Director,Exec,desk\n"
            b"Updated Own,own@org-a.com,EMP001,ResetPass123!,Lead,Eng,wfh\n"
        )

        async def fake_find_one(q):
            if q.get("email") == "victim@org-b.com":
                return {"_id": "oid_b", "email": "victim@org-b.com", "organization_id": "org_b"}
            if q.get("email") == "own@org-a.com":
                return {"_id": "oid_a", "email": "own@org-a.com", "organization_id": "org_a"}
            return None

        updated_calls = []

        async def fake_update_one(flt, doc):
            updated_calls.append((flt, doc))

        revoked_calls = []

        async def fake_revoke_many(flt, doc):
            revoked_calls.append((flt, doc))

        monkeypatch.setattr(admin_core.employees_collection, "find_one", fake_find_one)
        monkeypatch.setattr(admin_core.employees_collection, "update_one", fake_update_one)
        monkeypatch.setattr(admin_core, "get_password_hash_async", AsyncMock(return_value="hashed_secret"))
        monkeypatch.setattr(database.auth_sessions_collection, "update_many", fake_revoke_many)

        res = await admin_core.admin_import_employees(file=csv_bytes, current_admin=admin_org_a)
        assert res["updated"] == 1
        assert len(res["errors"]) == 1
        assert "another organization" in res["errors"][0]["error"]
        assert len(updated_calls) == 1
        assert updated_calls[0][0] == {"_id": "oid_a", "organization_id": "org_a"}
        assert updated_calls[0][1]["$inc"] == {"auth_version": 1}
        assert len(revoked_calls) == 1
        assert revoked_calls[0][0] == {"account_id": "oid_a", "account_type": "employee"}

    asyncio.run(_run())


def test_export_logs_helper_filters_by_month_and_date_and_enriches_names(monkeypatch):
    from datetime import datetime, timezone
    from routers import admin_core

    async def _run():
        admin = SimpleNamespace(
            email="admin@org-a.com",
            role="owner",
            organization_id="org_a",
        )
        monkeypatch.setattr(admin_core.settings_collection, "find_one", AsyncMock(return_value={"timezone_offset": 330}))
        monkeypatch.setattr(
            admin_core.employees_collection,
            "find",
            lambda q, proj=None: _FakeFindCursor([
                {"_id": "emp_id_1", "email": "emp1@org-a.com", "full_name": "Alice Smith"},
            ]),
        )

        captured_queries = []

        def fake_logs_find(q):
            captured_queries.append(q)
            return _FakeFindCursor([
                {"user_id": "emp_id_1", "timestamp": datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc), "type": "check-in"},
            ])

        monkeypatch.setattr(admin_core.attendance_logs_collection, "find", fake_logs_find)

        logs, tz_offset, period_label = await admin_core._build_export_logs_query_and_enrich(
            admin, month="2026-11", date="2026-11-10"
        )
        assert period_label == "2026-11-10"
        assert tz_offset == 330
        assert len(logs) == 1
        assert logs[0]["full_name"] == "Alice Smith"
        assert logs[0]["email"] == "emp1@org-a.com"
        assert "timestamp" in captured_queries[-1]
        assert "$or" in captured_queries[-1]

    asyncio.run(_run())


def test_attendance_report_filters_flyfield_and_wfh(monkeypatch):
    from datetime import datetime, timezone
    from routers import reports_nudges

    async def _run():
        admin = SimpleNamespace(
            email="admin@org-a.com",
            role="owner",
            organization_id="org_a",
        )
        monkeypatch.setattr(reports_nudges, "get_scoped_employee_ids", AsyncMock(return_value=["507f1f77bcf86cd799439011", "507f1f77bcf86cd799439012"]))
        monkeypatch.setattr(reports_nudges, "get_scoped_employee_emails", AsyncMock(return_value=["fly@org-a.com", "wfh@org-a.com"]))
        monkeypatch.setattr(
            reports_nudges.attendance_logs_collection,
            "find",
            lambda q: _FakeFindCursor([
                {"_id": "l1", "user_id": "507f1f77bcf86cd799439011", "email": "fly@org-a.com", "type": "check-in", "timestamp": datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc)},
                {"_id": "l2", "user_id": "507f1f77bcf86cd799439012", "email": "wfh@org-a.com", "type": "wfh_checkin", "attendance_type": "wfh", "timestamp": datetime(2026, 11, 10, 4, 5, tzinfo=timezone.utc)},
            ]),
        )
        monkeypatch.setattr(
            reports_nudges.employees_collection,
            "find",
            lambda q, proj=None: _FakeFindCursor([
                {"_id": "507f1f77bcf86cd799439011", "email": "fly@org-a.com", "full_name": "Fly Agent", "employee_type": "field", "field_mode": "flyfield"},
                {"_id": "507f1f77bcf86cd799439012", "email": "wfh@org-a.com", "full_name": "WFH User", "employee_type": "wfh", "field_mode": "planned"},
            ]),
        )

        fly_report = await reports_nudges.attendance_report(employee_type="flyfield", admin=admin)
        assert fly_report["summary"]["total_records"] == 1
        assert fly_report["records"][0]["email"] == "fly@org-a.com"
        assert fly_report["records"][0]["employee_type"] == "flyfield"

        wfh_report = await reports_nudges.attendance_report(employee_type="wfh", admin=admin)
        assert wfh_report["summary"]["total_records"] == 1
        assert wfh_report["records"][0]["email"] == "wfh@org-a.com"
        assert wfh_report["records"][0]["employee_type"] == "wfh"

    asyncio.run(_run())



