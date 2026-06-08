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

# Ensure indexes for employee check-in status and session tracking
async def ensure_employee_indexes():
    # Core employee indexes
    await employees_collection.create_index("checked_in")
    await employees_collection.create_index("session_id")
    await employees_collection.create_index("email", unique=True, sparse=True)
    await employees_collection.create_index([("organization_id", 1), ("employee_type", 1)])

    # WFH Sessions: queries by employee_id+status, and by email+status
    await wfh_sessions_collection.create_index([("employee_id", 1), ("status", 1)])
    await wfh_sessions_collection.create_index([("employee_email", 1), ("status", 1)])
    await wfh_sessions_collection.create_index([("organization_id", 1), ("status", 1)])

    # WFH Screenshots: queries sorted by timestamp, filtered by employee_id or employee_email
    await wfh_screenshots_collection.create_index([("employee_id", 1), ("timestamp", -1)])
    await wfh_screenshots_collection.create_index([("employee_email", 1), ("timestamp", -1)])
    await wfh_screenshots_collection.create_index("timestamp")  # for purge job

    # WFH Activity: queries sorted by timestamp, filtered by employee_id or employee_email
    await wfh_activity_collection.create_index([("employee_id", 1), ("timestamp", -1)])
    await wfh_activity_collection.create_index([("employee_email", 1), ("timestamp", -1)])

    # WFH Productivity: queries by date + org — THIS was causing the 20s timeout
    await wfh_productivity_collection.create_index([("date", 1), ("organization_id", 1)])
    await wfh_productivity_collection.create_index([("employee_id", 1), ("date", 1)])

    # WFH Alerts: queries by status + timestamp, and by employee_email for timeline
    await wfh_alerts_collection.create_index([("status", 1), ("timestamp", -1)])
    await wfh_alerts_collection.create_index([("organization_id", 1), ("status", 1), ("timestamp", -1)])
    await wfh_alerts_collection.create_index([("employee_email", 1), ("timestamp", -1)])

    # WFH Commands: queries by employee_email + status (polling endpoint)
    await wfh_commands_collection.create_index([("employee_email", 1), ("status", 1)])

    # App usage: queries by employee_id + date
    await wfh_app_usage_collection.create_index([("employee_id", 1), ("date", 1)])

    # Face checks: queries by employee_id
    await wfh_face_checks_collection.create_index([("employee_id", 1), ("timestamp", -1)])