"""
OCR Document Extraction Utilities.

Tier 2 document verification: Extracts text and structured data
from uploaded document images using OCR.

Primary: Tesseract OCR (free, local)
Fallback: Google Cloud Vision API (paid, more accurate)

Configure via .env:
    OCR_ENGINE=tesseract          # "tesseract" or "google_vision"
    GOOGLE_VISION_API_KEY=...     # Required if OCR_ENGINE=google_vision
"""

import os
import re
import io
import base64
import logging
from typing import Optional, Dict, Any, Tuple

logger = logging.getLogger(__name__)

OCR_ENGINE = os.getenv("OCR_ENGINE", "tesseract")
GOOGLE_VISION_API_KEY = os.getenv("GOOGLE_VISION_API_KEY", "")


# ─── Text Extraction ────────────────────────────────────────────

def extract_text_from_image(image_data: bytes) -> Tuple[str, float]:
    """
    Extract text from an image using configured OCR engine.
    
    Args:
        image_data: Raw image bytes (JPEG/PNG)
    
    Returns:
        Tuple of (extracted_text, confidence_score 0-1)
    """
    if OCR_ENGINE == "google_vision" and GOOGLE_VISION_API_KEY:
        return _extract_with_google_vision(image_data)
    
    # Default: Tesseract
    return _extract_with_tesseract(image_data)


def _extract_with_tesseract(image_data: bytes) -> Tuple[str, float]:
    """Extract text using Tesseract OCR."""
    try:
        from PIL import Image
        import pytesseract

        image = Image.open(io.BytesIO(image_data))
        
        # Pre-process for better OCR accuracy
        image = image.convert("RGB")
        
        # Get detailed OCR data for confidence calculation
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT, lang="eng")
        
        # Calculate average confidence
        confidences = [int(c) for c in data["conf"] if int(c) > 0]
        avg_confidence = (sum(confidences) / len(confidences) / 100) if confidences else 0.0
        
        # Get full text
        text = pytesseract.image_to_string(image, lang="eng")
        
        logger.info(f"[Tesseract] Extracted {len(text)} chars with confidence {avg_confidence:.2f}")
        return text.strip(), avg_confidence

    except ImportError:
        logger.warning("Tesseract/PIL not installed. Falling back to Google Vision.")
        if GOOGLE_VISION_API_KEY:
            return _extract_with_google_vision(image_data)
        return "", 0.0
    except Exception as e:
        logger.error(f"Tesseract OCR failed: {e}")
        # Fallback to Google Vision if available
        if GOOGLE_VISION_API_KEY:
            logger.info("Falling back to Google Vision API")
            return _extract_with_google_vision(image_data)
        return "", 0.0


def _extract_with_google_vision(image_data: bytes) -> Tuple[str, float]:
    """Extract text using Google Cloud Vision API."""
    try:
        import httpx

        image_b64 = base64.b64encode(image_data).decode("utf-8")
        
        url = f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_API_KEY}"
        payload = {
            "requests": [{
                "image": {"content": image_b64},
                "features": [{"type": "TEXT_DETECTION", "maxResults": 1}]
            }]
        }

        # Synchronous call (used within async context via run_in_executor if needed)
        response = httpx.post(url, json=payload, timeout=30)
        
        if response.status_code != 200:
            logger.error(f"Google Vision API error: {response.status_code} - {response.text}")
            return "", 0.0

        result = response.json()
        annotations = result.get("responses", [{}])[0].get("textAnnotations", [])
        
        if not annotations:
            return "", 0.0

        full_text = annotations[0].get("description", "")
        # Google Vision doesn't give a single confidence; use locale confidence as proxy
        confidence = 0.85  # Default high confidence for Google Vision

        logger.info(f"[Google Vision] Extracted {len(full_text)} chars")
        return full_text.strip(), confidence

    except ImportError:
        logger.error("httpx not installed for Google Vision API")
        return "", 0.0
    except Exception as e:
        logger.error(f"Google Vision API failed: {e}")
        return "", 0.0


# ─── Document-Specific Extractors ────────────────────────────────

