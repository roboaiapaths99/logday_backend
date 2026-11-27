from services.work_calendar import is_working_day, month_days_off
"""Monthly attendance exports. No writes, penalties, or email side effects."""
import asyncio
import calendar
import io
import re
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

from fastapi import HTTPException
from attendance_service import classify_attendance_day
from database import employees_collection, attendance_logs_collection, leave_requests_collection, holidays_collection
from services.common_helpers import get_employee_filter, get_effective_org_settings, calculate_leave_balance, normalize_date_str

COLUMNS = [
    ('serial', 'Serial No.'), ('name', 'Employee Name'), ('email', 'Email ID'),
    ('present', 'Full Present Days'), ('absent', 'Absent Days'), ('late', 'Late Entry Days'),
    ('half', 'Half Days'), ('leave_pending', 'Leave Balance (Days)'),
    ('pending_requests', 'Pending Leave Days'),
]
NOTES = ('All employees in your permitted scope; page search/mode filters do not apply. '
         'Completed local dates only (today and future days excluded). Full present means a completed '
         'attendance day with full credit under current rules, excluding half days and short leave. '
         'Late entry is an overlapping flag using current organization rules. Weekends and configured '
         'holidays are excluded from absence. Leave balance uses the existing selected-month balance engine; '
         'pending leave days means requests awaiting approval. Monthly calendar totals cover the entire month; '
         'attendance totals cover completed dates only.')


def days_off(holidays):
    off = set()
    for holiday in holidays:
        off.update(int(d) for d in holiday.get('days_off', []))
        if holiday.get('date'):
            off.add(int(str(holiday['date'])[:10].split('-')[2]))
    return off


def working_days(month, holidays):
    year, number = map(int, month.split('-'))
    off = days_off(holidays)
    return sum(is_working_day(datetime(year, number, day), off)
               for day in range(1, calendar.monthrange(year, number)[1] + 1))


def calendar_heading(report):
    counts = sorted(set(report['working_days_by_org'].values()))
    count = str(counts[0]) if len(counts) == 1 else (
        f'{counts[0]} to {counts[-1]} (varies by organization)' if counts else 'N/A')
    return f"Total Working Days: {count}   |   Days in Month: {report['days_in_month']}"


def as_utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def summarize(employee, logs, leaves, holidays, settings, month, now):
    year, number = map(int, month.split('-'))
    offset = timedelta(minutes=int(settings.get('timezone_offset', 330)))
    today = (as_utc(now) + offset).date()
    days = defaultdict(list)
    valid_logs = []
    for log in logs:
        if str(log.get('status', 'success')).lower() != 'success' or not log.get('timestamp'):
            continue
        valid_logs.append(log)
        days[(as_utc(log['timestamp']) + offset).date()].append(log)
    valid_logs.sort(key=lambda l: as_utc(l['timestamp']))

    # Pair overnight shifts where a check-in on date D closes with an early check-out on date D+1 before any check-in on D+1
    overnight_checkout_by_date = {}
    consumed_overnight_outs = set()
    for d_key, d_events in list(days.items()):
        ordered = sorted(d_events, key=lambda l: as_utc(l['timestamp']))
        open_in = None
        for ev in ordered:
            if ev.get('type') == 'check-in' and open_in is None:
                open_in = ev
            elif ev.get('type') == 'check-out' and open_in is not None:
                open_in = None
        if open_in is not None:
            next_day = d_key + timedelta(days=1)
            next_events = sorted(days.get(next_day, []), key=lambda l: as_utc(l['timestamp']))
            if next_events and next_events[0].get('type') == 'check-out':
                out_ev = next_events[0]
                if (as_utc(out_ev['timestamp']) - as_utc(open_in['timestamp'])).total_seconds() <= 16 * 3600:
                    overnight_checkout_by_date[d_key] = out_ev
                    consumed_overnight_outs.add(id(out_ev))

    row = dict(name=employee.get('full_name') or '', email=employee.get('email') or '',
               **{key: 0 for key, _ in COLUMNS if key not in ('name', 'email')})
    row.update(approved=0, short=0, review=0, hours=0)
    off = days_off(holidays)
    joined = employee.get('date_of_joining') or employee.get('joining_date') or employee.get('created_at')
    joined_date = None
    if joined:
        try:
            joined_date = (as_utc(joined) + offset).date()
        except (ValueError, TypeError, AttributeError):
            pass
    for day in range(1, calendar.monthrange(year, number)[1] + 1):
        date = datetime(year, number, day).date()
        if date >= today or (joined_date and date < joined_date):
            continue
        date_str = date.isoformat()
        matching = [leave for leave in leaves if
                    (normalize_date_str(leave.get('start_date', '')) or '9999') <= date_str <=
                    (normalize_date_str(leave.get('end_date') or leave.get('start_date', '')) or '')]
        approved = next((l for l in matching if str(l.get('status', '')).lower() == 'approved'), None)
        pending = next((l for l in matching if str(l.get('status', '')).lower() == 'pending'), None)
        working = is_working_day(date, off)
        if working and approved:
            duration = str(approved.get('duration_type', '')).lower()
            row['approved'] += 0.5 if duration in ('half_day', 'first_half', 'second_half') or approved.get('duration') == 0.5 else (0 if duration == 'short_leave' else 1)
        if working and pending and not approved:
            row['pending_requests'] += 0.5 if str(pending.get('duration_type', '')).lower() in ('half_day', 'first_half', 'second_half') or pending.get('duration') == 0.5 else 1
        events = [ev for ev in sorted(days[date], key=lambda l: as_utc(l['timestamp'])) if id(ev) not in consumed_overnight_outs]
        if date in overnight_checkout_by_date:
            events.append(overnight_checkout_by_date[date])
        checkins = [l for l in events if l.get('type') == 'check-in']
        if not checkins:
            if events:
                row['review'] += 1
            elif working and approved:
                classification = classify_attendance_day(None, None, 0, settings,
                    employee.get('employee_type', 'desk'), approved)
                row['half'] += int(classification['is_half_day'])
                row['short'] += int(classification['is_short_leave'])
            elif working and not approved and not pending:
                row['absent'] += 1
            continue
        opened = None
        last_out = None
        seconds = 0
        for event in events:
            stamp = as_utc(event['timestamp'])
            if event.get('type') == 'check-in' and opened is None:
                opened = stamp
            elif event.get('type') == 'check-out' and opened is not None:
                seconds += max(0, (stamp - opened).total_seconds())
                opened = None
                last_out = stamp
        hours = seconds / 3600
        row['hours'] += hours
        result = classify_attendance_day(as_utc(checkins[0]['timestamp']) + offset,
                                        last_out + offset if last_out else None, hours,
                                        settings, employee.get('employee_type', 'desk'), approved)
        row['late'] += int(result['is_late'])
        if opened is not None or last_out is None:
            row['review'] += 1
            continue
        row['half'] += int(result['is_half_day'])
        row['short'] += int(result['is_short_leave'])
        row['present'] += int(result['effective_days'] == 1 and not result['is_half_day'] and not result['is_short_leave'])
    row['hours'] = round(row['hours'], 2)
    return row


