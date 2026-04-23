"""
app/services/gdrive/client.py
Phase 1: pickle replaced with google.oauth2.credentials.Credentials JSON serialization.
         Token stored via SecretStore (encrypted vault / OS keyring).
         Legacy token.pickle auto-migrated on first boot then deleted.
"""

import json
import logging
import threading

import httplib2
import google_auth_httplib2
from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from config.settings import Settings
from app.security.secret_store import SecretStore

logger = logging.getLogger("GDriveClient")

_NAMESPACE = "gdrive"
_KEY = "token"


class GDriveClient:
    SCOPES = ['https://www.googleapis.com/auth/drive']

    def __init__(self):
        self.creds: Credentials | None = None
        self._local = threading.local()
        self._auth_lock = threading.RLock()
        self._migrate_pickle_if_present()
        self.authenticate()

    # ------------------------------------------------------------------
    # One-time migration from token.pickle → SecretStore
    # ------------------------------------------------------------------

    def _migrate_pickle_if_present(self) -> None:
        """
        If a legacy token.pickle exists and the vault has no token yet,
        read the pickle, save to SecretStore, then delete the pickle.
        Runs once silently; safe to call on every boot.
        """
        pickle_path = Settings.TOKEN_PICKLE
        if not pickle_path.exists():
            return
        if SecretStore.get(_NAMESPACE, _KEY) is not None:
            # Vault already populated — just remove the leftover pickle.
            try:
                pickle_path.unlink()
                logger.info("[GDriveClient] Removed legacy token.pickle (vault already populated).")
            except OSError:
                pass
            return

        try:
            import pickle
            with open(pickle_path, "rb") as f:
                old_creds = pickle.load(f)   # one-time read of legacy file

            # Serialize using the safe JSON method.
            token_json = old_creds.to_json()
            SecretStore.put(_NAMESPACE, _KEY, token_json)
            pickle_path.unlink()
            logger.info("[GDriveClient] Migrated token.pickle → SecretStore and deleted pickle file.")
        except Exception as e:
            logger.warning(f"[GDriveClient] Could not migrate token.pickle: {e}. Will re-authenticate.")

    # ------------------------------------------------------------------
    # Core auth
    # ------------------------------------------------------------------

    def _load_token(self) -> Credentials | None:
        raw = SecretStore.get(_NAMESPACE, _KEY)
        if not raw:
            return None
        try:
            return Credentials.from_authorized_user_info(json.loads(raw), self.SCOPES)
        except Exception as e:
            logger.warning(f"[GDriveClient] Token deserialization failed: {e}")
            return None

    def _save_token(self, creds: Credentials) -> None:
        try:
            SecretStore.put(_NAMESPACE, _KEY, creds.to_json())
            logger.debug("[GDriveClient] Token saved to SecretStore.")
        except Exception as e:
            logger.error(f"[GDriveClient] Failed to save token: {e}")

    def authenticate(self) -> None:
        """Thread-safe authentication and token management."""
        with self._auth_lock:
            # 1. Load from SecretStore
            if not self.creds:
                self.creds = self._load_token()

            # 2. Refresh if expired
            if not self.creds or not self.creds.valid:
                if self.creds and self.creds.expired and self.creds.refresh_token:
                    logger.info("[GDriveClient] Refreshing expired token...")
                    try:
                        self.creds.refresh(Request())
                        self._save_token(self.creds)
                    except Exception as e:
                        logger.error(f"[GDriveClient] Token refresh failed: {e}")
                        self.creds = None

                # 3. Full OAuth flow (manual — VPS headless flow prints URL to console)
                if not self.creds or not self.creds.valid:
                    logger.info("[GDriveClient] Initiating new OAuth flow...")
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(Settings.CREDENTIALS_JSON), self.SCOPES
                    )
                    self.creds = flow.run_local_server(port=0)
                    self._save_token(self.creds)

            # 4. Invalidate thread-local service so it picks up fresh creds
            if hasattr(self._local, 'service'):
                del self._local.service

            logger.info("✅ GDrive Credentials Authenticated.")

    def get_service(self):
        """Returns a thread-local service instance."""
        if not self.creds or not self.creds.valid:
            self.authenticate()
        if not hasattr(self._local, 'service'):
            http = google_auth_httplib2.AuthorizedHttp(
                self.creds, http=httplib2.Http(timeout=120)
            )
            self._local.service = build('drive', 'v3', http=http, cache_discovery=False)
            logger.debug("Created thread-local GDrive Service instance.")
        return self._local.service

    def get_creds(self) -> Credentials | None:
        if not self.creds:
            self.authenticate()
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
