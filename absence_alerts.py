from permissions import require_feature, require_any_feature, require_admin_session
"""
4:00 PM Daily Absentee Admin Alert & 1.5-Day Warning Email System.

Workflow:
1. At 4:00 PM (or organization's configured `absence_alert_time` in their local timezone),
   checks all active employees in the organization on working days.
2. Any employee who has NOT checked in / logged in today and has NO approved leave for today
   triggers an informational alert email to the organization's configured `absence_notification_emails`.
3. The admin alert email includes full employee details and two action buttons:
   - "Send Mail": Sends an official warning email to the employee informing them that since they took
     leave today without sending an email or applying on LogDay, 1.5 days leave is deducted (if leaves are pending)
     or 1.5 days salary is deducted today (if no leaves are pending), and records the penalty deduction.
   - "Reject": Marks the alert as rejected and takes no action.
"""

import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

from bson import ObjectId
from fastapi import APIRouter, Body, Depends, Form, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse

from auth import get_current_admin
from database import (
    absence_alerts_collection,
    attendance_logs_collection,
    employees_collection,
    holidays_collection,
    leave_policies_collection,
    leave_requests_collection,
    organizations_collection,
    settings_collection,
    visit_logs_collection,
    wfh_alerts_collection,
    wfh_sessions_collection,
)
from hrms_payroll import _get_org_smtp_config, _send_email_sync
from models import Admin

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Absence Alerts"])


def _resolve_public_api_base_url() -> str:
    """Resolve the public backend base URL for clickable email action links."""
    env_url = (
        os.getenv("PUBLIC_API_URL")
        or os.getenv("API_BASE_URL")
        or os.getenv("BACKEND_URL")
        or ""
    ).strip().rstrip("/")
    if env_url and "localhost" not in env_url and "127.0.0.1" not in env_url:
        return env_url
    if os.getenv("APP_ENV", "development") == "production":
        return env_url or "https://logday-api.duckdns.org"
    return env_url or "http://localhost:8001"


async def _is_org_holiday_or_day_off(org_id: str, local_dt: datetime) -> Tuple[bool, str]:
    """Check if `local_dt` is a Sunday, configured day off, or named holiday for `org_id`."""
    from services.work_calendar import month_days_off
    off = await month_days_off(org_id, local_dt.strftime("%Y-%m"))
    return (True, "Scheduled day off") if local_dt.day in off else (False, "")


async def _has_employee_logged_in_today(
    emp: dict,
    org_id: str,
    day_start_utc: datetime,
    day_end_utc: datetime,
    today_str: str,
) -> bool:
    """Return True if the employee has any attendance check-in, WFH session, or field visit today."""
    clean_email = (emp.get("email") or "").strip().lower()
    emp_id_str = str(emp.get("_id", ""))
    emp_code = str(emp.get("employee_id", "")).strip()
    identifiers = [x for x in {clean_email, emp.get("email", "").strip(), emp_id_str, emp_code} if x]

    # 1. Standard / Desk / Field attendance check-in log
    att_log = await attendance_logs_collection.find_one({
        "$or": [
            {"user_id": {"$in": identifiers}},
            {"email": {"$in": identifiers}},
            {"employee_email": {"$in": identifiers}},
        ],
        "timestamp": {"$gte": day_start_utc, "$lt": day_end_utc},
    })
    if att_log:
        return True

    # 2. WFH session check-in today
    wfh_session = await wfh_sessions_collection.find_one({
        "$or": [
            {"employee_email": {"$in": identifiers}},
            {"employee_id": {"$in": identifiers}},
        ],
        "$and": [
            {
                "$or": [
                    {"date": today_str},
                    {"check_in_time": {"$gte": day_start_utc, "$lt": day_end_utc}},
                    {"start_time": {"$gte": day_start_utc, "$lt": day_end_utc}},
                ]
            }
        ],
    })
    if wfh_session:
        return True

    # 3. Field visit log today
    visit_log = await visit_logs_collection.find_one({
        "employee_id": {"$in": identifiers},
        "timestamp": {"$gte": day_start_utc, "$lt": day_end_utc},
    })
    if visit_log:
        return True

    return False


async def _has_approved_leave_today(emp: dict, today_str: str) -> bool:
    """Return True if the employee has an approved leave covering `today_str`."""
    clean_email = (emp.get("email") or "").strip().lower()
    emp_id_str = str(emp.get("_id", ""))
    emp_code = str(emp.get("employee_id", "")).strip()
    identifiers = [x for x in {clean_email, emp.get("email", "").strip(), emp_id_str, emp_code} if x]

    leave_doc = await leave_requests_collection.find_one({
        "$or": [
            {"employee_id": {"$in": identifiers}},
            {"email": {"$in": identifiers}},
        ],
        "start_date": {"$lte": today_str},
        "end_date": {"$gte": today_str},
        "status": {"$regex": "^approved$", "$options": "i"},
    })
    return leave_doc is not None


async def _get_remaining_paid_leaves(email: str, org_id: str, today_str: str) -> float:
    """Calculate the employee's remaining paid leave balance (Total Leave remaining)."""
    try:
        from services.common_helpers import calculate_leave_balance
        year = int(today_str[:4])
        month = today_str[:7]
        bal = await calculate_leave_balance(email, org_id, year=year, month=month)
        rem_tl = float((bal.get("total_leave") or {}).get("remaining", 0.0))
        return max(0.0, round(rem_tl, 2))
    except Exception as e:
        logger.warning(f"Fallback leave balance calculation for {email}: {e}")
        return 0.0