async def build_report(admin, month, now=None):
    if not re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', month) or not 1900 <= int(month[:4]) <= 9998:
        raise HTTPException(400, 'Invalid month. Expected YYYY-MM.')
    now = now or datetime.now(timezone.utc)
    employees = await employees_collection.find(get_employee_filter(admin)).sort('full_name', 1).to_list(length=None)
    rows = []
    by_org = defaultdict(list)
    working_days_by_org = {}
    for employee in employees:
        by_org[employee.get('organization_id')].append(employee)
    if not by_org and admin.organization_id:
        by_org[admin.organization_id] = []
    for org, staff in by_org.items():
        if not org:
            continue
        settings = await get_effective_org_settings(org)
        offset = timedelta(minutes=int(settings.get('timezone_offset', 330)))
        start = datetime.strptime(month, '%Y-%m').replace(tzinfo=timezone.utc)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        emails = [e.get('email') for e in staff if e.get('email')]
        # Complete month query, never the paginated Attendance Logs list.
        logs = await attendance_logs_collection.find({'organization_id': org, 'email': {'$in': emails},
            'timestamp': {'$gte': start - offset, '$lt': end - offset}}).to_list(length=None)
        leaves = await leave_requests_collection.find({'organization_id': org,
            'status': {'$regex': '^(approved|pending)$', '$options': 'i'}}).to_list(length=None)
        holidays = await holidays_collection.find({'organization_id': {'$in': [org, 'system_org']},
                                                   'payroll_month': month}).to_list(length=None)
        holidays = [{'days_off': list(await month_days_off(org, month))}]
        working_days_by_org[str(org)] = working_days(month, holidays)
        logs_by_email = defaultdict(list)
        leaves_by_identity = defaultdict(list)
        for log in logs:
            logs_by_email[log.get('email')].append(log)
        for leave in leaves:
            for identity in {str(leave.get('email') or ''), str(leave.get('employee_id') or '')} - {''}:
                leaves_by_identity[identity].append(leave)

        sem = asyncio.Semaphore(10)
        async def _process_emp(employee):
            email = employee.get('email') or ''
            matched = {}
            for identity in (email, str(employee['_id']), str(employee.get('employee_id') or '')):
                for leave in leaves_by_identity[identity]:
                    matched[str(leave['_id'])] = leave
            row = summarize(employee, logs_by_email[email], list(matched.values()), holidays, settings, month, now)
            async with sem:
                balance = await calculate_leave_balance(email, org, int(month[:4]), month, absent_days=row['absent'])
            row['leave_pending'] = balance.get('total_leave', {}).get('remaining', 0)
            return row

        org_rows = await asyncio.gather(*(_process_emp(emp) for emp in staff))
        rows.extend(org_rows)
    rows.sort(key=lambda row: (row['name'].casefold(), row['email']))
    for index, row in enumerate(rows, 1):
        row['serial'] = index
    return {'month': month, 'generated_at': now.isoformat(), 'notes': NOTES, 'rows': rows, 'incomplete_days': sum(row['review'] for row in rows),
            'days_in_month': calendar.monthrange(*map(int, month.split('-')))[1],
            'working_days_by_org': working_days_by_org}


