"""
Feature-Based Access Control for Admin Endpoints.

Provides require_feature() dependency that gates endpoint access
based on admin's allowed_features list.

Superadmin/Owner/Admin roles bypass all feature checks.
Sub-admins (HR, Support, Manager) are restricted to their allowed_features.
"""

from fastapi import Depends, HTTPException
from auth import get_current_admin
from models import Admin, AdminRole


# Master list of all available features in the system
ALL_FEATURES = [
    # Core Attendance & Operations
    {"id": "dashboard", "label": "Dashboard", "category": "Core", "icon": "📊"},
    {"id": "attendance", "label": "Attendance Management", "category": "Core", "icon": "📋"},
    {"id": "employees", "label": "Employee Management", "category": "Core", "icon": "👥"},
    {"id": "leaves", "label": "Leave Management", "category": "Core", "icon": "🏖️"},
    {"id": "reports", "label": "Reports & Analytics", "category": "Core", "icon": "📈"},
    {"id": "announcements", "label": "Announcements", "category": "Core", "icon": "📢"},
    {"id": "expenses", "label": "Expense Management", "category": "Core", "icon": "💳"},

    # Field Force
    {"id": "war_room", "label": "Field War Room", "category": "Field Force", "icon": "🗺️"},
    {"id": "territory", "label": "Territory Management", "category": "Field Force", "icon": "📍"},
    {"id": "nudge", "label": "Nudge Center", "category": "Field Force", "icon": "⚡"},
    {"id": "leaderboard", "label": "Team Leaderboard", "category": "Field Force", "icon": "🏆"},

    # HRMS
    {"id": "onboarding", "label": "Onboarding", "category": "HRMS", "icon": "🚀"},
    {"id": "exit_management", "label": "Exit Management", "category": "HRMS", "icon": "🚪"},
    {"id": "payroll", "label": "Payroll", "category": "HRMS", "icon": "💰"},
    {"id": "document_verification", "label": "Document Verification", "category": "HRMS", "icon": "📄"},

    # Operations
    {"id": "wfh_management", "label": "WFH Management", "category": "Operations", "icon": "🏠"},
    {"id": "wfh_monitoring", "label": "WFH Monitoring", "category": "Operations", "icon": "🖥️"},

    # Admin
    {"id": "settings", "label": "System Settings", "category": "Admin", "icon": "⚙️"},
    {"id": "sub_admins", "label": "Sub-Admin Management", "category": "Admin", "icon": "🛡️"},
]

# Roles that bypass all feature checks
BYPASS_ROLES = {"superadmin", "owner", "admin"}

# All feature IDs for superadmin/owner/admin
ALL_FEATURE_IDS = [f["id"] for f in ALL_FEATURES]


def _is_privileged(admin: Admin) -> bool:
    """Check if admin has a role that bypasses feature checks."""
    role = admin.role if isinstance(admin.role, str) else admin.role.value
    return role in BYPASS_ROLES


def require_feature(feature: str):
    """
    FastAPI dependency factory that enforces feature-level access control.
    
    Usage:
        @app.get("/admin/payroll")
        async def get_payroll(admin = Depends(require_feature("payroll"))):
            ...
    
    Superadmin/Owner/Admin roles bypass this check entirely.
    Sub-admins (HR, Support, Manager) must have the feature in their allowed_features.
    """
    async def _check_feature(current_admin: Admin = Depends(get_current_admin)) -> Admin:
        # Privileged roles bypass all checks
        if _is_privileged(current_admin):
            return current_admin

        # Sub-admins need explicit feature permission
        allowed = current_admin.allowed_features or []

        if feature not in allowed:
            raise HTTPException(
                status_code=403,
                detail=f"Access denied. You don't have permission for '{feature}'. Contact your admin to get access."
            )

        return current_admin

    return _check_feature


def require_any_feature(*features: str):
    """
    Require that the admin has at least ONE of the listed features.
    Useful for endpoints that serve multiple feature areas.
    """
    async def _check_any(current_admin: Admin = Depends(get_current_admin)) -> Admin:
        if _is_privileged(current_admin):
            return current_admin

        allowed = current_admin.allowed_features or []
        if not any(f in allowed for f in features):
            raise HTTPException(
                status_code=403,
                detail=f"Access denied. You need one of: {', '.join(features)}."
            )
        return current_admin

    return _check_any


def get_all_features():
    """Return the master feature list for the frontend to render toggles."""
    return ALL_FEATURES


def get_all_feature_ids():
    """Return all feature IDs (used when creating new owner/admin accounts)."""
    return ALL_FEATURE_IDS
