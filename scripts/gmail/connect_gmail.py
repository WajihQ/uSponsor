"""Authorize a Google account for CRM Gmail sync (run once per account).

Prereqs (see SETUP_GMAIL.md): a Google Cloud OAuth *Desktop app* client whose
downloaded JSON is saved next to this file as `client_secret.json`, and the
Gmail API enabled on that project.

    python scripts/gmail/connect_gmail.py   (run from the repo root)

Opens a browser to sign in and consent (read-only header access to your mail).
On success the token is written to gmail_tokens/<your-address>.json. Repeat for
each sending account you want synced (e.g. both Instantly mailboxes plus the
account you send manual outreach from).
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tracker.gmail_sync import SCOPES, _save_new_token


def main():
    secrets = sorted(glob.glob(os.path.join(os.path.dirname(__file__), "client_secret*.json")))
    if not secrets:
        sys.exit("No client_secret*.json found. Download your OAuth Desktop client "
                 "from Google Cloud and save it here as client_secret.json "
                 "(see SETUP_GMAIL.md).")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        sys.exit("Google libraries missing. Run:  pip install -r requirements.txt")

    flow = InstalledAppFlow.from_client_secrets_file(secrets[0], SCOPES)
    print("A browser window will open — sign in with the account you want to sync.")
    creds = flow.run_local_server(port=0)

    address = _save_new_token(creds)
    print(f"\nConnected {address}. Token saved to gmail_tokens/{address}.json")
    print("Run this again for any other sending account, then use the CRM's "
          "'Sync now' / 'Full resync' buttons (or the Settings page's Connect/"
          "Reconnect links, which do this same flow without a terminal).")


if __name__ == "__main__":
    main()