def render_xlsx(report):
    """Small standards-based XLSX writer; strings are literal, never Excel formulas."""
    matrix = [[f"Log Day Automation Data - {report['month']}"], [calendar_heading(report)],
              ['Generated at (UTC)', report['generated_at']], [NOTES + f" Attendance days requiring review: {report.get('incomplete_days', 0)}."], [], [title for _, title in COLUMNS]]
    matrix += [[row[key] for key, _ in COLUMNS] for row in report['rows']]
    def cell(value, style=0):
        if isinstance(value, (int, float)):
            return f'<c s="{style}"><v>{value}</v></c>'
        clean = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value))
        return f'<c s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(clean)}</t></is></c>'
    sheet = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="6" topLeftCell="A7" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="1" width="10" customWidth="1"/><col min="2" max="2" width="28" customWidth="1"/><col min="3" max="3" width="38" customWidth="1"/><col min="4" max="9" width="23" customWidth="1"/></cols><sheetData>'
    for index, row in enumerate(matrix, 1):
        height = ' ht="80" customHeight="1"' if index == 4 else (' ht="38" customHeight="1"' if index in (1, 2, 6) else '')
        style = 3 if index in (1, 2) else (1 if index == 6 else 2)
        sheet += f'<row r="{index}"{height}>' + ''.join(
            cell(v, {3: 4, 4: 5, 6: 6}.get(column, style) if index >= 6 else style)
            for column, v in enumerate(row)) + '</row>'
    sheet += f'</sheetData><autoFilter ref="A6:I{max(6, len(matrix))}"/><mergeCells count="3"><mergeCell ref="A1:I1"/><mergeCell ref="A2:I2"/><mergeCell ref="A4:I4"/></mergeCells></worksheet>'
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Monthly Summary" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        archive.writestr('xl/styles.xml', '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="6"><font><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FF004B87"/><sz val="22"/><name val="Calibri"/></font><font><b/><color rgb="FF15803D"/><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFDC2626"/><sz val="11"/><name val="Calibri"/></font><font><b/><color rgb="FFB58B00"/><sz val="11"/><name val="Calibri"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF004B87"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs><cellXfs count="7"><xf fontId="0" fillId="0" borderId="0"/><xf fontId="1" fillId="2" borderId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf fontId="0" fillId="0" borderId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf fontId="2" fillId="0" borderId="0" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf><xf fontId="3" fillId="0" borderId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf fontId="4" fillId="0" borderId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf><xf fontId="5" fillId="0" borderId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf></cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')
        archive.writestr('xl/worksheets/sheet1.xml', sheet)
    return output.getvalue()


def render_pdf(report):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A3, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, LongTable, TableStyle
    output = io.BytesIO()
    styles = getSampleStyleSheet()
    small = ParagraphStyle('cells', fontSize=8, leading=11, wordWrap='CJK')
    header = ParagraphStyle('headers', parent=small, textColor=colors.white)
    highlighted = {key: ParagraphStyle(key, parent=small, fontName='Helvetica-Bold',
        textColor=colors.HexColor(color)) for key, color in
        [('present', '#15803D'), ('absent', '#DC2626'), ('half', '#B58B00')]}
    data = [[Paragraph(escape(title), highlighted.get(key, header)) for key, title in COLUMNS]]
    data += [[Paragraph(escape(str(row[key])), highlighted.get(key, small))
              for key, _ in COLUMNS] for row in report['rows']]
    table = LongTable(data, colWidths=[40, 155, 225] + [115] * 6, repeatRows=1, hAlign='LEFT')
    table.setStyle(TableStyle([('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#004B87')),
        ('BACKGROUND', (3, 0), (3, 0), colors.HexColor('#F0FDF4')),
        ('BACKGROUND', (4, 0), (4, 0), colors.HexColor('#FEF2F2')),
        ('BACKGROUND', (6, 0), (6, 0), colors.HexColor('#FEFCE8')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#F1F5F9')]),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'), ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 8)]))
    def footer(canvas, doc):
        canvas.setFont('Helvetica', 8)
        canvas.drawRightString(1150, 20, f"Page {doc.page}")
    SimpleDocTemplate(output, pagesize=landscape(A3), leftMargin=30, rightMargin=30,
                      topMargin=30, bottomMargin=35).build([
        Paragraph(f"Log Day Automation Data - {report['month']}", styles['Title']),
        Paragraph(escape(calendar_heading(report)), ParagraphStyle('calendar', parent=styles['Heading1'], fontSize=22, leading=28)),
        Spacer(1, 12),
        Paragraph(escape(NOTES + f" Attendance days requiring review: {report.get('incomplete_days', 0)}."), styles['Normal']), Spacer(1, 15), table],
        onFirstPage=footer, onLaterPages=footer)
    return output.getvalue()
