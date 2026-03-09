import pickle
import logging
import threading
import httplib2
import google_auth_httplib2
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google_auth_oauthlib.flow import InstalledAppFlow
from config.settings import Settings

logger = logging.getLogger("GDriveClient")

class GDriveClient:
    SCOPES = ['https://www.googleapis.com/auth/drive']

    def __init__(self):
        self.creds = None
        self._local = threading.local()
        self._auth_lock = threading.RLock() # 🛡️ Guard against concurrent refreshes
        self.authenticate()

    def authenticate(self):
        """Thread-safe authentication and token management."""
        with self._auth_lock:
            # 1. Load from disk if not already in memory
            if not self.creds and Settings.TOKEN_PICKLE.exists():
                with open(Settings.TOKEN_PICKLE, 'rb') as token:
                    self.creds = pickle.load(token)

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

                # 4. Save back to disk immediately
                with open(Settings.TOKEN_PICKLE, 'wb') as token:
                    pickle.dump(self.creds, token)
                    logger.info("💾 Updated GDrive token saved to disk.")

            # 5. Clear local services to ensure they pick up new creds
            if hasattr(self._local, 'service'):
                del self._local.service
            
            logger.info("✅ GDrive Credentials Authenticated.")

    def get_service(self):
        """Returns a thread-local service instance to prevent httplib2 socket corruption."""
        if not self.creds or not self.creds.valid:
            self.authenticate()
            
        if not hasattr(self._local, 'service'):
            # Use httplib2-based transport as googleapiclient expects it
            # We create a NEW Http() and service for EVERY thread via threading.local
            http = google_auth_httplib2.AuthorizedHttp(self.creds, http=httplib2.Http())
            self._local.service = build('drive', 'v3', http=http, cache_discovery=False)
            logger.debug("Created thread-local GDrive Service instance.")
            
        return self._local.service

    def get_creds(self):
        if not self.creds: self.authenticate()
        return self.creds