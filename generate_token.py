import os
import pickle
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow

# --- CONFIGURATION ---
# Based on your previous setup
BASE_DIR = Path(__file__).resolve().parent
SECRETS_DIR = BASE_DIR / "data" / "secrets"

CREDENTIALS_FILE = SECRETS_DIR / "credentials.json"
TOKEN_FILE = SECRETS_DIR / "token.pickle"

# Full access to Google Drive
SCOPES = ['https://www.googleapis.com/auth/drive']

def main():
    print("="*50)
    print("      GOOGLE DRIVE TOKEN GENERATOR")
    print("="*50)

    # 1. Check if credentials.json exists
    if not CREDENTIALS_FILE.exists():
        print(f"❌ ERROR: Could not find credentials file at:")
        print(f"   {CREDENTIALS_FILE}")
        print("\nPlease move your 'credentials.json' downloaded from Google into the 'data/secrets/' folder.")
        return

    # 2. Check if old token exists and delete it (Start Fresh)
    if TOKEN_FILE.exists():
        print(f"🗑️  Deleting old token: {TOKEN_FILE}")
        os.remove(TOKEN_FILE)

    print("🚀 Initiating OAuth 2.0 Flow...")
    
    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(CREDENTIALS_FILE), SCOPES
        )
        
        # This will print a URL to the console. 
        # Click it, log in, and it might auto-close or give you a code.
        creds = flow.run_local_server(port=0)

        # 3. Save the new token
        print(f"💾 Saving new token to: {TOKEN_FILE}")
        with open(TOKEN_FILE, 'wb') as token:
            pickle.dump(creds, token)

        print("\n✅ SUCCESS! Token generated.")
        print("You can now restart 'main.py'.")

    except Exception as e:
        print(f"\n❌ FAILED: {e}")

if __name__ == '__main__':
    main()