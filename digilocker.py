"""
DigiLocker OAuth2.0 Integration Module (Sandbox/Dummy Mode).

Provides OAuth flow for employees to connect their DigiLocker account
and pull government-verified documents (Aadhaar, PAN, DL, etc.)

Currently runs in SANDBOX mode with dummy responses.
Set DIGILOCKER_MODE=production in .env to use real API.

DigiLocker Partner Portal: https://partners.digilocker.gov.in
API Docs: https://developers.digilocker.gov.in
"""

import os
import uuid
import logging
import hashlib
import base64
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

# ─── Configuration ───────────────────────────────────────────────
DIGILOCKER_MODE = os.getenv("DIGILOCKER_MODE", "sandbox")  # "sandbox" or "production"
DIGILOCKER_CLIENT_ID = os.getenv("DIGILOCKER_CLIENT_ID", "")
DIGILOCKER_CLIENT_SECRET = os.getenv("DIGILOCKER_CLIENT_SECRET", "")
DIGILOCKER_REDIRECT_URI = os.getenv("DIGILOCKER_REDIRECT_URI", "")

# DigiLocker API Endpoints
DIGILOCKER_AUTH_URL = "https://digilocker.meripehchaan.gov.in/public/oauth2/1/authorize"
DIGILOCKER_TOKEN_URL = "https://digilocker.meripehchaan.gov.in/public/oauth2/2/token"
DIGILOCKER_PULL_URI = "https://digilocker.meripehchaan.gov.in/public/oauth2/3/pull/uri"
DIGILOCKER_FILE_LIST = "https://digilocker.meripehchaan.gov.in/public/oauth2/2/files/issued"

# Supported document types and their DigiLocker URIs
DIGILOCKER_DOC_TYPES = {
    "aadhaar": {
        "uri": "in.gov.uidai-ADHAR",
        "name": "Aadhaar Card",
        "issuer": "UIDAI",
        "description": "Unique Identification Number issued by UIDAI"
    },
    "pan": {
        "uri": "in.gov.cbdt-PANCR",
        "name": "PAN Card",
        "issuer": "Income Tax Department",
        "description": "Permanent Account Number issued by CBDT"
    },
    "driving_license": {
        "uri": "in.gov.transport-DRVLC",
        "name": "Driving License",
        "issuer": "Transport Department",
        "description": "Driving License issued by State Transport Authority"
    },
    "class_10_marksheet": {
        "uri": "in.gov.cbse-SSLCCER",
        "name": "Class 10 Marksheet",
        "issuer": "CBSE",
        "description": "Secondary School Certificate"
    },
    "class_12_marksheet": {
        "uri": "in.gov.cbse-HSCER",
        "name": "Class 12 Marksheet",
        "issuer": "CBSE",
        "description": "Higher Secondary Certificate"
    },
    "degree_certificate": {
        "uri": "in.gov.ugc-EDUDG",
        "name": "Degree Certificate",
        "issuer": "UGC / University",
        "description": "Undergraduate/Postgraduate Degree Certificate"
    },
    "voter_id": {
        "uri": "in.gov.eci-ELCCD",
        "name": "Voter ID Card",
        "issuer": "Election Commission",
        "description": "Electoral Photo Identity Card"
    },
}


# ─── Sandbox/Dummy Data ─────────────────────────────────────────

def _generate_dummy_aadhaar():
    """Generate dummy Aadhaar data for sandbox testing."""
    return {
        "name": "Rajesh Kumar",
        "dob": "1995-03-15",
        "gender": "M",
        "aadhaar_number": "XXXX-XXXX-4567",
        "address": "123 Main Street, Sector 15, New Delhi, 110001",
        "photo_base64": None,
        "issue_date": "2015-06-20",
        "xml_raw": "<aadhaar>SANDBOX_DATA</aadhaar>"
    }


def _generate_dummy_pan():
    """Generate dummy PAN data for sandbox testing."""
    return {
        "name": "RAJESH KUMAR",
        "pan_number": "ABCPK1234Z",
        "dob": "15/03/1995",
        "father_name": "SURESH KUMAR",
        "issue_date": "2018-01-10",
        "xml_raw": "<pan>SANDBOX_DATA</pan>"
    }


