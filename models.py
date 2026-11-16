from pydantic import BaseModel, Field, model_validator
from typing import List, Optional, Any
from datetime import datetime
from enum import Enum


class EmployeeType(str, Enum):
    DESK = "desk"
    FIELD = "field"
    OFFICE = "office"
    WFH = "wfh"
    HYBRID = "hybrid"


class AdminFeature(str, Enum):
    DASHBOARD = "dashboard"
    ATTENDANCE = "attendance"
    EMPLOYEES = "employees"
    LEAVES = "leaves"
    REPORTS = "reports"
    ANNOUNCEMENTS = "announcements"
    EXPENSES = "expenses"
    WAR_ROOM = "war_room"
    TERRITORY = "territory"
    NUDGE = "nudge"
    LEADERBOARD = "leaderboard"
    ONBOARDING = "onboarding"
    EXIT_MANAGEMENT = "exit_management"
    PAYROLL = "payroll"
    DOCUMENT_VERIFICATION = "document_verification"
    WFH_MANAGEMENT = "wfh_management"
    WFH_MONITORING = "wfh_monitoring"
    SETTINGS = "settings"
    SUB_ADMINS = "sub_admins"
    FLYFIELD = "flyfield"


class WFHStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class WFHRequest(BaseModel):
    employee_id: str
    employee_email: str
    employee_name: str
    organization_id: str
    date: str  # YYYY-MM-DD (single day)
    reason: str
    status: WFHStatus = WFHStatus.PENDING
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)



class TerritoryType(str, Enum):
    RADIUS = "radius"
    POLYGON = "polygon"


class AttendanceType(str, Enum):
    OFFICE = "office"
    REMOTE_FIELD = "remote_field"
    WFH = "wfh"


class CheckInMethod(str, Enum):
    WIFI_GEOFENCE = "wifi_geofence"
    GPS_TERRITORY = "gps_territory"
    OTP_FALLBACK = "otp_fallback"


