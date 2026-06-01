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
    await employees_collection.create_index("checked_in")
    await employees_collection.create_index("session_id")