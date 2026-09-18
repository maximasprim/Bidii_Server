"""
Standalone diagnostic for the Google Sheets export 403 error.

Run this from inside bidii-credit-backend, with the same venv active:

    cd bidii-credit-backend
    .venv\\Scripts\\activate
    python check_google_sheets_config.py

It loads GOOGLE_SERVICE_ACCOUNT_JSON exactly the way the real app does,
prints which service account + project are ACTUALLY being used (not what
you assume is being used), and then makes the real spreadsheets.create()
call directly - so you see Google's raw response with nothing in between.

This file is not part of the app - delete it once you're done debugging.
"""
import json
import sys
from pathlib import Path


def main():
    try:
        from app.config import get_settings
    except ImportError:
        print("Run this from inside the bidii-credit-backend folder (where 'app/' lives).")
        sys.exit(1)

    raw = get_settings().google_service_account_json
    if not raw:
        print("GOOGLE_SERVICE_ACCOUNT_JSON is not set in the .env this process can see.")
        print("(If you just edited .env, this confirms the process needs a restart.)")
        sys.exit(1)

    candidate_path = Path(raw)
    if candidate_path.exists():
        print(f"Loaded key from file path: {candidate_path.resolve()}")
        info = json.loads(candidate_path.read_text())
    else:
        print("Loaded key from inline JSON (not a file path).")
        info = json.loads(raw)

    print()
    print("=== Identity actually being used right now ===")
    print(f"  project_id    : {info.get('project_id')}")
    print(f"  client_email  : {info.get('client_email')}")
    print(f"  private_key_id: {info.get('private_key_id')}")
    print()
    print("Cross-check these against the exact project you enabled")
    print("Sheets API + Drive API in (top-left project switcher in Cloud Console),")
    print("and against the service account shown under IAM & Admin > Service Accounts.")
    print()

    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError

    credentials = service_account.Credentials.from_service_account_info(
        info,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive.file",
        ],
    )
    sheets = build("sheets", "v4", credentials=credentials, cache_discovery=False)

    print("=== Attempting spreadsheets.create() directly ===")
    try:
        result = (
            sheets.spreadsheets()
            .create(
                body={"properties": {"title": "Bidii diagnostic test - safe to delete"}},
                fields="spreadsheetId,spreadsheetUrl",
            )
            .execute()
        )
        print("SUCCESS:", result["spreadsheetUrl"])
        print()
        print("Google Sheets export is working. The earlier failures were from a")
        print("stale process still using an old key/config - restarting uvicorn fixed it.")
    except HttpError as exc:
        print(f"FAILED: HTTP {exc.resp.status}")
        print(exc.content.decode("utf-8", errors="replace"))
        print()
        print("This is Google's raw response for the exact identity printed above.")
        print("If project_id/client_email above don't match what you configured in")
        print("Cloud Console, that mismatch is the bug. If they DO match and this")
        print("still fails, the full JSON body above usually names the real reason")
        print("(e.g. billing not linked, an org policy, or a propagation delay).")


if __name__ == "__main__":
    main()