import pickle
import logging
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
        self.service = None
        self.authenticate()

    def authenticate(self):
        if Settings.TOKEN_PICKLE.exists():
            with open(Settings.TOKEN_PICKLE, 'rb') as token:
                self.creds = pickle.load(token)

        if not self.creds or not self.creds.valid:
            if self.creds and self.creds.expired and self.creds.refresh_token:
                logger.info("Refreshing expired GDrive token...")
                self.creds.refresh(Request())
            else:
                logger.info("Initiating new GDrive OAuth Login...")
                flow = InstalledAppFlow.from_client_secrets_file(str(Settings.CREDENTIALS_JSON), self.SCOPES)
                self.creds = flow.run_local_server(port=0)

            with open(Settings.TOKEN_PICKLE, 'wb') as token:
                pickle.dump(self.creds, token)

        # Use requests-based transport for better SSL/Proxy handling
        from google.auth.transport.requests import AuthorizedSession
        session = AuthorizedSession(self.creds)
        self.service = build('drive', 'v3', http=session, cache_discovery=False)
        logger.info("✅ GDrive Service Authorized (Requests Transport).")

    def get_service(self):
        if not self.service: self.authenticate()
        return self.service