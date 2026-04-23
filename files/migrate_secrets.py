#!/usr/bin/env python3
"""
scripts/migrate_secrets.py
==========================
Phase 1 one-time migration: moves all legacy plaintext secret files
into the SecretStore vault, then deletes the originals.

Handles:
  - data/secrets/<platform>/account.json  → vault namespace "credentials"
  - data/secrets/token.pickle             → vault namespace "gdrive" (via GDriveClient)

Run once after deploying Phase 1:
    python scripts/migrate_secrets.py

Safe to re-run — skips anything already in the vault.
"""

import json
import sys
from pathlib import Path

# Make sure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.security.secret_store import SecretStore

SECRETS_DIR = Path(__file__).resolve().parent.parent / "data" / "secrets"


def migrate_account_jsons() -> None:
    print("\n── Credentials (account.json files) ──────────────────────────")
    if not SECRETS_DIR.exists():
        print("  data/secrets/ not found — nothing to migrate.")
        return

    migrated, skipped, failed = 0, 0, 0

    for account_file in SECRETS_DIR.rglob("account.json"):
        platform = account_file.parent.name
        vault_key = f"{platform}/primary"

        if SecretStore.get("credentials", vault_key) is not None:
            print(f"  SKIP  {platform}/account.json — already in vault.")
            # Remove the stale file anyway.
            try:
                account_file.unlink()
                print(f"        Deleted stale file.")
            except OSError as e:
                print(f"        Could not delete: {e}")
            skipped += 1
            continue

        try:
            data = json.loads(account_file.read_text())
            SecretStore.put("credentials", vault_key, json.dumps(data))
            account_file.unlink()
            print(f"  ✅    {platform}/account.json → vault (deleted original).")
            migrated += 1
        except Exception as e:
            print(f"  ❌    {platform}/account.json FAILED: {e}")
            failed += 1

    print(f"\n  Result: {migrated} migrated, {skipped} already done, {failed} failed.")


def migrate_token_pickle() -> None:
    print("\n── GDrive token (token.pickle) ───────────────────────────────")
    pickle_path = SECRETS_DIR / "token.pickle"

    if not pickle_path.exists():
        print("  token.pickle not found — nothing to migrate.")
        return

    if SecretStore.get("gdrive", "token") is not None:
        print("  SKIP  token already in vault.")
        try:
            pickle_path.unlink()
            print("        Deleted stale token.pickle.")
        except OSError as e:
            print(f"        Could not delete: {e}")
        return

    try:
        import pickle
        with open(pickle_path, "rb") as f:
            creds = pickle.load(f)
        SecretStore.put("gdrive", "token", creds.to_json())
        pickle_path.unlink()
        print("  ✅    token.pickle → vault (deleted original).")
    except Exception as e:
        print(f"  ❌    token.pickle FAILED: {e}")
        print("        Run generate_token.py to create a fresh token.")


def main() -> None:
    print("=" * 56)
    print("  Verzue Phase 1 — Secret Migration")
    print("=" * 56)

    migrate_account_jsons()
    migrate_token_pickle()

    print("\n── Post-migration check ──────────────────────────────────────")
    remaining = list(SECRETS_DIR.rglob("account.json")) + list(SECRETS_DIR.glob("token.pickle"))
    if remaining:
        print("  ⚠️  Files still present (check errors above):")
        for f in remaining:
            print(f"     {f}")
    else:
        print("  ✅  No legacy plaintext secret files remain.")

    print("\nDone. Restart the bot.\n")


if __name__ == "__main__":
    main()
