from pydantic import BaseModel, Field
from typing import List, Optional, Any
from datetime import datetime
from enum import Enum

# --- Onboarding Models ---

class VerificationSource(str, Enum):
    DIGILOCKER = "digilocker"       # Tier 1: Government-verified
    OCR = "ocr"                     # Tier 2: OCR-extracted
    MANUAL = "manual"               # Tier 3: Manual upload

class DocumentType(str, Enum):
    AADHAAR = "aadhaar"
    PAN = "pan"
    DRIVING_LICENSE = "driving_license"
    VOTER_ID = "voter_id"
    PASSPORT = "passport"
    CLASS_10 = "class_10_marksheet"
    CLASS_12 = "class_12_marksheet"
    DEGREE = "degree_certificate"
    BANK_PASSBOOK = "bank_passbook"
    SALARY_SLIP = "salary_slip"
    EXPERIENCE_LETTER = "experience_letter"
    RELIEVING_LETTER = "relieving_letter"
    OFFER_LETTER = "offer_letter"
    PHOTO = "photo"
    OTHER = "other"

class OnboardingDocument(BaseModel):
    doc_type: DocumentType
    file_url: Optional[str] = None
    status: str = "pending"  # pending, verified, rejected, digilocker_verified
    verification_source: VerificationSource = VerificationSource.MANUAL
    digilocker_uri: Optional[str] = None       # DigiLocker document URI
    digilocker_data: Optional[dict] = None     # Parsed data from DigiLocker XML
    ocr_data: Optional[dict] = None            # OCR-extracted fields
    ocr_confidence: Optional[float] = None     # OCR confidence score (0-1)
    verified_by: Optional[str] = None
    verified_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    is_government_verified: bool = False        # True for DigiLocker docs



class OnboardingStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

class OnboardingTaskStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    OVERDUE = "overdue"

class OnboardingTask(BaseModel):
    task_id: str
    title: str
    category: str  # "HR", "IT", "Team", "Compliance"
    assigned_to: Optional[str] = None
    due_date: Optional[str] = None  # YYYY-MM-DD
    status: OnboardingTaskStatus = OnboardingTaskStatus.PENDING
    completed_at: Optional[datetime] = None
    completed_by: Optional[str] = None
    notes: Optional[str] = None

class OnboardingCreate(BaseModel):
    employee_email: str
    employee_name: str
    department: str
    designation: str
    start_date: str  # YYYY-MM-DD
    expected_completion_date: Optional[str] = None  # YYYY-MM-DD
    assigned_to: str  # HR Manager email
    buddy: Optional[str] = None
    tasks: List[OnboardingTask] = []
    documents_required: List[str] = ["pan_card", "aadhaar", "bank_details", "offer_letter", "photo"]
    notes: Optional[str] = None

# --- Payroll Models ---

class PayrollStatus(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    LOCKED = "locked"
    PAID = "paid"
    CANCELLED = "cancelled"

class PayrollCreate(BaseModel):
    employee_email: str
    payroll_month: str  # "YYYY-MM"
    working_days: Optional[int] = None
    present_days: Optional[int] = None
    leave_days: Optional[int] = None
    absent_days: Optional[int] = None
    overtime_hours: float = 0.0
    bonus: float = 0.0
    other_earnings: float = 0.0
    loan_deductions: float = 0.0
    advance_deductions: float = 0.0
    other_deductions: float = 0.0

class PayrollRunRequest(BaseModel):
    payroll_month: str  # "YYYY-MM"
    department: Optional[str] = None
    employee_emails: Optional[List[str]] = None
    auto_approve: bool = False

class DaysOffSaveRequest(BaseModel):
    payroll_month: str  # "YYYY-MM"
    days_off: List[int]
    organization_id: Optional[str] = None

# --- Exit Management Models ---

class ExitStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"

class ClearanceStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"

class ExitCreate(BaseModel):
    employee_email: str
    employee_name: Optional[str] = None
    department: Optional[str] = None
    designation: Optional[str] = None
    exit_reason: str  # "resignation", "termination", "retirement", "contract_end"
    resignation_date: str  # YYYY-MM-DD
    last_working_day: str  # YYYY-MM-DD
    notice_period_days: int = 30
    assigned_to: str  # HR Manager email
    exit_interviewer: Optional[str] = None
    notes: Optional[str] = None

class ClearanceUpdate(BaseModel):
    department: str  # "hr", "it", "finance", "assets"
    status: ClearanceStatus
    notes: Optional[str] = None
    cleared_by: Optional[str] = None
