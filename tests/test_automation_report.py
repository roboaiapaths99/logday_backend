import asyncio
import io
import zipfile
from datetime import datetime, timezone
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest
from services import automation_report as report
from test_security import account, bearer

NOW = datetime(2026, 12, 1, tzinfo=timezone.utc)
SETTINGS = {'timezone_offset': 330, 'office_start_time': '10:00', 'office_end_time': '18:00',
            'late_grace_mins': 15, 'half_day_late_cutoff_time': '11:30'}


def test_full_month_calendar_and_removed_columns():
    assert report.working_days('2026-11', []) == 21
    assert report.working_days('2026-11', [{'days_off': [1, 2, 2]}, {'date': '2026-11-02'}]) == 20
    assert report.working_days('2028-02', []) == 21
    assert not {'approved', 'short', 'review', 'hours', 'pending_requests'} & {key for key, _ in report.COLUMNS}
    assert report.calendar_heading({'days_in_month': 30, 'working_days_by_org': {'a': 20}}) == 'Total Working Days: 20   |   Days in Month: 30'


def event(day, time, kind='check-in', **extra):
    # Input clock is India local time.
    from datetime import timedelta
    stamp = datetime.fromisoformat(f'2026-11-{day:02d}T{time}:00+00:00') - timedelta(minutes=330)
    return dict(timestamp=stamp, type=kind, **extra)


def test_classification_unique_days_late_boundary_and_incomplete():
    logs = [event(2, '10:15'), event(2, '10:15'), event(2, '18:00', 'check-out'),
            event(3, '10:16'), event(3, '18:00', 'check-out'),
            event(4, '11:30'), event(4, '18:00', 'check-out'), event(5, '10:00')]
    row = report.summarize({}, logs, [], [], SETTINGS, '2026-11', NOW)
    assert row['present'] == 2
    assert row['half'] == 1
    assert row['late'] == 1  # Half day is counted only as half day, not double-counted in late.
    assert row['review'] == 1
    assert row['absent'] == 17  # 21 weekdays minus four attendance dates.


def test_holidays_leaves_joining_and_future_days():
    employee = {'joining_date': '2026-11-02'}
    leaves = [dict(start_date='2026-11-03', end_date='2026-11-03', status='approved'),
              dict(start_date='2026-11-04', end_date='2026-11-04', status='pending')]
    row = report.summarize(employee, [], leaves, [{'days_off': [2]}], SETTINGS, '2026-11',
                           datetime(2026, 11, 6, tzinfo=timezone.utc))
    assert row['absent'] == 1  # Nov 5 only; Nov 6 is in progress.
    assert row['approved'] == 1
    assert row['pending_requests'] == 1


def test_approved_half_day_without_logs_is_not_full_absence():
    leave = dict(start_date='2026-11-02', end_date='2026-11-02', status='approved', duration_type='half_day')
    row = report.summarize({}, [], [leave], [], SETTINGS, '2026-11', NOW)
    assert row['half'] == 1
    assert row['approved'] == 0.5
    assert row['absent'] == 20


def test_overnight_shift_across_midnight_paired_cleanly():
    logs = [
        event(2, '10:00', 'check-in'),
        event(3, '02:00', 'check-out'),
        event(3, '10:00', 'check-in'),
        event(3, '18:00', 'check-out'),
    ]
    row = report.summarize({}, logs, [], [], SETTINGS, '2026-11', NOW)
    assert row['present'] == 2
    assert row['review'] == 0


