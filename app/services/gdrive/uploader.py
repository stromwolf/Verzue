import logging
import time
import threading
import io
import os
from googleapiclient.http import MediaIoBaseUpload
from googleapiclient.errors import HttpError
from .client import GDriveClient

logger = logging.getLogger("GDriveUploader")

# 🟢 UPDATE (08 March 2026): Google Drive API Quotas
# - Queries: 12,000 / 60s (200/sec)
# - Sustained Writes: ~3 / sec (CRITICAL)
# - Daily Upload Limit: 750 GB

class GDriveUploader:
    def __init__(self, client):
        self.client = client
        self._write_semaphore = threading.Semaphore(5)
        self.is_disabled = client is None or getattr(client, "is_null", False)

    def find_folder(self, name: str, parent_id: str):
        """Finds ONLY folders (Shared Drive Compatible, Case-Insensitive)."""
        if self.is_disabled: return None
        # Escape single quotes for Google Drive API query syntax
        safe_name = name.replace("'", "\\'")
        # Use exact name match for folders
        query = f"mimeType='application/vnd.google-apps.folder' and name = '{safe_name}' and '{parent_id}' in parents and trashed=false"
        try:
            service = self.client.get_service()
            if not service: return None
            results = service.files().list(
                q=query, 
                fields="files(id, name)", 
                supportsAllDrives=True, 
                includeItemsFromAllDrives=True
            ).execute()
            files = results.get('files', [])
            
            # If search returns nothing, try a broader search as fallback (e.g. if case-sensitivity is an issue)
            if not files:
                broad_query = f"mimeType='application/vnd.google-apps.folder' and name contains '{safe_name}' and '{parent_id}' in parents and trashed=false"
                results = self.client.get_service().files().list(
                    q=broad_query, 
                    fields="files(id, name)", 
                    supportsAllDrives=True, 
                    includeItemsFromAllDrives=True
                ).execute()
                files = results.get('files', [])

            for f in files:
                if f['name'].lower() == name.lower():
                    return f['id']
            return None
        except Exception as e:
            logger.error(f"Folder search failed for '{name}': {e}")
            return None

    def find_folder_by_prefix(self, prefix: str, parent_id: str):
        """Finds a folder that starts with a specific prefix (e.g. '[SeriesID]'). Returns {id, name} or None."""
        safe_prefix = prefix.replace("'", "\\'")
        query = f"mimeType='application/vnd.google-apps.folder' and name contains '{safe_prefix}' and '{parent_id}' in parents and trashed=false"
        try:
            results = self.client.get_service().files().list(
                q=query, 
                fields="files(id, name)", 
                supportsAllDrives=True, 
                includeItemsFromAllDrives=True
            ).execute()
            files = results.get('files', [])
            # Filter manually to ensure it STARTS with the prefix
            for f in files:
                if f['name'].startswith(prefix):
                    return f # Return full metadata (id, name)
            return None
        except Exception as e:
            logger.error(f"Folder prefix search failed for '{prefix}': {e}")
            return None

    def find_item(self, name: str, parent_id: str):
        """Finds ANY item (Shared Drive Compatible, Case-Insensitive fallback)."""
        safe_name = name.replace("'", "\\'")
        # Try exact match first (case-sensitive in Drive API)
        query = f"name='{safe_name}' and '{parent_id}' in parents and trashed=false"
        try:
            results = self.client.get_service().files().list(
                q=query, 
                fields="files(id, name)", 
                supportsAllDrives=True, 
                includeItemsFromAllDrives=True
            ).execute()
            files = results.get('files', [])
            
            # Fallback for case-insensitive match if exact fails
            if not files:
                broad_query = f"name contains '{safe_name}' and '{parent_id}' in parents and trashed=false"
                results = self.client.get_service().files().list(
                    q=broad_query, 
                    fields="files(id, name)", 
                    supportsAllDrives=True, 
                    includeItemsFromAllDrives=True
                ).execute()
                files = results.get('files', [])
            
            for f in files:
                if f['name'].lower() == name.lower():
                    return f['id']
            return None
        except Exception as e:
            logger.error(f"Item search failed for '{name}': {e}")
            return None

    def list_all_items(self, parent_id):
        """Fetches ALL items in a folder in one go."""
        items_map = {}
        query = f"'{parent_id}' in parents and trashed = false"
        try:
            page_token = None
            while True:
                results = self.client.get_service().files().list(
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

        if self.is_disabled: return "null-folder-id"
        existing_id = self.find_folder(name, parent_id)
        if existing_id: return existing_id

        metadata = {'name': name, 'mimeType': 'application/vnd.google-apps.folder', 'parents': [parent_id]}
        
        for attempt in range(3):
            try:
                with self._write_semaphore:
                    folder = self.client.get_service().files().create(
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
        """Uploads file with SSL-error protection and GUARANTEED file handle release."""
        import mimetypes
        
        if self.is_disabled or not self.client.get_service():
            logger.warning(f"⏩ Skipping GDrive upload for {file_name} (Drive Disabled)")
            return
        
        # 🟢 Dynamic mimetype detection
        mime_type, _ = mimetypes.guess_type(file_name)
        if not mime_type:
            if file_name.lower().endswith('.webp'):
                mime_type = 'image/webp'
            else:
                mime_type = 'image/jpeg'

        logger.info(f"⏳ Beginning upload of {file_name}...")
        for attempt in range(5):
            try:
                metadata = {'name': file_name, 'parents': [parent_id]}
                with open(file_path, 'rb') as f:
                    media = MediaIoBaseUpload(f, mimetype=mime_type, resumable=True)
                    with self._write_semaphore:
                        self.client.get_service().files().create(
                            body=metadata, 
                            media_body=media, 
                            fields='id', 
                            supportsAllDrives=True
                        ).execute()
                logger.debug(f"⬆️ Uploaded {file_name} ({mime_type})")
                return
            except Exception as e:
                err_msg = str(e).lower()
                is_timeout = "timeout" in err_msg or "deadline" in err_msg
                
                if is_timeout:
                    logger.warning(f"⏰ Network Timeout during upload of {file_name} (Attempt {attempt+1})")
                    time.sleep(5) # Give it some room to breathe
                    continue

                if "ssl" in err_msg or "version number" in err_msg or "connection reset" in err_msg:
                    logger.warning(f"⚠️ SSL/Transient error during upload of {file_name} (Attempt {attempt+1}): {e}")
                    time.sleep(2 * (attempt + 1))
                    continue
                if attempt == 4:
                    logger.error(f"Upload failed for {file_name} after retries: {e}")
                else:
                    logger.warning(f"⚠️ Upload error for {file_name} (Attempt {attempt+1}): {e}")
                    time.sleep(1)

    def create_shortcut(self, target_id, parent_id, name):
        """Creates shortcut with Shared Drive support, checking for existence first."""
        existing_id = self.find_item(name, parent_id)
        if existing_id: 
            logger.debug(f"🔗 Shortcut already exists: {name}")
            return existing_id

        metadata = {
            'name': name, 'mimeType': 'application/vnd.google-apps.shortcut',
            'parents': [parent_id], 'shortcutDetails': {'targetId': target_id}
        }
        try:
            with self._write_semaphore:
                file = self.client.get_service().files().create(
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
            with self._write_semaphore:
                self.client.get_service().files().update(
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
            file = self.client.get_service().files().get(
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
            with self._write_semaphore:
                self.client.get_service().permissions().create(
                    fileId=file_id, 
                    body=permission, 
                    supportsAllDrives=True
                ).execute()
        except Exception as e:
            logger.error(f"Permission error for {file_id}: {e}")

    def delete_file(self, file_id):
        """Deletes a file or folder (Shared Drive Compatible)."""
        try:
            with self._write_semaphore:
                self.client.get_service().files().delete(
                    fileId=file_id,
                    supportsAllDrives=True
                ).execute()
            logger.info(f"🗑️ Deleted item: {file_id}")
            return True
        except Exception as e:
            logger.error(f"Delete failed for {file_id}: {e}")
            return False
