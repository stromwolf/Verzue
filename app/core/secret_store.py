import os
import json
import base64
import logging
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

try:
    import keyring
except ImportError:
    keyring = None

from config.settings import Settings

logger = logging.getLogger("SecretStore")

class SecretStore:
    SERVICE_NAME = "VerzueBot"

    def __init__(self):
        self.vault_path = Settings.VAULT_FILE
        self.key_path = Settings.VAULT_KEY_FILE
        self._master_key = None

    def _get_master_key(self) -> bytes:
        if self._master_key:
            return self._master_key

        if not self.key_path.exists():
            key = AESGCM.generate_key(bit_length=256)
            # Ensure directory exists
            self.key_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.key_path, "wb") as f:
                f.write(key)
            try:
                os.chmod(self.key_path, 0o600)
            except Exception:
                pass # Chmod might fail on some Windows setups, but we try
            logger.info(f"🛡️ Generated new master key at {self.key_path}")
        else:
            with open(self.key_path, "rb") as f:
                key = f.read()
        
        self._master_key = key
        return key

    def _encrypt(self, data: str) -> str:
        aesgcm = AESGCM(self._get_master_key())
        nonce = os.urandom(12)
        ciphertext = aesgcm.encrypt(nonce, data.encode(), None)
        return base64.b64encode(nonce + ciphertext).decode()

    def _decrypt(self, encrypted_data: str) -> str:
        try:
            data = base64.b64decode(encrypted_data)
            nonce = data[:12]
            ciphertext = data[12:]
            aesgcm = AESGCM(self._get_master_key())
            return aesgcm.decrypt(nonce, ciphertext, None).decode()
        except Exception as e:
            logger.error(f"Decryption failed: {e}")
            raise

    def _read_vault(self) -> dict:
        if not self.vault_path.exists():
            return {}
        try:
            with open(self.vault_path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read vault: {e}")
            return {}

    def _write_vault(self, data: dict):
        tmp_path = self.vault_path.with_suffix(".tmp")
        try:
            # Ensure directory exists
            self.vault_path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "w") as f:
                json.dump(data, f, indent=4)
            os.replace(tmp_path, self.vault_path)
            try:
                os.chmod(self.vault_path, 0o600)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Failed to write vault: {e}")
            if tmp_path.exists():
                os.remove(tmp_path)

    def get(self, key: str) -> str | None:
        """Retrieves a secret. Tries OS keyring first, then fallback to encrypted vault."""
        # 1. Try Keyring (Primary for Dev)
        if keyring:
            try:
                value = keyring.get_password(self.SERVICE_NAME, key)
                if value:
                    logger.debug(f"🔑 Retrieved '{key}' from OS keyring.")
                    return value
            except Exception as e:
                logger.debug(f"Keyring access failed for '{key}': {e}")

        # 2. Fallback to Vault (Primary for VPS/Production)
        vault = self._read_vault()
        encrypted_value = vault.get(key)
        if encrypted_value:
            try:
                value = self._decrypt(encrypted_value)
                logger.debug(f"📦 Retrieved '{key}' from encrypted vault.")
                return value
            except Exception as e:
                logger.error(f"Failed to decrypt '{key}' from vault: {e}")
        
        return None

    def set(self, key: str, value: str):
        """Stores a secret in both the encrypted vault (always) and OS keyring (if available)."""
        if not value:
            logger.warning(f"Attempted to set empty value for secret '{key}'. Skipping.")
            return

        # 1. Store in Vault (Always - acts as source of truth for VPS migration)
        vault = self._read_vault()
        vault[key] = self._encrypt(value)
        self._write_vault(vault)
        logger.info(f"💾 Stored '{key}' in encrypted vault.")

        # 2. Store in Keyring (Optional)
        if keyring:
            try:
                keyring.set_password(self.SERVICE_NAME, key, value)
                logger.debug(f"Stored '{key}' in OS keyring.")
            except Exception as e:
                logger.warning(f"Failed to store '{key}' in OS keyring: {e}")

    def delete(self, key: str):
        """Removes a secret from both the vault and OS keyring."""
        # 1. Remove from Vault
        vault = self._read_vault()
        if key in vault:
            del vault[key]
            self._write_vault(vault)
            logger.info(f"🗑️ Deleted '{key}' from encrypted vault.")

        # 2. Remove from Keyring
        if keyring:
            try:
                keyring.delete_password(self.SERVICE_NAME, key)
                logger.debug(f"Deleted '{key}' from OS keyring.")
            except Exception:
                pass
