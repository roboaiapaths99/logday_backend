from pydantic import BaseModel, Field
from typing import List, Optional, Any
from datetime import datetime
from enum import Enum


class EmployeeType(str, Enum):
    DESK = "desk"
    FIELD = "field"
    OFFICE = "office"


class TerritoryType(str, Enum):
    RADIUS = "radius"
    POLYGON = "polygon"


class AttendanceType(str, Enum):
    OFFICE = "office"
    REMOTE_FIELD = "remote_field"


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


class EmployeeBase(BaseModel):
    full_name: str
    email: str
    employee_id: str
    designation: str = "Employee"
    department: str = "General"
    organization_id: str
    employee_type: EmployeeType = EmployeeType.DESK
    manager_id: Optional[str] = None
    beat_zone_name: Optional[str] = None


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


class LoginRequest(BaseModel):
    email: str
    password: str
    device_id: Optional[str] = None
    organization_id: Optional[str] = None


class VerifyPresenceRequest(BaseModel):
    email: str
    image: str
    lat: float
    long: float
    accuracy: Optional[float] = None
    wifi_ssid: str = ""
    wifi_bssid: str = ""
    wifi_strength: float = -50.0
    address: Optional[str] = None
    intended_type: Optional[str] = None
    device_id: Optional[str] = None
    otp_used: bool = False
    mock_detected: bool = False


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


class EmployeeUpdate(BaseModel):
    full_name: Optional[str] = None
    designation: Optional[str] = None
    department: Optional[str] = None
    status: Optional[str] = None
    employee_type: Optional[EmployeeType] = None
    manager_id: Optional[str] = None
    territory_type: Optional[TerritoryType] = None
    territory_center_lat: Optional[float] = None
    territory_center_lng: Optional[float] = None
    territory_radius_meters: Optional[float] = None
    territory_polygon: Optional[List[dict]] = None


class SystemSettings(BaseModel):
    office_start_time: str = "09:00"
    late_threshold_mins: int = 15
    required_hours: float = 8.0
    
    # Field specific defaults
    field_office_start_time: str = "10:00"
    field_late_threshold_mins: int = 30
    field_required_hours: float = 9.0
    field_visits_goal: int = 10
    field_km_goal: float = 20.0
    field_rate_per_km: float = 10.0
    
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
    lat: float
    lng: float
    accuracy: float
    recorded_at: datetime = Field(default_factory=datetime.utcnow)
    source: PingSource = PingSource.AUTO
    offline_id: Optional[str] = None
    synced_at: Optional[datetime] = None


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

