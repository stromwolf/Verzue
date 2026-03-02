import logging
import time
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from .client import GDriveClient

logger = logging.getLogger("GDriveUploader")

class GDriveUploader:
    def __init__(self, client: GDriveClient):
        self.client = client
        self.service = client.get_service()

    def find_folder(self, name: str, parent_id: str):
        """Finds ONLY folders (Shared Drive Compatible)."""
        safe_name = name.replace("'", "\\'")
        query = f"mimeType='application/vnd.google-apps.folder' and name='{safe_name}' and '{parent_id}' in parents and trashed=false"
        try:
            # CRITICAL FIX: Added supportsAllDrives and includeItemsFromAllDrives
            results = self.service.files().list(
                q=query, 
                fields="files(id)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            ).execute()
            files = results.get('files', [])
            return files[0]['id'] if files else None
        except Exception as e:
            logger.error(f"Folder search failed for '{name}': {e}")
            return None

    def find_item(self, name: str, parent_id: str):
        """Finds ANY item (Shared Drive Compatible)."""
        safe_name = name.replace("'", "\\'")
        query = f"name='{safe_name}' and '{parent_id}' in parents and trashed=false"
        try:
            # CRITICAL FIX: Added supportsAllDrives and includeItemsFromAllDrives
            results = self.service.files().list(
                q=query, 
                fields="files(id)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True
            ).execute()
            files = results.get('files', [])
            return files[0]['id'] if files else None
        except Exception as e:
            logger.error(f"Item search failed for '{name}': {e}")
            return None

    def list_all_items(self, parent_id):
        """
        Fetches ALL items in a folder in one go.
        Returns a dictionary: { "File Name": "File ID" }
        """
        items_map = {}
        query = f"'{parent_id}' in parents and trashed = false"
        try:
            page_token = None
            while True:
                # We fetch 1000 items at a time (Max Level Efficiency)
                results = self.service.files().list(
                    q=query,
                    fields="nextPageToken, files(id, name)",
                    pageSize=1000,
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                    pageToken=page_token
                ).execute()
                
                for f in results.get('files', []):
                    items_map[f['name']] = f['id']
                
                page_token = results.get('nextPageToken')
                if not page_token:
                    break
            return items_map
        except Exception as e:
            logger.error(f"Failed to list folder {parent_id}: {e}")
            return {}

    def create_folder(self, name: str, parent_id: str):
        """Creates a folder with Shared Drive support and retries."""
        existing_id = self.find_folder(name, parent_id)
        if existing_id: return existing_id

        metadata = {
            'name': name,
            'mimeType': 'application/vnd.google-apps.folder',
            'parents': [parent_id]
        }
        
        for attempt in range(3):
            try:
                # CRITICAL FIX: Added supportsAllDrives=True
                folder = self.service.files().create(
                    body=metadata, 
                    fields='id',
                    supportsAllDrives=True 
                ).execute()
                logger.info(f"📁 Created folder: {name}")
                return folder.get('id')
            except Exception as e:
                if attempt == 2: 
                    logger.error(f"Final attempt failed to create folder '{name}': {e}")
                    raise e
                time.sleep(1)

    def upload_file(self, file_path, file_name, parent_id):
        """Uploads file with SSL-error protection and retries."""
        from googleapiclient.http import MediaFileUpload
        for attempt in range(5):
            try:
                metadata = {'name': file_name, 'parents': [parent_id]}
                media = MediaFileUpload(str(file_path), mimetype='image/jpeg', resumable=True)
                # CRITICAL FIX: Added supportsAllDrives=True
                self.service.files().create(
                    body=metadata, 
                    media_body=media, 
                    fields='id',
                    supportsAllDrives=True
                ).execute()
                logger.info(f"⬆️ Uploaded {file_name}")
                return
            except Exception as e:
                # Catch SSL and transient network errors
                if "SSL" in str(e) or "version number" in str(e):
                    logger.warning(f"⚠️ SSL/Transient error during upload of {file_name} (Attempt {attempt+1}): {e}")
                    time.sleep(2 * (attempt + 1))
                    continue
                if attempt == 4:
                    logger.error(f"Upload failed for {file_name} after retries: {e}")
                else:
                    logger.warning(f"⚠️ Upload error for {file_name} (Attempt {attempt+1}): {e}")
                    time.sleep(1)

    def create_shortcut(self, target_id, parent_id, name):
        """Creates shortcut with Shared Drive support."""
        metadata = {
            'name': name,
            'mimeType': 'application/vnd.google-apps.shortcut',
            'parents': [parent_id],
            'shortcutDetails': {'targetId': target_id}
        }
        try:
            # CRITICAL FIX: Added supportsAllDrives=True
            file = self.service.files().create(
                body=metadata, 
                fields='id',
                supportsAllDrives=True
            ).execute()
            logger.info(f"🔗 Created Shortcut: {name}")
            return file.get('id')
        except Exception as e:
            logger.error(f"Shortcut creation failed: {e}")
            return None

    def rename_file(self, file_id, new_name):
        """Renames file with Shared Drive support."""
        try:
            # CRITICAL FIX: Added supportsAllDrives=True
            self.service.files().update(
                fileId=file_id, 
                body={'name': new_name},
                supportsAllDrives=True
            ).execute()
            logger.info(f"✨ Renamed to: {new_name}")
        except Exception as e:
            logger.error(f"Rename failed for {file_id}: {e}")

    def get_share_link(self, file_id):
        """Fetches link with Shared Drive support."""
        try:
            # CRITICAL FIX: Added supportsAllDrives=True
            file = self.service.files().get(
                fileId=file_id, 
                fields='webViewLink',
                supportsAllDrives=True
            ).execute()
            return file.get('webViewLink')
        except Exception as e:
            logger.error(f"Failed to get link for {file_id}: {e}")
            return None

    def make_public(self, file_id):
        """Sets permissions with Shared Drive support."""
        try:
            permission = {'role': 'reader', 'type': 'anyone'}
            self.service.permissions().create(
                fileId=file_id,
                body=permission,
                supportsAllDrives=True
            ).execute()
        except Exception as e:
            logger.error(f"Permission error for {file_id}: {e}")
