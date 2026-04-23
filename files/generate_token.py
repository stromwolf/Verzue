"""
generate_token.py
Phase 1: OAuth token stored via SecretStore (encrypted), never pickle.
         Run once on a machine with a browser to generate the initial token.
         On headless VPS: run locally, then copy data/secrets/.vault* to server.
"""

import json
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from app.security.secret_store import SecretStore

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_FILE = BASE_DIR / "data" / "secrets" / "credentials.json"
SCOPES = ['https://www.googleapis.com/auth/drive']


def main():
    print("=" * 50)
    print("      GOOGLE DRIVE TOKEN GENERATOR")
    print("=" * 50)

    if not CREDENTIALS_FILE.exists():
        print(f"❌ ERROR: credentials.json not found at:\n   {CREDENTIALS_FILE}")
        print("\nDownload it from Google Cloud Console and place it in data/secrets/.")
        return

    # Warn if old vault token exists
    existing = SecretStore.get("gdrive", "token")
    if existing:
        confirm = input("⚠️  A token already exists in the vault. Overwrite? [y/N]: ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return

    # Also clean up legacy pickle if it somehow still exists
    pickle_path = BASE_DIR / "data" / "secrets" / "token.pickle"
    if pickle_path.exists():
        pickle_path.unlink()
        print(f"🗑️  Deleted legacy token.pickle")

    print("🚀 Starting OAuth 2.0 flow...")
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)

        SecretStore.put("gdrive", "token", creds.to_json())
        print("\n✅ Token saved to SecretStore (encrypted vault).")
        print("Restart the bot — GDriveClient will load from the vault automatically.")

    except Exception as e:
        print(f"\n❌ FAILED: {e}")


if __name__ == "__main__":
    main()
