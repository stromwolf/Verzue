"""
app/security/secret_store.py
============================
Phase 1 — Single encrypted I/O boundary for all Verzue secrets.

Architecture
------------
Secrets live in one of two backends, tried in order:

  1. OS keyring  (keyring package — Secret Service / Keychain / DPAPI)
     Best option on desktop/dev. Zero extra files.

  2. AES-256-GCM encrypted JSON file  (data/secrets/.vault.json)
     Used on headless Linux VPS where no keyring daemon runs.
     Master key is derived from a 32-byte random seed stored in
     data/secrets/.vault_key  (chmod 0600, created on first use).

Public API
----------
    from app.security.secret_store import SecretStore

    # Write
    SecretStore.put("gdrive", "token", json_string)
    SecretStore.put("piccoma", "credentials", json_string)

    # Read  (returns None if absent)
    value = SecretStore.get("gdrive", "token")

    # Delete
    SecretStore.delete("piccoma", "credentials")

    # List keys in a namespace
    keys = SecretStore.list_keys("gdrive")   # e.g. ["token"]

Namespace + key are joined as  "<namespace>/<key>"  for the keyring
service name, and stored under  vault[namespace][key]  in the JSON vault.

Values are always plain Python strings (JSON-encoded blobs, tokens, etc.).
Encoding/decoding of structured data is the caller's responsibility.

Migration
---------
Run  scripts/migrate_secrets.py  once after deploying Phase 1 to move
existing account.json files and token.pickle into the vault, then delete
the originals.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Optional

logger = logging.getLogger("SecretStore")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_BASE_DIR = Path(__file__).resolve().parent.parent.parent   # project root
_SECRETS_DIR = _BASE_DIR / "data" / "secrets"
_VAULT_FILE = _SECRETS_DIR / ".vault.json"
_VAULT_KEY_FILE = _SECRETS_DIR / ".vault_key"

_KEYRING_SERVICE = "verzue-bot"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_secrets_dir() -> None:
    _SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(_SECRETS_DIR, stat.S_IRWXU)   # 0700
    except OSError:
        pass


def _load_or_create_master_key() -> bytes:
    """Return 32-byte master key, creating it on first call."""
    _ensure_secrets_dir()
    if _VAULT_KEY_FILE.exists():
        raw = _VAULT_KEY_FILE.read_bytes()
        if len(raw) == 32:
            return raw
        logger.warning("[SecretStore] Corrupt vault key — regenerating. All existing vault entries will be unreadable.")

    key = os.urandom(32)
    _VAULT_KEY_FILE.write_bytes(key)
    try:
        os.chmod(_VAULT_KEY_FILE, stat.S_IRUSR | stat.S_IWUSR)   # 0600
    except OSError:
        pass
    logger.info("[SecretStore] New vault master key generated.")
    return key


def _encrypt(plaintext: str, key: bytes) -> dict:
    """AES-256-GCM encrypt. Returns dict with b64-encoded fields."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import base64

    nonce = os.urandom(12)
    aesgcm = AESGCM(key)
    ct = aesgcm.encrypt(nonce, plaintext.encode(), None)
    return {
        "n": base64.b64encode(nonce).decode(),
        "c": base64.b64encode(ct).decode(),
    }