def test_all_four_apps_desk_field_flyfield_and_wfh_count_in_present_and_absent():
    logs = [
        # Day 2: Normal Office Desk App (status="SUCCESS")
        event(2, '10:00', 'check-in', status='SUCCESS', attendance_type='office'),
        event(2, '18:00', 'check-out', status='SUCCESS', attendance_type='office'),
        # Day 3: Planned Field App / Offline Sync (type="IN"/"OUT", status="SUCCESS")
        event(3, '10:00', 'IN', status='SUCCESS', attendance_type='remote_field'),
        event(3, '18:00', 'OUT', status='SUCCESS', attendance_type='remote_field'),
        # Day 4: FlyField App (status="SUCCESS")
        event(4, '10:00', 'check-in', status='SUCCESS', attendance_type='remote_field', field_mode='flyfield'),
        event(4, '18:00', 'check-out', status='SUCCESS', attendance_type='remote_field', field_mode='flyfield'),
        # Day 5: WFH App historical logs (status="Present", source="wfh_desktop")
        event(5, '10:00', 'check-in', status='Present', source='wfh_desktop', attendance_type='wfh'),
        event(5, '18:00', 'check-out', status='Present', source='wfh_desktop', attendance_type='wfh'),
        # Day 6: WFH App new logs (status="SUCCESS", source="wfh_desktop")
        event(6, '10:00', 'check-in', status='SUCCESS', source='wfh_desktop', attendance_type='wfh'),
        event(6, '18:00', 'check-out', status='SUCCESS', source='wfh_desktop', attendance_type='wfh'),
        # Day 9 (Monday): Historical WFH check-in where check-out was auto-closed in wfh_sessions without an attendance_logs check-out
        event(9, '10:00', 'check-in', status='Present', source='wfh_desktop', attendance_type='wfh'),
    ]
    row = report.summarize({}, logs, [], [], SETTINGS, '2026-11', NOW)
    assert row['present'] == 6
    assert row['absent'] == 15  # 21 working days minus 6 present days across all 4 apps
    assert row['review'] == 0


def test_local_month_boundary_and_scoping(db, monkeypatch):
    owner = account(db, 'admin', role='manager', features=['attendance'])
    employee = account(db, email='team@example.test', org=owner['organization_id'])
    db.employees_collection.update_one({'_id': employee['_id']}, {'$set': {
        'manager_id': owner['email'], 'employee_type': 'desk', 'created_at': datetime(2020, 1, 1)}})
    account(db, email='other@example.test', org=owner['organization_id'])
    account(db, email='foreign@example.test')
    async def settings(_):
        return SETTINGS
    async def balance(*args, **kwargs):
        return {'total_leave': {'remaining': 7.5}}
    monkeypatch.setattr(report, 'get_effective_org_settings', settings)
    monkeypatch.setattr(report, 'calculate_leave_balance', balance)
    db.attendance_logs_collection.insert_many([
        dict(event(1, '00:10'), email=employee['email'], organization_id=owner['organization_id']),
        dict(event(1, '18:00', 'check-out'), email=employee['email'], organization_id=owner['organization_id']),
        dict(event(2, '10:00'), email=employee['email'], organization_id='another-org'),
    ])
    result = asyncio.run(report.build_report(SimpleNamespace(**owner), '2026-11', NOW))
    assert len(result['rows']) == 1
    row = result['rows'][0]
    assert row['email'] == employee['email']
    assert row['present'] == 1
    assert row['review'] == 0
    assert row['leave_pending'] == 7.5


def test_downloads_and_permissions(client, db):
    admin = account(db, 'admin', features=['attendance'])
    headers = bearer(admin, 'admin')
    assert client.get('/admin/reports/automation-data?month=bad', headers=headers).status_code == 400
    assert client.get('/admin/reports/automation-data?month=2026-11&format=exe', headers=headers).status_code == 422
    for format, magic in [('xlsx', b'PK'), ('pdf', b'%PDF')]:
        response = client.get(f'/admin/reports/automation-data?month=2026-11&format={format}', headers=headers)
        assert response.status_code == 200
        assert response.content.startswith(magic)
        assert f'2026-11.{format}' in response.headers['content-disposition']
        assert 'no-store' in response.headers['cache-control']
    restricted = account(db, 'admin', email='restricted@example.test', features=['reports'])
    assert client.get('/admin/reports/automation-data?month=2026-11', headers=bearer(restricted, 'admin')).status_code == 403
    assert client.get('/admin/reports/automation-data?month=2026-11').status_code == 401


def test_xlsx_values_are_literal_and_both_formats_render():
    row = {key: 0 for key, _ in report.COLUMNS}
    row.update(name='=HYPERLINK("https://example.test")', email='employee@example.test')
    data = dict(month='2026-11', generated_at=NOW.isoformat(), rows=[row] * 90, days_in_month=30, working_days_by_org={'org': 21})
    with zipfile.ZipFile(io.BytesIO(report.render_xlsx(data))) as workbook:
        for name in workbook.namelist():
            ElementTree.fromstring(workbook.read(name))
        sheet = workbook.read('xl/worksheets/sheet1.xml')
        assert b'<f>' not in sheet
        assert b'HYPERLINK' in sheet
    assert report.render_pdf(data).startswith(b'%PDF')
