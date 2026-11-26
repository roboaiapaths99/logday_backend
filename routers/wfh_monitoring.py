from upload_security import SecureMediaRoute
from fastapi import APIRouter
from routers.wfh_employee import router as wfh_employee_router
from routers.wfh_admin import router as wfh_admin_router

router = APIRouter(route_class=SecureMediaRoute)
router.include_router(wfh_employee_router)
router.include_router(wfh_admin_router)