def extract_aadhaar_data(image_data: bytes) -> Dict[str, Any]:
    """
    Extract structured data from an Aadhaar card image.
    
    Returns dict with: name, dob, gender, aadhaar_number, address
    """
    text, confidence = extract_text_from_image(image_data)
    if not text:
        return {"success": False, "error": "No text extracted", "confidence": 0}

    data = {"raw_text": text, "confidence": confidence, "success": True}

    # Extract Aadhaar number (12 digits, usually with spaces: XXXX XXXX XXXX)
    aadhaar_pattern = r'\b(\d{4}\s?\d{4}\s?\d{4})\b'
    aadhaar_matches = re.findall(aadhaar_pattern, text)
    if aadhaar_matches:
        number = aadhaar_matches[-1].replace(" ", "")
        if validate_aadhaar_number(number):
            # Mask for security
            data["aadhaar_number"] = f"XXXX-XXXX-{number[-4:]}"
            data["aadhaar_valid"] = True
        else:
            data["aadhaar_number"] = f"XXXX-XXXX-{number[-4:]}"
            data["aadhaar_valid"] = False

    # Extract DOB (DD/MM/YYYY or DD-MM-YYYY)
    dob_pattern = r'\b(\d{2}[/-]\d{2}[/-]\d{4})\b'
    dob_matches = re.findall(dob_pattern, text)
    if dob_matches:
        data["dob"] = dob_matches[0]

    # Extract gender
    gender_pattern = r'\b(MALE|FEMALE|TRANSGENDER|पुरुष|महिला)\b'
    gender_match = re.search(gender_pattern, text, re.IGNORECASE)
    if gender_match:
        g = gender_match.group().upper()
        data["gender"] = "M" if g in ["MALE", "पुरुष"] else "F" if g in ["FEMALE", "महिला"] else "T"

    # Extract name (typically after "Name:" or first line of text)
    name_pattern = r'(?:Name|नाम)\s*[:\-]?\s*([A-Za-z\s]+)'
    name_match = re.search(name_pattern, text, re.IGNORECASE)
    if name_match:
        data["name"] = name_match.group(1).strip()

    return data


def extract_pan_data(image_data: bytes) -> Dict[str, Any]:
    """
    Extract structured data from a PAN card image.
    
    Returns dict with: name, pan_number, dob, father_name
    """
    text, confidence = extract_text_from_image(image_data)
    if not text:
        return {"success": False, "error": "No text extracted", "confidence": 0}

    data = {"raw_text": text, "confidence": confidence, "success": True}

    # Extract PAN number (AAAAA9999A format)
    pan_pattern = r'\b([A-Z]{5}\d{4}[A-Z])\b'
    pan_match = re.search(pan_pattern, text.upper())
    if pan_match:
        pan = pan_match.group(1)
        data["pan_number"] = pan
        data["pan_valid"] = validate_pan_number(pan)

    # Extract DOB
    dob_pattern = r'\b(\d{2}[/-]\d{2}[/-]\d{4})\b'
    dob_matches = re.findall(dob_pattern, text)
    if dob_matches:
        data["dob"] = dob_matches[0]

    # Extract name lines (PAN cards have specific layout)
    lines = [l.strip() for l in text.split("\n") if l.strip() and len(l.strip()) > 2]
    # Name is usually the second or third line
    if len(lines) >= 3:
        data["name"] = lines[1] if not re.match(r'^\d', lines[1]) else lines[2]
    if len(lines) >= 4:
        data["father_name"] = lines[2] if "name" in data and data.get("name") == lines[1] else None

    return data


def extract_bank_details(image_data: bytes) -> Dict[str, Any]:
    """
    Extract structured data from a bank passbook/statement image.
    
    Returns dict with: account_number, ifsc_code, bank_name, holder_name
    """
    text, confidence = extract_text_from_image(image_data)
    if not text:
        return {"success": False, "error": "No text extracted", "confidence": 0}

    data = {"raw_text": text, "confidence": confidence, "success": True}

    # Extract IFSC code (4 letters + 0 + 6 alphanumeric)
    ifsc_pattern = r'\b([A-Z]{4}0[A-Z0-9]{6})\b'
    ifsc_match = re.search(ifsc_pattern, text.upper())
    if ifsc_match:
        data["ifsc_code"] = ifsc_match.group(1)

    # Extract account number (8-18 digits)
    account_pattern = r'\b(\d{8,18})\b'
    account_matches = re.findall(account_pattern, text)
    # Filter out dates and other numeric patterns
    for num in account_matches:
        if len(num) >= 9 and not num.startswith("20"):  # Skip dates
            data["account_number"] = num
            break

    # Try to extract bank name from common bank names
    bank_names = [
        "STATE BANK", "SBI", "HDFC", "ICICI", "AXIS", "KOTAK", "PNB",
        "BOB", "CANARA", "UNION BANK", "BANK OF BARODA", "IDBI", "YES BANK",
        "INDIAN BANK", "CENTRAL BANK", "BANK OF INDIA", "INDUSIND"
    ]
    text_upper = text.upper()
    for bank in bank_names:
        if bank in text_upper:
            data["bank_name"] = bank
            break

    return data