def _build_admin_absence_alert_email_html(
    comp_name: str,
    emp: dict,
    today_str: str,
    cutoff_time_str: str,
    remaining_leaves: float,
    send_mail_url: str,
    reject_url: str,
) -> str:
    """Build the HTML email sent to the organization's admin notification email(s) at 4:00 PM."""
    emp_name = emp.get("full_name") or emp.get("name") or emp.get("email", "Employee")
    emp_email = (emp.get("email") or "").strip().lower()
    emp_code = emp.get("employee_id") or "N/A"
    dept = emp.get("department") or "General"
    desig = emp.get("designation") or "Employee"
    emp_type = str(emp.get("employee_type") or "desk").upper()

    if remaining_leaves >= 1.5:
        penalty_preview = f"<b>1.5 Days Paid Leave Cut</b> (Employee currently has <b>{remaining_leaves}</b> paid leave(s) remaining)"
    elif remaining_leaves > 0:
        lop_part = round(1.5 - remaining_leaves, 2)
        penalty_preview = (
            f"<b>{remaining_leaves} Day Paid Leave Cut + {lop_part} Day Salary Cut (LOP)</b> "
            f"(Only {remaining_leaves} paid leave remaining)"
        )
    else:
        penalty_preview = "<b>1.5 Days Salary Cut Today (Loss of Pay)</b> (Employee has <b>0</b> paid leaves pending)"

    return f"""
    <div style="font-family: 'Segoe UI', Arial, sans-serif; max-width: 620px; margin: 0 auto; border: 1px solid #e2e8f0; border-radius: 16px; overflow: hidden; background: #ffffff;">
      <div style="background: #004B87; color: #ffffff; padding: 22px 24px;">
        <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 1px; opacity: 0.85; font-weight: 700;">
          {comp_name} • Daily 4:00 PM Attendance Alert
        </div>
        <h2 style="margin: 6px 0 0; font-size: 20px; font-weight: 800;">
          Employee Not Present Today ({today_str})
        </h2>
      </div>

      <div style="padding: 24px; color: #1e293b;">
        <p style="font-size: 14px; margin-top: 0; line-height: 1.5; color: #334155;">
          The following employee has <b>not logged in or checked in today till {cutoff_time_str}</b> and has <b>not applied for any leave</b> on LogDay:
        </p>

        <table style="width: 100%; border-collapse: collapse; margin: 16px 0; font-size: 13px; border: 1px solid #e2e8f0; border-radius: 10px; overflow: hidden;">
          <tr style="background: #f8fafc; border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 10px 14px; color: #64748b; width: 38%;">Employee Name</td>
            <td style="padding: 10px 14px; font-weight: 700; color: #0f172a;">{emp_name} ({emp_code})</td>
          </tr>
          <tr style="border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 10px 14px; color: #64748b;">LogDay Login Email</td>
            <td style="padding: 10px 14px; font-weight: 700; color: #004B87;">{emp_email}</td>
          </tr>
          <tr style="background: #f8fafc; border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 10px 14px; color: #64748b;">Department & Designation</td>
            <td style="padding: 10px 14px; font-weight: 600; color: #1e293b;">{dept} — {desig}</td>
          </tr>
          <tr style="border-bottom: 1px solid #e2e8f0;">
            <td style="padding: 10px 14px; color: #64748b;">Work Mode</td>
            <td style="padding: 10px 14px; font-weight: 600; color: #1e293b;">{emp_type}</td>
          </tr>
          <tr style="background: #fff1f2; border-bottom: 1px solid #fecdd3;">
            <td style="padding: 10px 14px; color: #be123c; font-weight: 700;">Current Status (as of {cutoff_time_str})</td>
            <td style="padding: 10px 14px; font-weight: 800; color: #e11d48;">NOT PRESENT (No Check-In / No Leave Applied)</td>
          </tr>
          <tr style="background: #fffbeb;">
            <td style="padding: 10px 14px; color: #92400e; font-weight: 700;">Pending Paid Leave Balance</td>
            <td style="padding: 10px 14px; color: #92400e;">{remaining_leaves} Day(s) • Action Impact: {penalty_preview}</td>
          </tr>
        </table>

        <div style="background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 12px; padding: 16px; margin: 18px 0;">
          <p style="margin: 0 0 14px; font-size: 13px; color: #475569; line-height: 1.5;">
            Choose an action below:
            <br/>• <b>Send Mail</b>: Sends an official warning email to <b>{emp_email}</b> and applies the <b>1.5-day leave / salary deduction</b>.
            <br/>• <b>Reject</b>: Dismisses this alert — nothing happens and no email is sent to the employee.
          </p>

          <div style="text-align: center; padding-top: 6px;">
            <a href="{send_mail_url}"
               style="display: inline-block; background: #dc2626; color: #ffffff; text-decoration: none; font-weight: 700; font-size: 14px; padding: 12px 26px; border-radius: 10px; margin-right: 12px;">
              Send Mail
            </a>
            <a href="{reject_url}"
               style="display: inline-block; background: #475569; color: #ffffff; text-decoration: none; font-weight: 700; font-size: 14px; padding: 12px 26px; border-radius: 10px;">
              Reject
            </a>
          </div>
        </div>

        <p style="font-size: 11px; color: #94a3b8; margin-top: 16px; text-align: center;">
          This automated alert was sent to the organization's configured Absentee Notification Email IDs in LogDay Admin Panel.
        </p>
      </div>
    </div>
    """