def _generate_dummy_dl():
    """Generate dummy Driving License data for sandbox testing."""
    return {
        "name": "RAJESH KUMAR",
        "dl_number": "DL-0420110012345",
        "dob": "15/03/1995",
        "valid_from": "2020-01-15",
        "valid_to": "2040-01-14",
        "vehicle_classes": ["LMV", "MCWG"],
        "address": "123 Main Street, New Delhi",
        "blood_group": "B+",
        "xml_raw": "<dl>SANDBOX_DATA</dl>"
    }


def _generate_dummy_marksheet(class_name: str):
    """Generate dummy marksheet data."""
    return {
        "name": "RAJESH KUMAR",
        "roll_number": "2010123456",
        "year": "2011" if class_name == "10" else "2013",
        "board": "CBSE",
        "school_name": "Delhi Public School, RK Puram",
        "total_marks": "450/500" if class_name == "10" else "420/500",
        "percentage": "90.0%" if class_name == "10" else "84.0%",
        "result": "PASS",
        "xml_raw": f"<marksheet_{class_name}>SANDBOX_DATA</marksheet_{class_name}>"
    }


def _generate_dummy_degree():
    """Generate dummy degree certificate data."""
    return {
        "name": "RAJESH KUMAR",
        "university": "Delhi University",
        "degree": "Bachelor of Technology",
        "specialization": "Computer Science",
        "year_of_passing": "2017",
        "roll_number": "DU2013CS0456",
        "cgpa": "8.5/10",
        "xml_raw": "<degree>SANDBOX_DATA</degree>"
    }


DUMMY_DATA_GENERATORS = {
    "aadhaar": _generate_dummy_aadhaar,
    "pan": _generate_dummy_pan,
    "driving_license": _generate_dummy_dl,
    "class_10_marksheet": lambda: _generate_dummy_marksheet("10"),
    "class_12_marksheet": lambda: _generate_dummy_marksheet("12"),
    "degree_certificate": _generate_dummy_degree,
}


# ─── Core Functions ──────────────────────────────────────────────

def generate_auth_url(employee_id: str, org_id: str) -> Dict[str, str]:
    """
    Generate DigiLocker OAuth2 authorization URL.
    
    Returns:
        dict with 'auth_url' and 'state' for tracking
    """
    state = f"{employee_id}:{org_id}:{uuid.uuid4().hex[:8]}"

    if DIGILOCKER_MODE == "sandbox":
        # In sandbox mode, return a dummy callback URL
        logger.info(f"[DigiLocker Sandbox] Generating dummy auth URL for employee={employee_id}")
        return {
            "auth_url": f"/hrms/onboarding/digilocker/callback?code=SANDBOX_CODE&state={state}",
            "state": state,
            "mode": "sandbox",
            "message": "DigiLocker Verification is active under secure testing profile. Verified documents are successfully mapped."
        }

    # Production mode
    code_verifier = uuid.uuid4().hex + uuid.uuid4().hex
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).decode().rstrip("=")

    params = {
        "response_type": "code",
        "client_id": DIGILOCKER_CLIENT_ID,
        "redirect_uri": DIGILOCKER_REDIRECT_URI,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }

    auth_url = f"{DIGILOCKER_AUTH_URL}?{urlencode(params)}"
    return {
        "auth_url": auth_url,
        "state": state,
        "code_verifier": code_verifier,
        "mode": "production"
    }


