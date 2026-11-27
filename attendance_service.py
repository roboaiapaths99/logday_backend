"""
Attendance Classification Service.
Pure business logic engine for determining daily attendance status,
calculating late/early hours, short leave, and half-day classifications.
Extracted to eliminate circular imports between main.py and hrms_payroll.py.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any
import logging

logger = logging.getLogger(__name__)


def classify_attendance_day(
    first_checkin_local: Optional[datetime],
    last_checkout_local: Optional[datetime],
    worked_hours: float,
    settings: Optional[dict] = None,
    employee_type: str = "desk",
    approved_leave: Optional[dict] = None
) -> dict:
    """
    Central enterprise classification engine for an employee's attendance day.
    Evaluates:
    - Office/Shift timings (desk vs field configurable by admin)
    - Late grace & late threshold
    - Late -> Short Leave threshold (mins or time)
    - Late -> Half Day threshold (time, e.g. after 11:30 or lunch)
    - Early Exit -> Half Day threshold (time, e.g. before 14:00)
    - Early Exit -> Short Leave threshold (mins before shift end)
    - Min Half-Day hours & Min Full-Day hours
    - Split off-hours: late deficit vs early exit deficit
    - Applied Short Leave / Half Day vs Attendance-triggered Short Leave / Half Day
    """
    settings = settings or {}
    emp_type = str(employee_type).lower().strip()
    is_field = (emp_type == "field") and settings.get("custom_field_rules_enabled", True)

    def _parse_t(t_str, def_h, def_m):
        try:
            if t_str and ":" in str(t_str):
                parts = str(t_str).strip().split(":")
                return int(parts[0]), int(parts[1])
        except Exception:
            pass
        return def_h, def_m

    # 1. Timings & thresholds from admin configuration
    if is_field:
        start_h, start_m = _parse_t(settings.get("field_office_start_time") or settings.get("office_start_time"), 10, 0)
        end_h, end_m = _parse_t(settings.get("field_office_end_time") or settings.get("office_end_time"), 19, 0)
        late_grace_mins = int(settings.get("field_late_grace_mins") if settings.get("field_late_grace_mins") is not None else settings.get("late_grace_mins", 30))
        late_to_short_mins = int(settings.get("field_late_to_short_leave_mins") if settings.get("field_late_to_short_leave_mins") is not None else settings.get("late_to_short_leave_mins", 45))
        late_half_h, late_half_m = _parse_t(settings.get("field_late_to_half_day_time") or settings.get("late_to_half_day_time"), 12, 30)
        early_half_h, early_half_m = _parse_t(settings.get("field_early_leave_half_day_time") or settings.get("early_leave_half_day_time"), 15, 0)
        early_short_mins = int(settings.get("field_early_leave_short_mins") if settings.get("field_early_leave_short_mins") is not None else settings.get("early_leave_short_mins", 45))
        half_day_min_hours = float(settings.get("field_half_day_min_hours") if settings.get("field_half_day_min_hours") is not None else settings.get("half_day_min_hours", 4.0))
        full_day_min_hours = float(settings.get("field_full_day_min_hours") if settings.get("field_full_day_min_hours") is not None else settings.get("full_day_min_hours", 8.0))
        required_hours = float(settings.get("field_required_hours") if settings.get("field_required_hours") is not None else settings.get("required_hours", 9.0))
        short_leave_max_hours = float(settings.get("field_short_leave_max_hours") if settings.get("field_short_leave_max_hours") is not None else settings.get("short_leave_max_hours", 2.0))
    else:
        start_h, start_m = _parse_t(settings.get("office_start_time"), 9, 0)
        end_h, end_m = _parse_t(settings.get("office_end_time"), 18, 0)
        late_grace_mins = int(settings.get("late_grace_mins") if settings.get("late_grace_mins") is not None else settings.get("late_threshold_mins", 15))
        
        # Support either exact cutoff time or relative mins for late-to-short-leave
        if settings.get("short_leave_cutoff_time"):
            sl_h, sl_m = _parse_t(settings.get("short_leave_cutoff_time"), 10, 15)
            late_to_short_mins = max(0, (sl_h * 60 + sl_m) - (start_h * 60 + start_m))
        else:
            late_to_short_mins = int(settings.get("late_to_short_leave_mins", 45))

        late_half_h, late_half_m = _parse_t(settings.get("half_day_late_cutoff_time") or settings.get("late_to_half_day_time"), 11, 30)
        early_half_h, early_half_m = _parse_t(settings.get("half_day_early_exit_cutoff_time") or settings.get("early_leave_half_day_time"), 14, 0)
        early_short_mins = int(settings.get("early_leave_short_mins", 45))
        half_day_min_hours = float(settings.get("half_day_min_hours", 4.0))
        full_day_min_hours = float(settings.get("full_day_min_hours", 8.0))
        required_hours = float(settings.get("required_hours", 8.0))
        short_leave_max_hours = float(settings.get("short_leave_max_hours", 2.0))

    # Format strings for explanations
    late_half_str = f"{late_half_h:02d}:{late_half_m:02d}"
    early_half_str = f"{early_half_h:02d}:{early_half_m:02d}"

    # Handle No Check-in Case
    if not first_checkin_local:
        if approved_leave:
            dur_type = str(approved_leave.get("duration_type", "")).lower().strip()
            l_type = str(approved_leave.get("leave_type", "leave")).title()
            if dur_type == "short_leave":
                return {
                    "status": "Short Leave",
                    "is_late": False,
                    "is_early_leave": False,
                    "is_short_leave": True,
                    "is_half_day": False,
                    "short_leave_type": "applied",
                    "half_day_type": None,
                    "late_mins": 0,
                    "early_exit_mins": 0,
                    "late_off_hours": 0.0,
                    "early_off_hours": 0.0,
                    "off_hours": 0.0,
                    "worked_hours": 0.0,
                    "required_hours": required_hours,
                    "effective_days": 1.0,
                    "classification_reason": f"Approved Short Leave ({l_type})",
                    "is_applied_leave": True
                }
            elif (dur_type in ["first_half", "second_half", "half_day"] or approved_leave.get("duration") == 0.5) and not approved_leave.get("is_uninformed_absence_penalty"):
                return {
                    "status": "Half Day",
                    "is_late": False,
                    "is_early_leave": False,
                    "is_short_leave": False,
                    "is_half_day": True,
                    "short_leave_type": None,
                    "half_day_type": f"{dur_type}_applied",
                    "late_mins": 0,
                    "early_exit_mins": 0,
                    "late_off_hours": 0.0,
                    "early_off_hours": 0.0,
                    "off_hours": 0.0,
                    "worked_hours": 0.0,
                    "required_hours": required_hours,
                    "effective_days": 0.5,
                    "classification_reason": f"Approved Half Day ({dur_type})",
                    "is_applied_leave": True
                }
            else:
                return {
                    "status": f"Leave ({l_type})",
                    "is_late": False,
                    "is_early_leave": False,
                    "is_short_leave": False,
                    "is_half_day": False,
                    "short_leave_type": None,
                    "half_day_type": None,
                    "late_mins": 0,
                    "early_exit_mins": 0,
                    "late_off_hours": 0.0,
                    "early_off_hours": 0.0,
                    "off_hours": 0.0,
                    "worked_hours": 0.0,
                    "required_hours": required_hours,
                    "effective_days": 0.0,
                    "classification_reason": f"Approved Leave ({l_type})",
                    "is_applied_leave": True
                }

        return {
            "status": "Absent",
            "is_late": False,
            "is_early_leave": False,
            "is_short_leave": False,
            "is_half_day": False,
            "short_leave_type": None,
            "half_day_type": None,
            "late_mins": 0,
            "early_exit_mins": 0,
            "late_off_hours": 0.0,
            "early_off_hours": 0.0,
            "off_hours": 0.0,
            "worked_hours": 0.0,
            "required_hours": required_hours,
            "effective_days": 0.0,
            "classification_reason": "No check-in recorded",
            "is_applied_leave": False
        }

    # Reference datetimes for shift evaluation
    shift_start = first_checkin_local.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    shift_end = first_checkin_local.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    grace_cutoff = shift_start + timedelta(minutes=late_grace_mins)
    short_leave_late_cutoff = shift_start + timedelta(minutes=late_to_short_mins)
    half_day_late_cutoff = first_checkin_local.replace(hour=late_half_h, minute=late_half_m, second=0, microsecond=0)
    half_day_early_cutoff = first_checkin_local.replace(hour=early_half_h, minute=early_half_m, second=0, microsecond=0)
    short_leave_early_cutoff = shift_end - timedelta(minutes=early_short_mins)

    # 2. Late Arrival Evaluation
    late_mins = 0
    late_off_hours = 0.0
    is_late = False
    if first_checkin_local > shift_start:
        diff_sec = (first_checkin_local - shift_start).total_seconds()
        raw_late_mins = max(0, int(diff_sec / 60))
        if first_checkin_local > grace_cutoff:
            is_late = True
            late_mins = raw_late_mins
            late_off_hours = round(late_mins / 60.0, 2)
        else:
            # Arrival within grace is fully ON-TIME: zero penalty, zero deficit
            is_late = False
            late_mins = 0
            late_off_hours = 0.0

    # 3. Early Departure Evaluation
    early_exit_mins = 0
    early_off_hours = 0.0
    is_early_leave = False
    if last_checkout_local:
        if last_checkout_local < shift_end:
            diff_sec = (shift_end - last_checkout_local).total_seconds()
            raw_early_mins = max(0, int(diff_sec / 60))
            if raw_early_mins > 10:  # 10 mins early checkout grace
                is_early_leave = True
                early_exit_mins = raw_early_mins
                early_off_hours = round(early_exit_mins / 60.0, 2)

    # Total off-hours: waived / zeroed out across the app
    total_off_hours = 0.0
    late_off_hours = 0.0
    early_off_hours = 0.0

    # 4. Status Determination
    status = "Present"
    is_short_leave = False
    is_half_day = False
    short_leave_type = None
    half_day_type = None
    effective_days = 1.0
    classification_reason = "On-time full day"
    is_applied_leave = False

    # A) Check if approved applied leave exists for today
    if approved_leave:
        dur_type = str(approved_leave.get("duration_type", "")).lower().strip()
        slot = str(approved_leave.get("short_leave_slot", "")).lower().strip()
        if dur_type == "short_leave":
            is_short_leave = True
            short_leave_type = "applied"
            is_applied_leave = True
            status = "Short Leave"
            slot_label = f" ({slot.title()})" if slot else ""
            classification_reason = f"Approved Short Leave{slot_label}"
            if slot == "morning":
                is_late = False
                late_mins = 0
            elif slot == "evening":
                is_early_leave = False
                early_exit_mins = 0
        elif (dur_type in ["first_half", "second_half", "half_day"] or approved_leave.get("duration") == 0.5) and not approved_leave.get("is_uninformed_absence_penalty"):
            is_half_day = True
            half_day_type = f"{dur_type}_applied"
            is_applied_leave = True
            status = "Half Day"
            effective_days = 0.5
            classification_reason = f"Approved Half Day ({dur_type.replace('_', ' ').title()})"
            if "first" in dur_type:
                is_late = False
                late_mins = 0
            elif "second" in dur_type:
                is_early_leave = False
                early_exit_mins = 0

    # B) Evaluate Half Day thresholds if not already half day or approved leave
    if not is_half_day and not is_short_leave:
        if first_checkin_local >= half_day_late_cutoff:
            is_half_day = True
            half_day_type = "late_arrival"
            status = "Half Day"
            effective_days = 0.5
            classification_reason = f"Check-in at {first_checkin_local.strftime('%H:%M')} (after Half-Day limit {late_half_str})"
        elif last_checkout_local and last_checkout_local <= half_day_early_cutoff:
            is_half_day = True
            half_day_type = "early_departure"
            status = "Half Day"
            effective_days = 0.5
            classification_reason = f"Check-out at {last_checkout_local.strftime('%H:%M')} (before Half-Day limit {early_half_str})"
        elif last_checkout_local and 0 < worked_hours < half_day_min_hours:
            is_half_day = True
            half_day_type = "low_hours"
            status = "Half Day"
            effective_days = 0.5
            classification_reason = f"Worked {worked_hours:.1f}h (< min half-day requirement {half_day_min_hours:.1f}h)"

    # A day classified as Half Day is counted only as a Half Day (never double-counted as Late)
    if is_half_day:
        is_late = False
        late_mins = 0

    # C) Short Leave is strictly for approved leave applications (never automatic)
    # D) Fallback states for standard working day
    if not is_half_day and not is_short_leave:
        if last_checkout_local and worked_hours >= (full_day_min_hours - 1.0):
            status = "Present"
            classification_reason = f"Full day attendance (worked {worked_hours:.1f}h)" if is_late else "On-time full day"
        elif is_early_leave:
            status = "Early Leave"
            classification_reason = f"Left {early_exit_mins} mins early"
        elif is_late:
            status = "Late"
            classification_reason = f"Late check-in by {late_mins} mins"
        else:
            status = "Present"
            classification_reason = "On-time attendance"

    return {
        "status": status,
        "is_late": is_late,
        "is_early_leave": is_early_leave,
        "is_short_leave": is_short_leave,
        "is_half_day": is_half_day,
        "short_leave_type": short_leave_type,
        "half_day_type": half_day_type,
        "late_mins": late_mins,
        "early_exit_mins": early_exit_mins,
        "late_off_hours": 0.0,
        "early_off_hours": 0.0,
        "off_hours": 0.0,
        "worked_hours": round(worked_hours, 2),
        "required_hours": round(required_hours, 2),
        "effective_days": effective_days,
        "classification_reason": classification_reason,
        "is_applied_leave": is_applied_leave
    }
