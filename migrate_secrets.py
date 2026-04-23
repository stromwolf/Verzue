import asyncio
import logging
from app.services.gdrive.client import GDriveClient
from app.services.login.service import LoginService

# Configure logging to show migration details
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("SecretMigrator")

async def main():
    print("="*60)
    print("STARTING SECRET STORAGE MIGRATION")
    print("="*60)
    print("\nThis tool will migrate legacy .pickle and .json secrets into")
    print("the new encrypted SecretStore (Keyring + AES-256-GCM Vault).")
    print("-" * 60)

    # 1. Migrate GDrive Tokens
    print("\n[Phase 1] Checking GDrive Tokens...")
    try:
        # Initializing GDriveClient automatically triggers _migrate_pickle_if_present()
        _ = GDriveClient()
        print("OK: GDrive migration check finished.")
    except Exception as e:
        logger.error(f"Error: GDrive migration failed: {e}")

    # 2. Migrate Account Credentials
    print("\n[Phase 2] Checking Account Credentials...")
    try:
        login_service = LoginService()
        # Common platforms to check
        platforms = ["piccoma", "mecha"]
        
        for platform in platforms:
            # Calling get_credentials triggers _migrate_account_json_if_present()
            await login_service.get_credentials(platform)
            print(f"OK: Checked migration for: {platform}")
            
        print("OK: Account migration check finished.")
    except Exception as e:
        logger.error(f"Error: Account migration failed: {e}")

    print("\n" + "="*60)
    print("MIGRATION COMPLETE")
    print("Vault: data/secrets/.vault.json")
    print("Key:   data/secrets/.vault_key (Permissions: 0600)")
    print("="*60)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nMigration interrupted by user.")
    except Exception as e:
        print(f"\nFATAL ERROR: {e}")
