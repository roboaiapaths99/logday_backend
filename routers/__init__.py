from routers.system_uploads import router as system_uploads_router
from routers.auth_employee import router as auth_employee_router
from routers.attendance_core import router as attendance_core_router
from routers.admin_core import router as admin_core_router
from routers.field_ops import router as field_ops_router
from routers.leaves_holidays import router as leaves_holidays_router
from routers.reports_nudges import router as reports_nudges_router
from routers.wfh_employee import router as wfh_employee_router
from routers.wfh_admin import router as wfh_admin_router
from routers.wfh_monitoring import router as wfh_monitoring_router

__all__ = [
    "system_uploads_router",
    "auth_employee_router",
    "attendance_core_router",
    "admin_core_router",
    "field_ops_router",
    "leaves_holidays_router",
    "reports_nudges_router",
    "wfh_employee_router",
    "wfh_admin_router",
    "wfh_monitoring_router",
]