def _build_employee_warning_email_html(
    comp_name: str,
    emp_name: str,
    emp_email: str,
    date_str: str,
    remaining_leaves_before: float,
    paid_leave_cut: float,
    salary_lop_cut: float,
) -> str:
    """Build the official warning email sent to the employee when Admin presses 'Send Mail'."""
    if paid_leave_cut >= 1.5 and salary_lop_cut == 0:
        deduction_headline = "1.5 Days Leave Has Been Deducted"
        deduction_explanation = (
            f"Because you had pending leaves in your account ({remaining_leaves_before} day(s) available), "
            f"<b>1.5 days of leave has been cut</b> from your leave balance for today ({date_str})."
        )
    elif paid_leave_cut == 0:
        deduction_headline = "1.5 Days Salary Has Been Deducted Today"
        deduction_explanation = (
            f"Because you have <b>no leaves pending</b> in your account, "
            f"<b>1.5 days of salary has been cut today ({date_str})</b> as Loss of Pay (LOP)."
        )
    else:
        deduction_headline = f"{paid_leave_cut} Day Leave + {salary_lop_cut} Day Salary Has Been Deducted"
        deduction_explanation = (
            f"Because you only had <b>{paid_leave_cut} day(s)</b> of pending leave available, "
            f"<b>{paid_leave_cut} day leave has been cut</b> and the remaining <b>{salary_lop_cut} day salary has been cut today ({date_str})</b>."
        )

    return f"""
    <div style="font-family: 'Segoe UI', Arial, sans-serif; max-width: 600px; margin: 0 auto; border: 1px solid #fecdd3; border-radius: 16px; overflow: hidden; background: #ffffff;">
      <div style="background: #b91c1c; color: #ffffff; padding: 22px 24px;">
        <div style="font-size: 11px; text-transform: uppercase; letter-spacing: 1px; opacity: 0.9; font-weight: 700;">
          {comp_name} • Official Attendance Warning
        </div>
        <h2 style="margin: 6px 0 0; font-size: 20px; font-weight: 800;">
          Uninformed Absence Warning — {date_str}
        </h2>
      </div>

      <div style="padding: 24px; color: #1e293b;">
        <p style="font-size: 14px; margin-top: 0;">Dear <b>{emp_name}</b>,</p>

        <p style="font-size: 14px; color: #334155; line-height: 1.6;">
          Today (<b>{date_str}</b>) you have taken leave without sending any prior email notification or adding a leave request on the <b>LogDay</b> app.
        </p>

        <div style="background: #fff1f2; border-left: 4px solid #e11d48; padding: 16px; border-radius: 8px; margin: 18px 0;">
          <div style="font-size: 15px; font-weight: 800; color: #9f1239; margin-bottom: 6px;">
            Penalty Applied: {deduction_headline}
          </div>
          <p style="margin: 0; font-size: 13.5px; color: #881337; line-height: 1.55;">
            {deduction_explanation}
          </p>
        </div>

        <p style="font-size: 13.5px; color: #475569; line-height: 1.6;">
          <b>Please treat this as an official warning.</b> In the future, ensure that any leave is applied in advance on the LogDay app or formally communicated via email before your shift starts.
        </p>

        <p style="font-size: 12px; color: #94a3b8; margin-top: 22px; border-top: 1px solid #e2e8f0; padding-top: 12px;">
          Account: {emp_email} • Sent by {comp_name} HR & Administration via LogDay
        </p>
      </div>
    </div>
    """


def _render_action_html_page(title: str, badge_text: str, badge_color: str, message_html: str) -> HTMLResponse:
    """Render a clean confirmation webpage when the admin clicks 'Send Mail' or 'Reject' in their email."""
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{title} — LogDay Admin</title>
</head>
<body style="margin: 0; padding: 40px 16px; background: #f1f5f9; font-family: 'Segoe UI', Arial, sans-serif; color: #0f172a;">
  <div style="max-width: 520px; margin: 40px auto; background: #ffffff; border: 1px solid #e2e8f0; border-radius: 20px; padding: 32px; box-shadow: 0 10px 25px rgba(15,23,42,0.06); text-align: center;">
    <div style="display: inline-block; padding: 6px 14px; border-radius: 999px; background: {badge_color}; color: #ffffff; font-size: 12px; font-weight: 800; letter-spacing: 0.5px; text-transform: uppercase; margin-bottom: 14px;">
      {badge_text}
    </div>
    <h1 style="font-size: 22px; margin: 0 0 12px; color: #0f172a;">{title}</h1>
    <div style="font-size: 14px; color: #475569; line-height: 1.6; text-align: left; background: #f8fafc; padding: 16px 18px; border-radius: 12px; border: 1px solid #e2e8f0;">
      {message_html}
    </div>
    <p style="font-size: 12px; color: #94a3b8; margin: 20px 0 0;">
      You can safely close this browser tab.
    </p>
  </div>