def extract_generic_document(image_data: bytes) -> Dict[str, Any]:
    """
    Extract text from any document without specific field parsing.
    Used for experience letters, offer letters, etc.
    """
    text, confidence = extract_text_from_image(image_data)
    return {
        "success": bool(text),
        "raw_text": text,
        "confidence": confidence,
        "word_count": len(text.split()) if text else 0,
        "line_count": len(text.split("\n")) if text else 0,
    }


# ─── Validation Utilities ────────────────────────────────────────

def validate_aadhaar_number(number: str) -> bool:
    """
    Validate Aadhaar number using Verhoeff checksum algorithm.
    Aadhaar must be 12 digits, not start with 0 or 1.
    """
    number = number.replace(" ", "").replace("-", "")
    if len(number) != 12 or not number.isdigit():
        return False
    if number[0] in ("0", "1"):
        return False

    # Verhoeff algorithm tables
    d = [
        [0,1,2,3,4,5,6,7,8,9], [1,2,3,4,0,6,7,8,9,5],
        [2,3,4,0,1,7,8,9,5,6], [3,4,0,1,2,8,9,5,6,7],
        [4,0,1,2,3,9,5,6,7,8], [5,9,8,7,6,0,4,3,2,1],
        [6,5,9,8,7,1,0,4,3,2], [7,6,5,9,8,2,1,0,4,3],
        [8,7,6,5,9,3,2,1,0,4], [9,8,7,6,5,4,3,2,1,0]
    ]
    p = [
        [0,1,2,3,4,5,6,7,8,9], [1,5,7,6,2,8,3,0,9,4],
        [5,8,0,3,7,9,6,1,4,2], [8,9,1,6,0,4,3,5,2,7],
        [9,4,5,3,1,2,6,8,7,0], [4,2,8,6,5,7,3,9,0,1],
        [2,7,9,3,8,0,6,4,1,5], [7,0,4,6,9,1,3,2,5,8]
    ]
    inv = [0,4,3,2,1,5,6,7,8,9]

    c = 0
    reversed_number = number[::-1]
    for i, digit in enumerate(reversed_number):
        c = d[c][p[i % 8][int(digit)]]

    return c == 0


def validate_pan_number(pan: str) -> bool:
    """
    Validate PAN number format.
    Format: AAAAA9999A (5 letters, 4 digits, 1 letter)
    4th character indicates entity type: P=Person, C=Company, etc.
    """
    pan = pan.upper().strip()
    if len(pan) != 10:
        return False
    return bool(re.match(r'^[A-Z]{5}\d{4}[A-Z]$', pan))


def validate_ifsc_code(ifsc: str) -> bool:
    """Validate IFSC code format: 4 letters + 0 + 6 alphanumeric."""
    ifsc = ifsc.upper().strip()
    return bool(re.match(r'^[A-Z]{4}0[A-Z0-9]{6}$', ifsc))


# ─── Dispatcher ──────────────────────────────────────────────────

DOCUMENT_EXTRACTORS = {
    "aadhaar": extract_aadhaar_data,
    "pan": extract_pan_data,
    "bank_passbook": extract_bank_details,
    "salary_slip": extract_generic_document,
    "experience_letter": extract_generic_document,
    "relieving_letter": extract_generic_document,
    "offer_letter": extract_generic_document,
    "other": extract_generic_document,
}


def process_document_ocr(image_data: bytes, doc_type: str) -> Dict[str, Any]:
    """
    Process a document image through OCR and extract structured data.
    
    Args:
        image_data: Raw image bytes
        doc_type: Document type (aadhaar, pan, bank_passbook, etc.)
    
    Returns:
        Dict with extracted fields, confidence score, and validation results
    """
    extractor = DOCUMENT_EXTRACTORS.get(doc_type, extract_generic_document)
    result = extractor(image_data)
    result["doc_type"] = doc_type
    result["ocr_engine"] = OCR_ENGINE
    result["verification_source"] = "ocr"
    return result


def image_from_base64(b64_string: str) -> bytes:
    """Convert base64 image string to bytes, stripping data URI prefix if present."""
    if "," in b64_string:
        b64_string = b64_string.split(",", 1)[1]
    return base64.b64decode(b64_string)
