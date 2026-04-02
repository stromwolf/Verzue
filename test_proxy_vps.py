import asyncio
import os
import sys
from pathlib import Path

# Add project root to sys.path so we can import config
sys.path.append(str(Path(__file__).parent))

from config.settings import Settings
from curl_cffi.requests import AsyncSession

async def diagnosis():
    # Load settings from .env
    Settings.ensure_dirs()
    proxy = Settings.get_proxy()
    
    if not proxy:
        print("❌ Error: SCRAPING_PROXY is not defined in your .env file!")
        return

    url = "https://piccoma.com"
    print(f"📡 Testing IPRoyal Proxy: {proxy}")
    print(f"🔗 Target: {url}")
    
    # --- TEST 1: NO IMPERSONATION ---
    # This mimics a basic 'curl' command (which we know works for you)
    print("\n[Test 1] Standard Request (No Fingerprint)")
    async with AsyncSession(proxy=proxy) as session:
        try:
            resp = await session.get(url, timeout=15)
            print(f"✅ Success! HTTP Status: {resp.status_code}")
        except Exception as e:
            print(f"❌ Failed: {e}")

    # --- TEST 2: CHROME 120 IMPERSONATION ---
    # This is exactly what the bot uses (impersonate='chrome120')
    print("\n[Test 2] Browser Impersonation (Chrome 120)")
    async with AsyncSession(impersonate="chrome120", proxy=proxy) as session:
        try:
            resp = await session.get(url, timeout=15)
            print(f"✅ Success! HTTP Status: {resp.status_code}")
        except Exception as e:
            # Look for 403 Forbidden specifically here
            print(f"❌ Failed: {e}")

    print("\n--- Diagnostic Complete ---")
    print("If Test 1 works but Test 2 fails, we need to change the bot's fingerprint.")

if __name__ == "__main__":
    asyncio.run(diagnosis())
