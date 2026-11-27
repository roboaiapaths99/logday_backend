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