class PlanStatus(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"


class CustomerType(str, Enum):
    SCHOOL = "school"
    CUSTOMER = "customer"
    PROSPECT = "prospect"
    OTHER = "other"


class Priority(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class MediaType(str, Enum):
    PHOTO = "photo"
    VOICE_NOTE = "voice_note"
    DOCUMENT = "document"


class PingSource(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"


class ReimbursementStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAID = "paid"


class ExpenseStatus(str, Enum):
    PENDING = "pending"
    QUERIED = "queried"
    APPROVED = "approved"
    REJECTED = "rejected"
    PAID = "paid"


class AlertCategory(str, Enum):
    IDENTITY = "Identity"
    TERRITORY = "Territory"
    COMPLIANCE = "Compliance"
    PRODUCTIVITY = "Productivity"


class AlertSeverity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class AlertStatus(str, Enum):
    PENDING = "pending"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class SalaryStructure(BaseModel):
    """Per-employee salary breakdown — stored in employees collection"""
    ctc: float = 0.0                    # Annual CTC
    basic_salary: float = 0.0           # Monthly basic (typically 40-50% of CTC/12)
    hra: float = 0.0                    # House Rent Allowance
    special_allowance: float = 0.0      # Special / Flexible allowance
    other_allowances: float = 0.0       # Conveyance, medical, etc.
    pf_enabled: bool = True             # PF deduction enabled
    esi_enabled: bool = False           # ESI deduction enabled (only if gross <= 21000)
    professional_tax_enabled: bool = True
    bank_name: Optional[str] = None
    bank_account: Optional[str] = None
    bank_ifsc: Optional[str] = None
    pan_number: Optional[str] = None
    effective_from: Optional[str] = None  # YYYY-MM-DD


class EmployeeBase(BaseModel):
    full_name: str
    email: str
    employee_id: str
    designation: str = "Employee"
    department: str = "General"
    organization_id: str
    employee_type: EmployeeType = EmployeeType.DESK
    field_mode: Optional[str] = "planned"
    manager_id: Optional[str] = None
    beat_zone_name: Optional[str] = None
    salary_structure: Optional[SalaryStructure] = None



class EmployeeCreate(EmployeeBase):
    password: str


class EmployeeProfile(EmployeeBase):
    created_at: datetime
    profile_image: Optional[str] = None
    is_manager: bool = False


class EmployeeDB(EmployeeBase):
    id: str = Field(alias="_id")
    hashed_password: str
    face_embedding: List[float]
    profile_image: Optional[str] = None
    device_id: Optional[str] = None
    territory_type: Optional[TerritoryType] = None
    territory_center_lat: Optional[float] = None
    territory_center_lng: Optional[float] = None
    territory_radius_meters: Optional[float] = None
    territory_polygon: Optional[List[dict]] = None
    gps_otp_fallback_enabled: bool = True
    created_at: datetime = Field(default_factory=datetime.utcnow)


class LocationData(BaseModel):
    lat: float
    long: float


class AttendanceLog(BaseModel):
    user_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    type: str  # "check-in" or "check-out"
    attendance_type: AttendanceType = AttendanceType.OFFICE
    location: Optional[LocationData] = None
    check_in_lat: Optional[float] = None
    check_in_lng: Optional[float] = None
    check_in_accuracy_meters: Optional[float] = None
    check_in_method: Optional[CheckInMethod] = None
    wifi_info: Optional[dict] = None
    wifi_confidence: float = 0.0
    confidence_score: float = 0.0
    status: str = "Present"
    selfie_verified: bool = False
    mock_location_detected: bool = False
    otp_fallback_used: bool = False
    otp_fallback_reason: Optional[str] = None
    offline_id: Optional[str] = None
    synced_at: Optional[datetime] = None


class Token(BaseModel):
    access_token: str
    token_type: str


class TokenData(BaseModel):
    email: Optional[str] = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: str
    user: EmployeeProfile
    needs_face_enrollment: Optional[bool] = None
    force_password_change: Optional[bool] = False


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class RegisterRequest(BaseModel):
    full_name: str
    email: str
    employee_id: str
    designation: str
    department: str
    organization_id: Optional[str] = None
    password: str
    face_image: Optional[str] = None
    device_id: Optional[str] = None
    employee_type: EmployeeType = EmployeeType.DESK
    field_mode: Optional[str] = "planned"


class LoginRequest(BaseModel):
    email: str
    password: str
    device_id: Optional[str] = None
    organization_id: Optional[str] = None
    client_type: Optional[str] = None


class VerifyPresenceRequest(BaseModel):
    email: Optional[str] = None
    image: Optional[str] = None
    lat: Optional[float] = None
    long: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy: Optional[float] = None
    wifi_ssid: str = ""
    wifi_bssid: str = ""
    wifi_strength: float = -50.0
    address: Optional[str] = None
    intended_type: Optional[str] = None
    punch_type: Optional[str] = None
    device_id: Optional[str] = None
    otp_used: bool = False
    mock_detected: bool = False
    client_type: Optional[str] = None
    organization_id: Optional[str] = None

    @model_validator(mode="after")
    def sync_coords(self):
        if self.lat is None and self.latitude is not None:
            self.lat = self.latitude
        elif self.latitude is None and self.lat is not None:
            self.latitude = self.lat
        if self.long is None and self.longitude is not None:
            self.long = self.longitude
        elif self.longitude is None and self.long is not None:
            self.longitude = self.long
        return self

    def get_lat(self) -> float:
        return self.lat if self.lat is not None else (self.latitude or 0.0)

    def get_long(self) -> float:
        return self.long if self.long is not None else (self.longitude or 0.0)


class UpdateFaceRequest(BaseModel):
    email: str
    password: str
    face_image: str
    lat: float
    long: float
    wifi_ssid: str = ""
    wifi_bssid: str = ""
    wifi_strength: float = -50.0
    device_id: Optional[str] = None


class AdminRole(str, Enum):
    SUPERADMIN = "superadmin"
    OWNER = "owner"
    ADMIN = "admin"
    HR = "hr"
    SUPPORT = "support"
    MANAGER = "manager"


class Organization(BaseModel):
    name: str
    slug: str
    logo_url: Optional[str] = None
    primary_color: str = "#0f172a"
    address: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Admin(BaseModel):
    email: str
    full_name: str
    role: AdminRole = AdminRole.HR
    organization_id: Optional[str] = None
    allowed_features: List[str] = ["dashboard"]
    created_at: datetime = Field(default_factory=datetime.utcnow)


class OrganizationRegisterRequest(BaseModel):
    org_name: str
    org_slug: str
    admin_email: str
    admin_password: str
    admin_full_name: str
    logo_url: Optional[str] = None
    primary_color: str = "#0f172a"


class AdminLoginRequest(BaseModel):
    email: str
    password: str


class SubAdminCreate(BaseModel):
    full_name: str
    email: str
    password: str
    role: AdminRole = AdminRole.HR
    organization_id: Optional[str] = None


class EmployeeUpdate(BaseModel):
    full_name: Optional[str] = None
    designation: Optional[str] = None
    department: Optional[str] = None
    status: Optional[str] = None
    employee_type: Optional[EmployeeType] = None
    field_mode: Optional[str] = None
    manager_id: Optional[str] = None
    territory_type: Optional[TerritoryType] = None
    territory_center_lat: Optional[float] = None
    territory_center_lng: Optional[float] = None
    territory_radius_meters: Optional[float] = None
    territory_polygon: Optional[List[dict]] = None
    salary_structure: Optional[SalaryStructure] = None



class ConvertAbsentToLeaveRequest(BaseModel):
    employee_email: str
    date: str  # YYYY-MM-DD
    leave_type: str = "casual_leave"  # sick_leave, casual_leave, on_duty, other, unpaid
    duration_type: str = "full_day"   # full_day, first_half, second_half
    duration: float = 1.0             # 1.0 or 0.5
    is_paid: Optional[bool] = True    # True = Paid Leave, False = LOP
    reason: str = "Regularized by Admin (Informed via Email / WhatsApp)"
    admin_notes: Optional[str] = None


class SystemSettings(BaseModel):
    # Desk / General Office Timings
    office_start_time: str = "09:00"
    office_end_time: str = "18:00"
    lunch_start_time: str = "13:00"
    lunch_end_time: str = "14:00"
    late_threshold_mins: int = 15
    late_grace_mins: int = 15
    late_to_short_leave_mins: int = 45
    late_to_half_day_time: str = "11:30"
    early_leave_short_mins: int = 45
    early_leave_half_day_time: str = "14:00"
    half_day_min_hours: float = 4.0
    full_day_min_hours: float = 8.0
    required_hours: float = 8.0
    
    # Field specific toggle & defaults
    custom_field_rules_enabled: bool = False
    field_office_start_time: str = "10:00"
    field_office_end_time: str = "19:00"
    field_lunch_start_time: str = "13:00"
    field_lunch_end_time: str = "14:00"
    field_late_threshold_mins: int = 30
    field_late_grace_mins: int = 30
    field_late_to_short_leave_mins: int = 45
    field_late_to_half_day_time: str = "12:30"
    field_early_leave_short_mins: int = 45
    field_early_leave_half_day_time: str = "15:00"
    field_half_day_min_hours: float = 4.0
    field_full_day_min_hours: float = 8.0
    field_required_hours: float = 9.0
    field_visits_goal: int = 10
    field_km_goal: float = 20.0
    field_rate_per_km: float = 10.0
    field_settings: Optional[dict] = None
    
    timezone_offset: int = 330
    primary_color: Optional[str] = "#6366f1"
    logo_url: Optional[str] = None
    office_lat: float = 0.0
    office_long: float = 0.0
    office_wifi_ssid: Optional[str] = None
    office_wifi_bssid: Optional[str] = None
    geofence_radius: float = 40.0


# Field Force Specific Models
class VisitPlanStop(BaseModel):
    sequence_order: int
    place_name: str
    place_lat: float
    place_lng: float
    place_address: Optional[str] = None
    customer_type: CustomerType = CustomerType.OTHER
    priority: Priority = Priority.MEDIUM
    manager_reordered: bool = False
    expense_tags: Optional[List[dict]] = None # Array of {type, amount, proof_url, description}


class VisitPlan(BaseModel):
    employee_id: str
    organization_id: str
    date: str  # YYYY-MM-DD
    status: PlanStatus = PlanStatus.DRAFT
    stops: List[VisitPlanStop]
    submitted_at: Optional[datetime] = None
    reviewed_at: Optional[datetime] = None
    reviewed_by: Optional[str] = None
    manager_comments: Optional[str] = None
    is_recurring: bool = False
    template_id: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Visit(BaseModel):
    employee_id: str
    organization_id: str
    date: str  # YYYY-MM-DD
    visit_plan_stop_id: Optional[str] = None
    check_in_time: datetime = Field(default_factory=datetime.utcnow)
    check_out_time: Optional[datetime] = None
    check_in_lat: float
    check_in_lng: float
    check_in_accuracy: float
    check_out_lat: Optional[float] = None
    check_out_lng: Optional[float] = None
    geofence_validated: bool = False
    person_met_name: Optional[str] = None
    person_met_role: Optional[str] = None
    remarks: Optional[str] = None
    outcome: Optional[str] = None
    order_captured: bool = False
    lead_captured: bool = False
    offline_id: Optional[str] = None
    synced_at: Optional[datetime] = None


class VisitMedia(BaseModel):
    visit_id: str
    media_type: MediaType
    file_url: str
    file_size: int
    gps_lat: float
    gps_lng: float
    captured_at: datetime = Field(default_factory=datetime.utcnow)
    metadata_verified: bool = False


class LocationPing(BaseModel):
    employee_id: Optional[str] = None
    organization_id: Optional[str] = None
    attendance_id: Optional[str] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy: float = 10.0
    recorded_at: datetime = Field(default_factory=datetime.utcnow)
    source: Any = "auto"
    offline_id: Optional[str] = None
    synced_at: Optional[datetime] = None

    @model_validator(mode="after")
    def sync_coords(self):
        if self.lat is None and self.latitude is not None:
            self.lat = self.latitude
        elif self.latitude is None and self.lat is not None:
            self.latitude = self.lat
        if self.lng is None and self.longitude is not None:
            self.lng = self.longitude
        elif self.longitude is None and self.lng is not None:
            self.longitude = self.lng
        return self


class KMReimbursement(BaseModel):
    employee_id: str
    organization_id: str
    date: str  # YYYY-MM-DD
    total_km: float
    rate_per_km: float
    total_amount: float
    status: ReimbursementStatus = ReimbursementStatus.PENDING
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    payment_reference: Optional[str] = None


class ExpenseClaim(BaseModel):
    employee_id: str
    organization_id: str
    visit_id: Optional[str] = None
    expense_type: str
    amount: float
    description: str
    receipt_url: str
    claimed_km: Optional[float] = None
    auto_calculated_km: Optional[float] = None
    nights: Optional[int] = None
    accommodation_name: Optional[str] = None
    location_city: Optional[str] = None
    status: ExpenseStatus = ExpenseStatus.PENDING
    manager_query: Optional[str] = None
    employee_response: Optional[str] = None
    resolved_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class LeaveType(str, Enum):
    SICK = "sick"
    CASUAL = "casual"
    ON_DUTY = "on_duty"
    OTHER = "other"
    UNPAID = "unpaid"


class LeaveDuration(str, Enum):
    FULL_DAY = "full_day"
    FIRST_HALF = "first_half"
    SECOND_HALF = "second_half"
    SHORT_LEAVE = "short_leave"


class LeaveStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class DiscussionMessage(BaseModel):
    sender_id: str
    sender_name: str
    role: str  # "admin" or "employee"
    message: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class LeaveRequest(BaseModel):
    employee_id: str
    organization_id: str
    leave_type: LeaveType
    start_date: str  # YYYY-MM-DD
    end_date: str    # YYYY-MM-DD
    reason: str
    duration_type: Optional[str] = "full_day"  # full_day, first_half, second_half, short_leave
    duration: Optional[float] = 1.0  # 1.0 for full day, 0.5 for half day, 0.0 for short leave
    short_leave_hours: Optional[float] = None
    is_paid: Optional[bool] = None  # None = auto-calculated, True = explicitly Paid, False = explicitly LOP
    exceeds_quota: Optional[bool] = False
    admin_notes: Optional[str] = None
    status: LeaveStatus = LeaveStatus.PENDING
    proof_url: Optional[str] = None
    discussion: List[DiscussionMessage] = []
    created_at: datetime = Field(default_factory=datetime.utcnow)
    processed_at: Optional[datetime] = None
    processed_by: Optional[str] = None


class Alert(BaseModel):
    organization_id: str
    employee_id: str
    employee_name: str
    type: AlertCategory
    severity: AlertSeverity
    status: AlertStatus = AlertStatus.PENDING
    detail: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    metadata: Optional[dict] = None  # Lat, Long, accuracy, etc.
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None


class SyncBatchRequest(BaseModel):
    attendance_logs: List[dict] = []
    visits: List[dict] = []
    pings: List[dict] = []

# =========================
# WFH Desktop Monitoring Models
# =========================

class WFHDeviceInfo(BaseModel):
    employee_id: str
    organization_id: str
    device_id: str
    mac_address: Optional[str] = None
    cpu_id: Optional[str] = None
    os_info: Optional[str] = None
    hostname: Optional[str] = None
    ip_local: Optional[str] = None
    ip_public: Optional[str] = None
    ram_gb: Optional[float] = None
    screen_resolution: Optional[str] = None
    monitor_count: Optional[int] = None
    status: str = "pending"  # pending | approved | revoked
    registered_at: datetime = Field(default_factory=datetime.utcnow)
    last_seen: datetime = Field(default_factory=datetime.utcnow)
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    revoked_by: Optional[str] = None
    revoked_at: Optional[datetime] = None
    revoke_reason: Optional[str] = None


class WFHScreenshot(BaseModel):
    employee_id: str
    organization_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    image_url: str
    active_app: Optional[str] = None
    active_window: Optional[str] = None
    thumbnail_url: Optional[str] = None
    flagged: bool = False
    flag_reason: Optional[str] = None


class WFHActivity(BaseModel):
    employee_id: str
    organization_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    period_minutes: int = 5
    keystrokes: int = 0
    mouse_clicks: int = 0
    mouse_distance_px: int = 0
    scroll_events: int = 0
    idle_seconds: int = 0
    active_seconds: int = 0


class WFHAppUsage(BaseModel):
    employee_id: str
    organization_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    date: str
    apps: List[dict] = []


class WFHProductivity(BaseModel):
    employee_id: str
    organization_id: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    date: str
    score: float = 0.0
    breakdown: Optional[dict] = None


class WFHAlert(BaseModel):
    employee_id: str
    organization_id: str
    type: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    image_url: Optional[str] = None
    severity: str = "medium"
    status: str = "pending"
    details: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None


class WFHMeeting(BaseModel):
    employee_id: str
    organization_id: str
    platform: str
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_minutes: Optional[int] = None
    date: str


class HolidayCreateRequest(BaseModel):
    date: str  # YYYY-MM-DD
    name: str
    holiday_type: Optional[str] = "festival"  # national, festival, company, custom
    description: Optional[str] = None


class HolidayBulkCreateRequest(BaseModel):
    holidays: List[HolidayCreateRequest]


class LeavePolicyUpdateRequest(BaseModel):
    year: Optional[int] = None
    allocation_cycle: Optional[str] = "annual"  # "annual" (yearly quota) or "monthly" (monthly quota)
    carry_forward: Optional[bool] = True  # Carry forward unused monthly leaves to next month
    start_month: Optional[str] = "2026-08"  # Base month to start monthly accrual (e.g. 2026-08)
    sick_leave: float = 12.0
    casual_leave: float = 12.0
    total_leave: float = 30.0
    paid_leave_types: Optional[List[str]] = ["sick", "casual", "on_duty"]
    unpaid_leave_types: Optional[List[str]] = ["other", "unpaid"]
    half_day_min_hours: Optional[float] = 4.0
    full_day_min_hours: Optional[float] = 8.0
    late_grace_mins: Optional[int] = 15
    short_leave_monthly_limit: Optional[int] = 2
    short_leave_max_hours: Optional[float] = 2.0
    short_leave_penalty_rule: Optional[str] = "half_day_per_2_excess"
    office_start_time: Optional[str] = "09:00"
    office_end_time: Optional[str] = "18:00"
    short_leave_cutoff_time: Optional[str] = "10:15"
    half_day_late_cutoff_time: Optional[str] = "11:30"
    half_day_early_exit_cutoff_time: Optional[str] = "14:00"
    lunch_start_time: Optional[str] = "13:00"
    lunch_end_time: Optional[str] = "14:00"
    max_lunch_duration_mins: Optional[int] = 60


class EmployeeLeaveQuotaUpdateRequest(BaseModel):
    year: Optional[int] = None
    allocation_cycle: Optional[str] = "annual"  # "annual" or "monthly"
    carry_forward: Optional[bool] = True
    sick_leave: float
    casual_leave: float
    total_leave: float
    paid_leave_types: Optional[List[str]] = None
    unpaid_leave_types: Optional[List[str]] = None