import motor.motor_asyncio
import os
import certifi
from dotenv import load_dotenv

load_dotenv()

MONGODB_URL = os.getenv("MONGODB_URL")
DATABASE_NAME = os.getenv("DATABASE_NAME", "logday")

client = motor.motor_asyncio.AsyncIOMotorClient(
    MONGODB_URL,
    serverSelectionTimeoutMS=5000,   # fail fast if Atlas is unreachable
    connectTimeoutMS=5000,
    socketTimeoutMS=20000,
    tlsCAFile=certifi.where(),
)
db = client[DATABASE_NAME]

async def get_database():
    return db

# Collections
uploads_collection = db["uploads_registry"]
oauth_states_collection = db["oauth_states"]
auth_sessions_collection = db["auth_sessions"]
employee_invitations_collection = db["employee_invitations"]
employees_collection = db["employees"]
attendance_logs_collection = db["attendance_logs"]
settings_collection = db["settings"]
admins_collection = db["admins"]
organizations_collection = db["organizations"]
visit_plans_collection = db["visit_plans"]
visit_logs_collection = db["visit_logs"]
location_pings_collection = db["location_pings"]
km_reimbursements_collection = db["km_reimbursements"]
expense_claims_collection = db["expense_claims"]
otps_collection = db["otps"]
alerts_collection = db["alerts"]
leave_requests_collection = db["leave_requests"]
visit_plan_templates_collection = db["visit_plan_templates"]
nudge_logs_collection = db["nudge_logs"]
# WFH Collections
wfh_sessions_collection = db["wfh_sessions"]
wfh_screenshots_collection = db["wfh_screenshots"]
wfh_activity_collection = db["wfh_activity"]
wfh_app_usage_collection = db["wfh_app_usage"]
wfh_productivity_collection = db["wfh_productivity"]
wfh_alerts_collection = db["wfh_alerts"]
wfh_meetings_collection = db["wfh_meetings"]
wfh_device_info_collection = db["wfh_device_info"]
wfh_signals_collection = db["wfh_signals"]
wfh_commands_collection = db["wfh_commands"]
wfh_face_checks_collection = db["wfh_face_checks"]
tasks_collection = db["tasks"]
wfh_requests_collection = db["wfh_requests"]


# HRMS Collections
onboardings_collection = db["onboardings"]
payrolls_collection = db["payrolls"]
exit_managements_collection = db["exit_managements"]
salary_structures_collection = db["salary_structures"]
holidays_collection = db["holidays"]
leave_policies_collection = db["leave_policies"]
absence_alerts_collection = db["absence_alerts"]
scheduler_locks_collection = db["scheduler_locks"]


# Ensure indexes for employee check-in status and session tracking (each index isolated so one conflict never stops others)
async def ensure_employee_indexes():
    import logging
    logger = logging.getLogger(__name__)

    index_specs = [
        (oauth_states_collection, "expires_at", {"expireAfterSeconds": 0}),
        (auth_sessions_collection, "expires_at", {"expireAfterSeconds": 0}),
        (employee_invitations_collection, "expires_at", {"expireAfterSeconds": 0}),
        (scheduler_locks_collection, "expires_at", {"expireAfterSeconds": 0}),
        (wfh_signals_collection, "created_at", {"expireAfterSeconds": 3600}),
        (wfh_commands_collection, "timestamp", {"expireAfterSeconds": 86400}),
        (uploads_collection, "url", {"unique": True}),
        (uploads_collection, [("organization_id", 1), ("owner_id", 1)], {}),
        (employees_collection, "checked_in", {}),
        (employees_collection, "session_id", {}),
        (employees_collection, [("organization_id", 1), ("employee_type", 1)], {}),
        (wfh_sessions_collection, [("employee_id", 1), ("status", 1)], {}),
        (wfh_sessions_collection, [("employee_email", 1), ("status", 1)], {}),
        (wfh_sessions_collection, [("organization_id", 1), ("status", 1)], {}),
        (wfh_screenshots_collection, [("employee_id", 1), ("timestamp", -1)], {}),
        (wfh_screenshots_collection, [("employee_email", 1), ("timestamp", -1)], {}),
        (wfh_screenshots_collection, "timestamp", {}),
        (wfh_activity_collection, [("employee_id", 1), ("timestamp", -1)], {}),
        (wfh_activity_collection, [("employee_email", 1), ("timestamp", -1)], {}),
        (wfh_productivity_collection, [("date", 1), ("organization_id", 1)], {}),
        (wfh_productivity_collection, [("employee_id", 1), ("date", 1)], {}),
        (wfh_alerts_collection, [("status", 1), ("timestamp", -1)], {}),
        (wfh_alerts_collection, [("organization_id", 1), ("status", 1), ("timestamp", -1)], {}),
        (wfh_alerts_collection, [("employee_email", 1), ("timestamp", -1)], {}),
        (wfh_commands_collection, [("employee_email", 1), ("status", 1)], {}),
        (wfh_app_usage_collection, [("employee_id", 1), ("date", 1)], {}),
        (wfh_face_checks_collection, [("employee_id", 1), ("timestamp", -1)], {}),
        (absence_alerts_collection, [("organization_id", 1), ("employee_email", 1), ("date", 1)], {"unique": True, "sparse": True}),
        (absence_alerts_collection, "token", {"unique": True, "sparse": True}),
        (leave_requests_collection, "penalty_key", {"unique": True, "sparse": True}),
        (payrolls_collection, [("organization_id", 1), ("employee_email", 1), ("payroll_month", 1)], {"unique": True, "sparse": True}),
        (attendance_logs_collection, [("organization_id", 1), ("offline_id", 1)], {"unique": True, "sparse": True}),
        (visit_logs_collection, [("organization_id", 1), ("offline_id", 1)], {"unique": True, "sparse": True}),
        (location_pings_collection, [("organization_id", 1), ("offline_id", 1)], {"unique": True, "sparse": True}),
    ]

    try:
        existing_emp_indexes = await employees_collection.index_information()
        if "email_1" not in existing_emp_indexes and "employees_email" not in existing_emp_indexes:
            await employees_collection.create_index("email", unique=True, sparse=True)
    except Exception as e:
        logger.warning(f"Employee email index creation skipped: {e}")

    for col, keys, kwargs in index_specs:
        try:
            await col.create_index(keys, **kwargs)
        except Exception as e:
            logger.warning(f"Index creation skipped on {getattr(col, 'name', col)} ({keys}): {e}")