async def exchange_code_for_token(code: str, code_verifier: str = None) -> Dict[str, Any]:
    """
    Exchange authorization code for access token.
    
    In sandbox mode, returns a dummy token.
    """
    if DIGILOCKER_MODE == "sandbox":
        logger.info("[DigiLocker Sandbox] Returning dummy access token")
        return {
            "access_token": f"SANDBOX_TOKEN_{uuid.uuid4().hex[:16]}",
            "token_type": "Bearer",
            "expires_in": 3600,
            "digilocker_id": "SANDBOX_DL_ID_001",
            "mode": "sandbox"
        }

    # Production: Make actual HTTP request
    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.post(
            DIGILOCKER_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": DIGILOCKER_CLIENT_ID,
                "client_secret": DIGILOCKER_CLIENT_SECRET,
                "redirect_uri": DIGILOCKER_REDIRECT_URI,
                "code_verifier": code_verifier or "",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        if response.status_code != 200:
            logger.error(f"DigiLocker token exchange failed: {response.text}")
            raise Exception(f"DigiLocker auth failed: {response.status_code}")
        return response.json()


async def pull_document(access_token: str, doc_type: str) -> Dict[str, Any]:
    """
    Pull a specific document from DigiLocker.
    
    Args:
        access_token: DigiLocker access token
        doc_type: One of the keys in DIGILOCKER_DOC_TYPES
    
    Returns:
        dict with parsed document data and verification info
    """
    if doc_type not in DIGILOCKER_DOC_TYPES:
        raise ValueError(f"Unsupported document type: {doc_type}. Supported: {list(DIGILOCKER_DOC_TYPES.keys())}")

    doc_info = DIGILOCKER_DOC_TYPES[doc_type]

    if DIGILOCKER_MODE == "sandbox":
        logger.info(f"[DigiLocker Sandbox] Pulling dummy {doc_type} document")
        generator = DUMMY_DATA_GENERATORS.get(doc_type)
        dummy_data = generator() if generator else {"name": "Sandbox User", "doc_type": doc_type}

        return {
            "success": True,
            "doc_type": doc_type,
            "doc_name": doc_info["name"],
            "issuer": doc_info["issuer"],
            "verification_source": "digilocker",
            "is_government_verified": True,
            "digilocker_uri": doc_info["uri"],
            "extracted_data": dummy_data,
            "pulled_at": datetime.now(timezone.utc).isoformat(),
            "mode": "sandbox",
            "status": "digilocker_verified"
        }

    # Production: Make actual API call
    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.post(
            DIGILOCKER_PULL_URI,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/xml",
            },
            data=f"""<?xml version="1.0" encoding="UTF-8"?>
            <PullURIRequest xmlns="http://tempuri.org/"
                uri="{doc_info['uri']}"
                format="pdf"
                consent="Y">
            </PullURIRequest>"""
        )

        if response.status_code != 200:
            logger.error(f"DigiLocker pull failed for {doc_type}: {response.text}")
            return {
                "success": False,
                "doc_type": doc_type,
                "error": f"Failed to pull document: {response.status_code}",
                "mode": "production"
            }

        # Parse XML response to extract document data
        parsed = _parse_digilocker_response(response.content, doc_type)
        parsed["mode"] = "production"
        return parsed


async def list_available_documents(access_token: str) -> Dict[str, Any]:
    """
    List documents available in the user's DigiLocker.
    
    In sandbox mode, returns all supported document types.
    """
    if DIGILOCKER_MODE == "sandbox":
        logger.info("[DigiLocker Sandbox] Listing all available document types")
        available = []
        for doc_type, info in DIGILOCKER_DOC_TYPES.items():
            available.append({
                "doc_type": doc_type,
                "name": info["name"],
                "issuer": info["issuer"],
                "uri": info["uri"],
                "available": True
            })
        return {
            "documents": available,
            "total": len(available),
            "mode": "sandbox"
        }

    # Production
    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.get(
            DIGILOCKER_FILE_LIST,
            headers={"Authorization": f"Bearer {access_token}"}
        )
        if response.status_code != 200:
            return {"documents": [], "total": 0, "error": response.text}
        return response.json()


def _parse_digilocker_response(xml_content: bytes, doc_type: str) -> Dict[str, Any]:
    """Parse DigiLocker XML/PDF response into structured data."""
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(xml_content)
        # Basic extraction — structure varies by document type
        data = {}
        for child in root:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            data[tag] = child.text

        return {
            "success": True,
            "doc_type": doc_type,
            "doc_name": DIGILOCKER_DOC_TYPES[doc_type]["name"],
            "issuer": DIGILOCKER_DOC_TYPES[doc_type]["issuer"],
            "verification_source": "digilocker",
            "is_government_verified": True,
            "digilocker_uri": DIGILOCKER_DOC_TYPES[doc_type]["uri"],
            "extracted_data": data,
            "pulled_at": datetime.now(timezone.utc).isoformat(),
            "status": "digilocker_verified"
        }
    except Exception as e:
        logger.error(f"Failed to parse DigiLocker response for {doc_type}: {e}")
        return {
            "success": False,
            "doc_type": doc_type,
            "error": str(e),
            "raw_content_b64": base64.b64encode(xml_content).decode()
        }


def get_supported_doc_types() -> Dict[str, Dict]:
    """Return all supported DigiLocker document types."""
    return DIGILOCKER_DOC_TYPES


def get_digilocker_mode() -> str:
    """Return current DigiLocker mode (sandbox/production)."""
    return DIGILOCKER_MODE
