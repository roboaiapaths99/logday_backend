import gspread
import os
import logging
import asyncio
from datetime import datetime

logger = logging.getLogger(__name__)

# Scopes required for Google Sheets and Drive
SCOPES = [
    'https://www.googleapis.com/auth/spreadsheets',
    'https://www.googleapis.com/auth/drive'
]

def _sync_to_google_sheets_worker(log_entry: dict):
    """Synchronous worker running in a threadpool to prevent blocking the event loop."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    creds_path = os.getenv("SERVICE_ACCOUNT_JSON_PATH")

    if not sheet_id or not creds_path or not os.path.exists(creds_path):
        logger.warning("Google Sheets sync skipped: GOOGLE_SHEET_ID or SERVICE_ACCOUNT_JSON_PATH not configured.")
        return

    try:
        client = gspread.service_account(filename=creds_path, scopes=SCOPES)
        
        # Open the spreadsheet and the first worksheet
        sh = client.open_by_key(sheet_id)
        worksheet = sh.get_worksheet(0)

        # PrepareRow: [Timestamp, Email, Type, Method, Lat, Lng, Status]
        row = [
            log_entry.get("timestamp").isoformat() if isinstance(log_entry.get("timestamp"), datetime) else str(log_entry.get("timestamp")),
            log_entry.get("email"),
            log_entry.get("type"),
            log_entry.get("check_in_method"),
            log_entry.get("location", {}).get("lat"),
            log_entry.get("location", {}).get("long"),
            log_entry.get("status")
        ]

        # Append row
        worksheet.append_row(row)
        logger.info(f"Successfully synced log for {log_entry.get('email')} to Google Sheets.")

    except Exception as e:
        logger.error(f"Google Sheets synchronization failed: {e}")


async def sync_to_google_sheets(log_entry: dict):
    """
    Syncs a single attendance log entry to a designated Google Sheet.
    Dispatches blocking I/O to a worker thread.
    """
    await asyncio.to_thread(_sync_to_google_sheets_worker, log_entry)


def _sync_visit_to_google_sheets_worker(visit_entry: dict):
    """Synchronous worker running in a threadpool to prevent blocking the event loop."""
    sheet_id = os.getenv("GOOGLE_SHEET_ID")
    creds_path = os.getenv("SERVICE_ACCOUNT_JSON_PATH")

    if not sheet_id or not creds_path or not os.path.exists(creds_path):
        logger.warning("Visit Google Sheets sync skipped: credentials not configured.")
        return

    try:
        gc = gspread.service_account(filename=creds_path, scopes=SCOPES)

        sh = gc.open_by_key(sheet_id)

        # Use "Visits" worksheet; create it if it doesn't exist
        try:
            worksheet = sh.worksheet("Visits")
        except gspread.exceptions.WorksheetNotFound:
            worksheet = sh.add_worksheet(title="Visits", rows=1000, cols=10)
            worksheet.append_row([
                "Date", "Employee", "Place", "Check-In", "Check-Out",
                "Person Met", "Remarks", "Outcome", "Geofence OK", "Face Verified"
            ])

        row = [
            visit_entry.get("date", ""),
            visit_entry.get("employee_id", ""),
            visit_entry.get("place_name", ""),
            visit_entry.get("check_in_time").isoformat() if isinstance(visit_entry.get("check_in_time"), datetime) else str(visit_entry.get("check_in_time", "")),
            visit_entry.get("check_out_time").isoformat() if isinstance(visit_entry.get("check_out_time"), datetime) else str(visit_entry.get("check_out_time", "")),
            visit_entry.get("person_met", ""),
            visit_entry.get("remarks", ""),
            visit_entry.get("outcome", ""),
            str(visit_entry.get("geofence_validated", False)),
            str(visit_entry.get("face_verified", False))
        ]

        worksheet.append_row(row)
        logger.info(f"Visit sync: {visit_entry.get('employee_id')} at {visit_entry.get('place_name')}")

    except Exception as e:
        logger.error(f"Visit Google Sheets sync failed: {e}")


async def sync_visit_to_google_sheets(visit_entry: dict):
    """
    Syncs a completed visit record to a 'Visits' worksheet in the Google Sheet.
    Dispatches blocking I/O to a worker thread.
    """
    await asyncio.to_thread(_sync_visit_to_google_sheets_worker, visit_entry)