</body>
</html>"""
    return HTMLResponse(content=html, status_code=200)


async def process_org_absence_alerts(org_id: str, force_run: bool = False) -> dict:
    """
    Evaluate all active employees in `org_id` for today's attendance.
    If local time >= configured `absence_alert_time` (default 16:00 / 4:00 PM) or `force_run=True`,
    sends the absentee alert email with [Send Mail] and [Reject] buttons to the organization's configured admin emails.
    """
    if not org_id or org_id == "system_org":
        settings_doc = await settings_collection.find_one({"id": "config"})
    else:
        settings_doc = await settings_collection.find_one({"organization_id": org_id})

    settings_doc = settings_doc or {}
    enabled = settings_doc.get("absence_alert_enabled", True)
    if not enabled and not force_run:
        return {"organization_id": org_id, "status": "disabled", "alerts_sent": 0}

    raw_emails = settings_doc.get("absence_notification_emails") or []
    if isinstance(raw_emails, str):
        raw_emails = [e.strip().lower() for e in raw_emails.split(",") if e.strip()]
    else:
        raw_emails = [str(e).strip().lower() for e in raw_emails if str(e).strip()]

    if not raw_emails:
        return {
            "organization_id": org_id,
            "status": "no_recipient_emails",
            "message": "No Absentee Notification Email IDs configured for this organization in Settings.",
            "alerts_sent": 0,
        }

    tz_offset = int(settings_doc.get("timezone_offset", 330))
    cutoff_str = str(settings_doc.get("absence_alert_time") or "16:00").strip()
    try:
        c_hour, c_min = map(int, cutoff_str.split(":")[:2])
    except Exception:
        c_hour, c_min = 16, 0
        cutoff_str = "16:00"

    now_utc = datetime.now(timezone.utc)
    local_now = now_utc + timedelta(minutes=tz_offset)
    today_str = local_now.strftime("%Y-%m-%d")

    # Check if current local time has reached 4:00 PM (unless manually triggered by admin via force_run)
    if not force_run:
        if (local_now.hour, local_now.minute) < (c_hour, c_min):
            return {"organization_id": org_id, "status": "before_cutoff", "local_time": local_now.strftime("%H:%M"), "alerts_sent": 0}

    # Skip Sundays & Organization Holidays (unless force_run is explicitly testing, still check holiday unless forced)
    is_off, off_reason = await _is_org_holiday_or_day_off(org_id, local_now)
    if is_off and not force_run:
        return {"organization_id": org_id, "status": f"holiday_or_off ({off_reason})", "alerts_sent": 0}

    # Check email provider configuration for this organization
    smtp_cfg = await _get_org_smtp_config(org_id)
    if not smtp_cfg.get("configured"):
        return {
            "organization_id": org_id,
            "status": "email_not_configured",
            "message": "Brevo / Email service is not configured. Configure Brevo in Payroll -> Email Setup or .env.",
            "alerts_sent": 0,
        }

    # Resolve organization name
    comp_name = "LogDay Organization"
    if org_id != "system_org" and ObjectId.is_valid(org_id):
        org_doc = await organizations_collection.find_one({"_id": ObjectId(org_id)})
        if org_doc and org_doc.get("name"):
            comp_name = org_doc["name"]

    # Local day boundaries converted to UTC
    local_day_start = datetime(local_now.year, local_now.month, local_now.day, 0, 0, 0, tzinfo=timezone.utc)
    day_start_utc = local_day_start - timedelta(minutes=tz_offset)
    day_end_utc = day_start_utc + timedelta(days=1)

    emp_query: dict = {
        "$and": [
            {
                "$or": [
                    {"status": {"$in": ["Active", "active", None]}},
                    {"status": {"$exists": False}},
                ]
            },
            {"is_active": {"$ne": False}},
        ]
    }
    if org_id and org_id != "system_org":
        emp_query["organization_id"] = org_id

    employees = await employees_collection.find(emp_query, {"face_embedding": 0, "hashed_password": 0}).to_list(length=1000)
    public_base = _resolve_public_api_base_url()

    alerts_sent = 0
    absent_employees = []
    errors = []

    for emp in employees:
        emp_email = (emp.get("email") or "").strip().lower()
        if not emp_email:
            continue

        emp_org_id = emp.get("organization_id") or org_id or "system_org"

        # 1. Check if already alerted today (unless force_run re-sends for pending ones)
        existing_alert = await absence_alerts_collection.find_one({
            "organization_id": emp_org_id,
            "employee_email": emp_email,
            "date": today_str,
        })
        if existing_alert and not force_run:
            continue
        if existing_alert and existing_alert.get("status") in ("mail_sent", "rejected"):
            continue

        # 2. Check if employee logged in / checked in today
        logged_in = await _has_employee_logged_in_today(emp, emp_org_id, day_start_utc, day_end_utc, today_str)
        if logged_in:
            continue

        # 3. Check if employee has an approved leave today
        on_leave = await _has_approved_leave_today(emp, today_str)
        if on_leave:
            continue

        # Employee is absent without leave till 4:00 PM
        rem_leaves = await _get_remaining_paid_leaves(emp_email, emp_org_id, today_str)
        token = existing_alert["token"] if (existing_alert and existing_alert.get("token")) else secrets.token_urlsafe(32)

        send_mail_url = f"{public_base}/api/absence-alert/action?token={token}&action=send_mail"
        reject_url = f"{public_base}/api/absence-alert/action?token={token}&action=reject"

        html_body = _build_admin_absence_alert_email_html(
            comp_name=comp_name,
            emp=emp,
            today_str=today_str,
            cutoff_time_str=cutoff_str,
            remaining_leaves=rem_leaves,
            send_mail_url=send_mail_url,
            reject_url=reject_url,
        )

        emp_name = emp.get("full_name") or emp.get("name") or emp_email
        subject = f"[4 PM Alert] {emp_name} is Not Present Today ({today_str}) — Action Required"

        delivered_to = []
        for admin_recipient in raw_emails:
            ok, note = await run_in_threadpool(
                _send_email_sync,
                smtp_cfg,
                admin_recipient,
                subject,
                html_body,
                None,
                None,
            )
            if ok:
                delivered_to.append(admin_recipient)
            else:
                errors.append(f"{admin_recipient}: {note}")

        if delivered_to:
            alert_doc = {
                "organization_id": emp_org_id,
                "company_name": comp_name,
                "employee_email": emp_email,
                "employee_name": emp_name,
                "employee_id": emp.get("employee_id", ""),
                "department": emp.get("department", "General"),
                "designation": emp.get("designation", "Employee"),
                "date": today_str,
                "token": token,
                "token_expires_at": now_utc + timedelta(hours=72),
                "status": "pending",
                "remaining_leaves_at_alert": rem_leaves,
                "notified_admin_emails": delivered_to,
                "created_at": now_utc,
                "updated_at": now_utc,
            }
            await absence_alerts_collection.update_one(
                {"organization_id": emp_org_id, "employee_email": emp_email, "date": today_str},
                {"$set": alert_doc},
                upsert=True,
            )
            alerts_sent += 1
            absent_employees.append({"email": emp_email, "name": emp_name, "notified": delivered_to})

    return {
        "organization_id": org_id,
        "date": today_str,
        "status": "completed",
        "alerts_sent": alerts_sent,
        "recipient_emails": raw_emails,
        "absent_employees": absent_employees,
        "errors": errors,
    }


async def check_uninformed_absences_4pm():
    """Scheduled background job: runs every 5 minutes and processes 4:00 PM absentee alerts per organization."""
    try:
        from services.scheduler_jobs import acquire_scheduler_lock
        if not await acquire_scheduler_lock("check_uninformed_absences_4pm", ttl_seconds=240):
            return
        settings_docs = await settings_collection.find({
            "absence_notification_emails": {"$exists": True, "$ne": []}
        }).to_list(length=500)

        processed_orgs = set()
        for s_doc in settings_docs:
            org_id = s_doc.get("organization_id") or ("system_org" if s_doc.get("id") == "config" else None)
            if not org_id or org_id in processed_orgs:
                continue
            processed_orgs.add(org_id)
            try:
                res = await process_org_absence_alerts(org_id, force_run=False)
                if res.get("alerts_sent", 0) > 0:
                    logger.info(f"4 PM Absence Alert: sent {res['alerts_sent']} admin alert(s) for org {org_id}")
            except Exception as org_err:
                logger.error(f"Error processing 4 PM absence alerts for org {org_id}: {org_err}")
    except Exception as e:
        logger.error(f"check_uninformed_absences_4pm failed: {e}")


def _is_alert_token_expired(alert_doc: dict, now_utc: Optional[datetime] = None) -> bool:
    now_utc = now_utc or datetime.now(timezone.utc)
    exp = alert_doc.get("token_expires_at") or alert_doc.get("expires_at")
    if isinstance(exp, datetime):
        exp_utc = exp.replace(tzinfo=timezone.utc) if exp.tzinfo is None else exp.astimezone(timezone.utc)
        return now_utc >= exp_utc
    created = alert_doc.get("created_at")
    if isinstance(created, datetime):
        created_utc = created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created.astimezone(timezone.utc)
        return (now_utc - created_utc) > timedelta(hours=72)
    return False


@router.get("/api/absence-alert/action", response_class=HTMLResponse)
async def preview_absence_alert_email_action(
    token: str = Query(..., description="Unique alert security token"),
    action: str = Query(..., description="'send_mail' or 'reject'"),
):
    """
    Safe GET confirmation page for 'Send Mail' or 'Reject' clicked inside the 4:00 PM admin email.
    Using a read-only GET page + POST form submission prevents corporate email security scanners
    (Outlook SafeLinks, Mimecast, Gmail link pre-fetchers) from accidentally triggering the penalty.
    """
    clean_action = (action or "").strip().lower()
    if clean_action not in ("send_mail", "reject"):
        return _render_action_html_page(
            title="Invalid Action",
            badge_text="Error",
            badge_color="#dc2626",
            message_html="<p>Invalid action parameter. Please use the <b>Send Mail</b> or <b>Reject</b> button from your email.</p>",
        )

    alert_doc = await absence_alerts_collection.find_one({"token": token})
    if not alert_doc or _is_alert_token_expired(alert_doc):
        return _render_action_html_page(
            title="Alert Link Expired or Not Found",
            badge_text="Expired" if alert_doc else "Not Found",
            badge_color="#64748b",
            message_html="<p>This attendance alert link could not be found or has expired (links are valid for 72 hours).</p>",
        )

    emp_email = alert_doc.get("employee_email", "")
    emp_name = alert_doc.get("employee_name", emp_email)
    date_str = alert_doc.get("date", "")
    org_id = alert_doc.get("organization_id") or "system_org"
    current_status = alert_doc.get("status", "pending")

    if current_status == "mail_sent":
        return _render_action_html_page(
            title="Warning Email Already Sent",
            badge_text="Already Completed",
            badge_color="#059669",
            message_html=(
                f"<p>The 1.5-day warning email and deduction for <b>{emp_name}</b> ({emp_email}) on <b>{date_str}</b> "
                f"have already been processed.</p>"
            ),
        )
    if current_status == "rejected":
        return _render_action_html_page(
            title="Alert Already Rejected",
            badge_text="Rejected",
            badge_color="#475569",
            message_html=(
                f"<p>This alert for <b>{emp_name}</b> ({emp_email}) on <b>{date_str}</b> was already rejected. "
                f"No email was sent and no penalty was applied.</p>"
            ),
        )

    rem_leaves = await _get_remaining_paid_leaves(emp_email, org_id, date_str)
    if clean_action == "send_mail":
        impact_text = (
            f"<b>1.5 Days Paid Leave Cut</b> (Employee has {rem_leaves} paid leave(s) available)"
            if rem_leaves >= 1.5
            else (
                "<b>1.5 Days Salary Cut (LOP)</b> (Employee has 0 paid leaves pending)"
                if rem_leaves <= 0
                else f"<b>{rem_leaves} Day Paid Leave + {round(1.5 - rem_leaves, 2)} Day Salary Cut (LOP)</b>"
            )
        )
        form_html = f"""
        <p style="margin-top:0;">Confirm sending the official warning email to <b>{emp_name}</b> (<b>{emp_email}</b>) for <b>{date_str}</b>:</p>
        <p>Penalty to be applied: {impact_text}.</p>
        <form method="POST" action="/api/absence-alert/action" style="margin-top: 18px; text-align: center;">
          <input type="hidden" name="token" value="{token}" />
          <input type="hidden" name="action" value="send_mail" />
          <button type="submit" style="cursor: pointer; border: none; background: #dc2626; color: #ffffff; font-weight: 800; font-size: 14px; padding: 12px 28px; border-radius: 10px;">
            Confirm &amp; Send Warning Mail to Employee
          </button>
        </form>
        """
        return _render_action_html_page(
            title="Confirm 1.5-Day Warning Email",
            badge_text="Action Confirmation",
            badge_color="#dc2626",
            message_html=form_html,
        )
    else:
        form_html = f"""
        <p style="margin-top:0;">Confirm rejecting the 4:00 PM absence alert for <b>{emp_name}</b> (<b>{emp_email}</b>) on <b>{date_str}</b>:</p>
        <p>No warning email will be sent to the employee and no penalty will be deducted.</p>
        <form method="POST" action="/api/absence-alert/action" style="margin-top: 18px; text-align: center;">
          <input type="hidden" name="token" value="{token}" />
          <input type="hidden" name="action" value="reject" />
          <button type="submit" style="cursor: pointer; border: none; background: #475569; color: #ffffff; font-weight: 800; font-size: 14px; padding: 12px 28px; border-radius: 10px;">
            Confirm Reject (Take No Action)
          </button>
        </form>
        """
        return _render_action_html_page(
            title="Confirm Reject Alert",
            badge_text="Reject Confirmation",
            badge_color="#475569",
            message_html=form_html,
        )


@router.post("/api/absence-alert/action", response_class=HTMLResponse)
async def execute_absence_alert_email_action(
    token: Optional[str] = Form(None),
    action: Optional[str] = Form(None),
    token_q: Optional[str] = Query(None, alias="token"),
    action_q: Optional[str] = Query(None, alias="action"),
):
    """
    Execute the confirmed 'Send Mail' or 'Reject' action.
    - If action == 'reject': Marks the alert as rejected. Nothing happens to the employee.
    - If action == 'send_mail': Applies the 1.5-day leave/salary penalty and sends the official warning email to the employee.
    """
    token = token or token_q or ""
    action = action or action_q or ""
    clean_action = (action or "").strip().lower()
    if clean_action not in ("send_mail", "reject"):
        return _render_action_html_page(
            title="Invalid Action",
            badge_text="Error",
            badge_color="#dc2626",
            message_html="<p>Invalid action parameter. Please use the <b>Send Mail</b> or <b>Reject</b> button from your email.</p>",
        )

    alert_doc = await absence_alerts_collection.find_one({"token": token})
    now_utc = datetime.now(timezone.utc)
    if not alert_doc or _is_alert_token_expired(alert_doc, now_utc):
        return _render_action_html_page(
            title="Alert Link Expired or Not Found",
            badge_text="Expired" if alert_doc else "Not Found",
            badge_color="#64748b",
            message_html="<p>This attendance alert link could not be found or has expired (links are valid for 72 hours).</p>",
        )

    emp_email = alert_doc.get("employee_email", "")
    emp_name = alert_doc.get("employee_name", emp_email)
    date_str = alert_doc.get("date", "")
    org_id = alert_doc.get("organization_id") or "system_org"
    comp_name = alert_doc.get("company_name") or "LogDay Organization"
    current_status = alert_doc.get("status", "pending")

    # Prevent duplicate processing if already finalized
    if current_status == "mail_sent":
        return _render_action_html_page(
            title="Warning Email Already Sent",
            badge_text="Already Completed",
            badge_color="#059669",
            message_html=(
                f"<p>The 1.5-day warning email and deduction for <b>{emp_name}</b> ({emp_email}) on <b>{date_str}</b> "
                f"have already been processed.</p>"
            ),
        )
    if current_status == "rejected":
        return _render_action_html_page(
            title="Alert Already Rejected",
            badge_text="Rejected",
            badge_color="#475569",
            message_html=(
                f"<p>This alert for <b>{emp_name}</b> ({emp_email}) on <b>{date_str}</b> was already rejected. "
                f"No email was sent and no penalty was applied.</p>"
            ),
        )
    if current_status == "processing":
        return _render_action_html_page(
            title="Action Already In Progress",
            badge_text="Processing",
            badge_color="#d97706",
            message_html=(
                f"<p>This alert for <b>{emp_name}</b> ({emp_email}) on <b>{date_str}</b> is currently being processed.</p>"
            ),
        )

    # --- CASE 1: ADMIN PRESSED "REJECT" -> ATOMIC CLAIM & NOTHING HAPPENS ---
    if clean_action == "reject":
        claimed = await absence_alerts_collection.find_one_and_update(
            {"_id": alert_doc["_id"], "status": "pending"},
            {
                "$set": {
                    "status": "rejected",
                    "actioned_at": now_utc,
                    "updated_at": now_utc,
                }
            },
        )
        if not claimed:
            return _render_action_html_page(
                title="Alert Already Processed",
                badge_text="Already Processed",
                badge_color="#475569",
                message_html=f"<p>This alert for <b>{emp_name}</b> ({emp_email}) on <b>{date_str}</b> was already processed.</p>",
            )
        return _render_action_html_page(
            title="Alert Rejected — No Action Taken",
            badge_text="Rejected",
            badge_color="#475569",
            message_html=(
                f"<p>You selected <b>Reject</b> for <b>{emp_name}</b> ({emp_email}) on <b>{date_str}</b>.</p>"
                f"<p style='margin-bottom:0;'>No warning email was sent to the employee and no 1.5-day penalty was applied.</p>"
            ),
        )

    # --- CASE 2: ADMIN PRESSED "SEND MAIL" -> ATOMIC CLAIM, UPSERT PENALTY ONCE, THEN SEND EMAIL ---
    claimed = await absence_alerts_collection.find_one_and_update(
        {"_id": alert_doc["_id"], "status": {"$in": ["pending", "mail_failed"]}},
        {
            "$set": {
                "status": "processing",
                "processing_started_at": now_utc,
                "updated_at": now_utc,
            }
        },
    )
    if not claimed:
        return _render_action_html_page(
            title="Alert Already Processed",
            badge_text="Already Completed",
            badge_color="#059669",
            message_html=(
                f"<p>The 1.5-day warning email and deduction for <b>{emp_name}</b> ({emp_email}) on <b>{date_str}</b> "
                f"have already been processed.</p>"
            ),
        )

    # If a previous attempt already applied the penalty and only email delivery failed, reuse the recorded cuts
    if claimed.get("penalty_applied"):
        paid_leave_cut = float(claimed.get("paid_leave_cut", 0.0))
        salary_lop_cut = float(claimed.get("salary_lop_cut", 0.0))
        rem_leaves = float(claimed.get("remaining_leaves_before", claimed.get("remaining_leaves_at_alert", 0.0)))
    else:
        rem_leaves = await _get_remaining_paid_leaves(emp_email, org_id, date_str)
        if rem_leaves >= 1.5:
            paid_leave_cut = 1.5
            salary_lop_cut = 0.0
        elif rem_leaves > 0:
            paid_leave_cut = round(rem_leaves, 2)
            salary_lop_cut = round(1.5 - paid_leave_cut, 2)
        else:
            paid_leave_cut = 0.0
            salary_lop_cut = 1.5

        paid_penalty_key = f"absence_penalty:{org_id}:{emp_email}:{date_str}:paid"
        lop_penalty_key = f"absence_penalty:{org_id}:{emp_email}:{date_str}:lop"

        if paid_leave_cut > 0:
            await leave_requests_collection.update_one(
                {"penalty_key": paid_penalty_key},
                {"$set": {
                    "penalty_key": paid_penalty_key,
                    "employee_id": emp_email,
                    "email": emp_email,
                    "organization_id": org_id,
                    "leave_type": "casual_leave",
                    "start_date": date_str,
                    "end_date": date_str,
                    "duration_type": "full_day",
                    "duration": paid_leave_cut,
                    "reason": f"Uninformed absence warning penalty ({paid_leave_cut} day paid leave cut)",
                    "is_paid": True,
                    "is_uninformed_absence_penalty": True,
                    "status": "approved",
                    "admin_notes": f"Auto-applied 1.5-day uninformed absence penalty on {date_str} via Admin Email Approval",
                    "processed_at": now_utc,
                    "processed_by": "admin_email_action",
                    "updated_at": now_utc,
                }, "$setOnInsert": {"created_at": now_utc}},
                upsert=True,
            )

        if salary_lop_cut > 0:
            await leave_requests_collection.update_one(
                {"penalty_key": lop_penalty_key},
                {"$set": {
                    "penalty_key": lop_penalty_key,
                    "employee_id": emp_email,
                    "email": emp_email,
                    "organization_id": org_id,
                    "leave_type": "unpaid",
                    "start_date": date_str,
                    "end_date": date_str,
                    "duration_type": "full_day",
                    "duration": salary_lop_cut,
                    "reason": f"Uninformed absence warning penalty ({salary_lop_cut} day LOP salary cut)",
                    "is_paid": False,
                    "is_uninformed_absence_penalty": True,
                    "status": "approved",
                    "admin_notes": f"Auto-applied 1.5-day uninformed absence salary deduction on {date_str} via Admin Email Approval",
                    "processed_at": now_utc,
                    "processed_by": "admin_email_action",
                    "updated_at": now_utc,
                }, "$setOnInsert": {"created_at": now_utc}},
                upsert=True,
            )

        await absence_alerts_collection.update_one(
            {"_id": alert_doc["_id"]},
            {"$set": {
                "penalty_applied": True,
                "remaining_leaves_before": rem_leaves,
                "paid_leave_cut": paid_leave_cut,
                "salary_lop_cut": salary_lop_cut,
                "updated_at": now_utc,
            }},
        )

    # Also recalculate any existing DRAFT payroll for this employee & month so Payroll page shows the updated 1.5-day deduction immediately
    try:
        from hrms_payroll import fetch_attendance_data, _resolve_employee_salary_structure, fetch_approved_claims_and_reimbursements, calculate_payslip_fields
        from database import payrolls_collection
        from hrms_models import PayrollStatus
        payroll_month = date_str[:7]
        draft_payroll = await payrolls_collection.find_one({
            "organization_id": org_id,
            "employee_email": emp_email,
            "payroll_month": payroll_month,
            "status": PayrollStatus.DRAFT,
        })
        if draft_payroll:
            emp_doc = await employees_collection.find_one({"email": emp_email, "organization_id": org_id}) or {"email": emp_email}
            w_days, p_days, paid_l, unpaid_l, a_days, short_pen = await fetch_attendance_data(emp_email, org_id, payroll_month)
            sal_struct = await _resolve_employee_salary_structure(emp_doc)
            t_reimb, e_claims = await fetch_approved_claims_and_reimbursements(emp_email, org_id, payroll_month)
            reimb = t_reimb + e_claims
            calc = calculate_payslip_fields(
                ctc=float(draft_payroll.get("ctc") or sal_struct.get("ctc") or 0.0),
                working_days=w_days,
                present_days=p_days,
                paid_leave_days=paid_l,
                unpaid_leave_days=unpaid_l,
                short_leave_penalty_days=short_pen,
                overtime_hours=float(draft_payroll.get("overtime_hours") or 0.0),
                bonus=float(draft_payroll.get("bonus") or 0.0),
                other_earnings=float(draft_payroll.get("other_earnings") or 0.0),
                reimbursements=reimb,
                loan_deductions=float(draft_payroll.get("loan_deductions") or 0.0),
                advance_deductions=float(draft_payroll.get("advance_deductions") or 0.0),
                other_deductions=float(draft_payroll.get("other_deductions") or 0.0),
                salary_structure=sal_struct,
                absent_days_override=a_days,
            )
            await payrolls_collection.update_one(
                {"_id": draft_payroll["_id"]},
                {"$set": {
                    "working_days": w_days,
                    "present_days": p_days,
                    "leave_days": paid_l + unpaid_l,
                    "paid_leave_days": calc["paid_leave_days"],
                    "unpaid_leave_days": calc["unpaid_leave_days"],
                    "short_leave_penalty_days": calc["short_leave_penalty_days"],
                    "absent_days": calc["absent_days"],
                    "lop_days": calc["lop_days"],
                    "lop_deduction": calc["lop_deduction"],
                    "attendance_salary": calc["attendance_salary"],
                    "gross_salary": calc["gross_salary"],
                    "total_deductions": calc["total_deductions"],
                    "calculated_net_salary": calc["net_salary"],
                    "net_salary": calc["net_salary"] + float(draft_payroll.get("admin_adjustment") or 0.0),
                    "updated_at": now_utc,
                }},
            )
    except Exception as pay_sync_err:
        logger.warning(f"Payroll draft sync after absence penalty warning: {pay_sync_err}")

    # Send the warning email to the employee
    smtp_cfg = await _get_org_smtp_config(org_id)
    emp_html = _build_employee_warning_email_html(
        comp_name=comp_name,
        emp_name=emp_name,
        emp_email=emp_email,
        date_str=date_str,
        remaining_leaves_before=rem_leaves,
        paid_leave_cut=paid_leave_cut,
        salary_lop_cut=salary_lop_cut,
    )
    emp_subject = f"Official Warning: Uninformed Absence on {date_str} (1.5 Days Deduction)"

    email_ok, delivery_note = await run_in_threadpool(
        _send_email_sync,
        smtp_cfg,
        emp_email,
        emp_subject,
        emp_html,
        None,
        None,
    )

    # Also log an in-app notification for the employee
    try:
        penalty_summary = (
            "1.5 days leave has been deducted"
            if salary_lop_cut == 0
            else (
                "1.5 days salary has been deducted today"
                if paid_leave_cut == 0
                else f"{paid_leave_cut} day leave + {salary_lop_cut} day salary has been deducted"
            )
        )
        await wfh_alerts_collection.insert_one({
            "employee_email": emp_email,
            "employee_name": emp_name,
            "organization_id": org_id,
            "type": "UNINFORMED_ABSENCE_WARNING",
            "severity": "high",
            "status": "pending",
            "details": f"Official Warning ({date_str}): You took leave without informing via email or LogDay. {penalty_summary}.",
            "timestamp": now_utc,
            "created_at": now_utc,
        })
    except Exception:
        pass

    await absence_alerts_collection.update_one(
        {"_id": alert_doc["_id"]},
        {
            "$set": {
                "status": "mail_sent" if email_ok else "mail_failed",
                "employee_email_delivered": email_ok,
                "delivery_note": delivery_note,
                "paid_leave_cut": paid_leave_cut,
                "salary_lop_cut": salary_lop_cut,
                "actioned_at": now_utc,
                "updated_at": now_utc,
            }
        },
    )

    if not email_ok:
        return _render_action_html_page(
            title="Penalty Recorded, But Email Delivery Failed",
            badge_text="Email Error",
            badge_color="#d97706",
            message_html=(
                f"<p>The 1.5-day penalty was recorded for <b>{emp_name}</b> ({emp_email}), "
                f"but sending the email failed:</p><p><code>{delivery_note}</code></p>"
            ),
        )

    deduction_summary_html = (
        f"<b>1.5 Days Paid Leave Deducted</b> (from {rem_leaves} available paid leaves)"
        if salary_lop_cut == 0
        else (
            "<b>1.5 Days Salary Deducted (LOP)</b> (0 paid leaves were pending)"
            if paid_leave_cut == 0
            else f"<b>{paid_leave_cut} Day Paid Leave + {salary_lop_cut} Day Salary Deducted (LOP)</b>"
        )
    )

    return _render_action_html_page(
        title="Warning Email Sent to Employee",
        badge_text="Sent Successfully",
        badge_color="#dc2626",
        message_html=(
            f"<p>The official warning email has been sent to <b>{emp_name}</b> (<b>{emp_email}</b>) for <b>{date_str}</b>.</p>"
            f"<p style='margin-bottom:0;'>Penalty Applied: {deduction_summary_html}.</p>"
        ),
    )


@router.post("/admin/absence-alerts/run-now")
async def admin_trigger_absence_alerts_now(current_admin: Admin = Depends(require_feature("attendance"))):
    """Manually trigger the 4:00 PM absentee check for the admin's organization right now."""
    org_id = current_admin.organization_id or "system_org"
    result = await process_org_absence_alerts(org_id, force_run=True)
    if result.get("status") == "no_recipient_emails":
        raise HTTPException(status_code=400, detail=result.get("message"))
    if result.get("status") == "email_not_configured":
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.get("/admin/absence-alerts")
async def admin_list_absence_alerts(
    date: Optional[str] = None,
    current_admin: Admin = Depends(require_feature("attendance")),
):
    """List recent 4:00 PM absentee alerts and their status ('pending', 'mail_sent', 'rejected') for the organization."""
    org_id = current_admin.organization_id or "system_org"
    query: dict = {}
    if not (current_admin.role == "superadmin" or org_id == "system_org"):
        query["organization_id"] = org_id
    if date:
        query["date"] = date

    docs = await absence_alerts_collection.find(query).sort("created_at", -1).to_list(length=100)
    for d in docs:
        d["_id"] = str(d["_id"])
        d.pop("token", None)
    return docs
