import os
import json
import logging
import threading
import httplib2
import google_auth_httplib2
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from config.settings import Settings
from app.core.secret_store import SecretStore

logger = logging.getLogger("GDriveClient")

class GDriveClient:
    SCOPES = ['https://www.googleapis.com/auth/drive']

    def __init__(self):
        self.creds = None
        self.secret_store = SecretStore()
        self._local = threading.local()
        self._auth_lock = threading.RLock() # 🛡️ Guard against concurrent refreshes
        self.authenticate()

    def _migrate_pickle_if_present(self):
        """One-time migration from pickle to SecretStore."""
        if Settings.TOKEN_PICKLE.exists():
            try:
                import pickle
                logger.info("📦 Legacy GDrive token.pickle found. Migrating...")
                with open(Settings.TOKEN_PICKLE, 'rb') as f:
                    creds = pickle.load(f)
                
                # Check if vault already has it to avoid overwriting newer data
                if not self.secret_store.get("gdrive_token"):
                    self.secret_store.set("gdrive_token", creds.to_json())
                    logger.info("🚚 Migrated GDrive token from pickle to SecretStore.")
                
                os.remove(Settings.TOKEN_PICKLE)
                logger.info(f"🗑️ Deleted legacy {Settings.TOKEN_PICKLE.name}")
            except Exception as e:
                logger.error(f"Failed to migrate GDrive pickle: {e}")

    def authenticate(self):
        """Thread-safe authentication and token management."""
        with self._auth_lock:
            # 0. Check for legacy migration
            self._migrate_pickle_if_present()

            # 1. Load from SecretStore if not already in memory
            if not self.creds:
                token_json = self.secret_store.get("gdrive_token")
                if token_json:
                    self.creds = Credentials.from_authorized_user_info(json.loads(token_json), self.SCOPES)

            # 2. Check and Refresh if needed
            if not self.creds or not self.creds.valid:
                if self.creds and self.creds.expired and self.creds.refresh_token:
                    logger.info("Refreshing expired GDrive token...")
                    try:
                        self.creds.refresh(Request())
                    except Exception as e:
                        logger.error(f"Failed to refresh GDrive token: {e}")
                        self.creds = None # Force a new login if refresh fails

                # 3. New Login if no valid creds (Manual step usually)
                if not self.creds or not self.creds.valid:
                    logger.info("Initiating new GDrive OAuth Login flow...")
                    flow = InstalledAppFlow.from_client_secrets_file(str(Settings.CREDENTIALS_JSON), self.SCOPES)
                    self.creds = flow.run_local_server(port=0)

                # 4. Save back to SecretStore immediately
                self.secret_store.set("gdrive_token", self.creds.to_json())
                logger.info("💾 Updated GDrive token saved to SecretStore.")

            # 5. Clear local services to ensure they pick up new creds
            if hasattr(self._local, 'service'):
                del self._local.service
            
            logger.info("✅ GDrive Credentials Authenticated.")

    def get_service(self):
        """Returns a thread-local service instance to prevent httplib2 socket corruption."""
        if not self.creds or not self.creds.valid:
            self.authenticate()
            
        if not hasattr(self._local, 'service'):
            # Use httplib2-based transport with an explicit timeout (120s)
            # We create a NEW Http() and service for EVERY thread via threading.local
            http = google_auth_httplib2.AuthorizedHttp(self.creds, http=httplib2.Http(timeout=120))
            self._local.service = build('drive', 'v3', http=http, cache_discovery=False)
            logger.debug("Created thread-local GDrive Service instance (Timeout: 120s).")
            
        return self._local.service

    def get_creds(self):
        if not self.creds: self.authenticate()
        return self.creds


class NullGDriveClient:
    """Fallback client when GDrive is disabled or authentication fails."""
    def __init__(self, *args, **kwargs):
        logger.warning("🚫 Running with NullGDriveClient (Degraded Mode)")

    def authenticate(self): pass
    def get_service(self): return None
    def get_creds(self): return None
    @property
    def is_null(self): return True