def _decrypt(entry: dict, key: bytes) -> str:
    """AES-256-GCM decrypt. Raises ValueError on tamper/wrong key."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    import base64

    nonce = base64.b64decode(entry["n"])
    ct = base64.b64decode(entry["c"])
    aesgcm = AESGCM(key)
    return aesgcm.decrypt(nonce, ct, None).decode()


# ---------------------------------------------------------------------------
# Backend: OS keyring
# ---------------------------------------------------------------------------

def _keyring_available() -> bool:
    try:
        import keyring                          # noqa: F401
        import keyring.errors                   # noqa: F401
        return True
    except ImportError:
        return False


def _kr_get(namespace: str, key: str) -> Optional[str]:
    import keyring
    try:
        return keyring.get_password(_KEYRING_SERVICE, f"{namespace}/{key}")
    except Exception as e:
        logger.debug(f"[SecretStore/keyring] get failed: {e}")
        return None


def _kr_put(namespace: str, key: str, value: str) -> bool:
    import keyring
    try:
        keyring.set_password(_KEYRING_SERVICE, f"{namespace}/{key}", value)
        return True
    except Exception as e:
        logger.debug(f"[SecretStore/keyring] put failed: {e}")
        return False


def _kr_delete(namespace: str, key: str) -> None:
    import keyring, keyring.errors
    try:
        keyring.delete_password(_KEYRING_SERVICE, f"{namespace}/{key}")
    except keyring.errors.PasswordDeleteError:
        pass
    except Exception as e:
        logger.debug(f"[SecretStore/keyring] delete failed: {e}")


# ---------------------------------------------------------------------------
# Backend: encrypted JSON vault
# ---------------------------------------------------------------------------

def _vault_load() -> dict:
    if not _VAULT_FILE.exists():
        return {}
    try:
        return json.loads(_VAULT_FILE.read_text())
    except Exception as e:
        logger.error(f"[SecretStore/vault] Failed to read vault: {e}")
        return {}


def _vault_save(data: dict) -> None:
    _ensure_secrets_dir()
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=_SECRETS_DIR, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _VAULT_FILE)
        os.chmod(_VAULT_FILE, stat.S_IRUSR | stat.S_IWUSR)   # 0600
    except Exception as e:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        logger.error(f"[SecretStore/vault] Failed to write vault: {e}")
        raise


def _vault_get(namespace: str, key: str) -> Optional[str]:
    data = _vault_load()
    entry = data.get(namespace, {}).get(key)
    if entry is None:
        return None
    try:
        return _decrypt(entry, _load_or_create_master_key())
    except Exception as e:
        logger.error(f"[SecretStore/vault] Decryption failed for {namespace}/{key}: {e}")
        return None


def _vault_put(namespace: str, key: str, value: str) -> None:
    data = _vault_load()
    master_key = _load_or_create_master_key()
    data.setdefault(namespace, {})[key] = _encrypt(value, master_key)
    _vault_save(data)


def _vault_delete(namespace: str, key: str) -> None:
    data = _vault_load()
    if namespace in data and key in data[namespace]:
        del data[namespace][key]
        if not data[namespace]:
            del data[namespace]
        _vault_save(data)


def _vault_list_keys(namespace: str) -> list[str]:
    data = _vault_load()
    return list(data.get(namespace, {}).keys())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class SecretStore:
    """
    Encrypted secret storage with OS keyring → file vault fallback.

    All methods are synchronous (secrets I/O is fast and infrequent;
    wrapping in asyncio.to_thread is the caller's job if needed).
    """

    _use_keyring: Optional[bool] = None   # cached after first probe

    @classmethod
    def _backend(cls) -> str:
        """Returns 'keyring' or 'vault'. Cached after first call."""
        if cls._use_keyring is None:
            cls._use_keyring = _keyring_available()
            backend = "keyring" if cls._use_keyring else "encrypted-vault"
            logger.info(f"[SecretStore] Backend: {backend}")
        return "keyring" if cls._use_keyring else "vault"

    @classmethod
    def get(cls, namespace: str, key: str) -> Optional[str]:
        """Return secret value, or None if absent."""
        if cls._backend() == "keyring":
            val = _kr_get(namespace, key)
            if val is not None:
                return val
            # Fallback: may have been written to vault before keyring was available
            return _vault_get(namespace, key)
        return _vault_get(namespace, key)

    @classmethod
    def put(cls, namespace: str, key: str, value: str) -> None:
        """Store a secret. Overwrites if already present."""
        if cls._backend() == "keyring":
            if _kr_put(namespace, key, value):
                return
            logger.warning("[SecretStore] keyring write failed — falling back to vault.")
        _vault_put(namespace, key, value)

    @classmethod
    def delete(cls, namespace: str, key: str) -> None:
        """Delete a secret. No-op if absent."""
        _kr_delete(namespace, key)    # safe even if vault is backend
        _vault_delete(namespace, key)  # clean both in case of backend switch

    @classmethod
    def list_keys(cls, namespace: str) -> list[str]:
        """Return all stored key names under a namespace."""
        keys: set[str] = set()
        if cls._backend() == "keyring":
            # keyring has no native list; rely on vault index for discovery
            pass
        keys.update(_vault_list_keys(namespace))
        return list(keys)
