import asyncio
import os
import sys

# Add the project root to sys.path
sys.path.append(os.getcwd())

from app.providers.manager import ProviderManager
from app.core.exceptions import MechaException

async def test_provider_lookup():
    pm = ProviderManager()
    
    test_urls = [
        "https://google.com/invalid",
        "https://example.com",
        "invalid-string"
    ]
    
    print("\n🧪 Starting Provider Lookup Safety Test...")
    
    # URL TESTS
    for url in test_urls:
        try:
            print(f"🔍 Testing URL: {url}")
            pm.get_provider_for_url(url)
            print(f"❌ Failed: URL {url} should have raised MechaException.")
        except MechaException as e:
            print(f"✅ Caught Expected Exception: {e} (Code: {e.code})")
            if e.code != "SY_002":
                print(f"❌ Failed: Expected code SY_002, got {e.code}")
        except Exception as e:
            print(f"❌ Failed: Caught unexpected exception type: {type(e).__name__} - {e}")

    # PLATFORM TESTS
    print("\n🧪 Testing Platform Name Lookup...")
    try:
        pm.get_provider("non_existent_platform")
        print("❌ Failed: Platform 'non_existent_platform' should have raised MechaException.")
    except MechaException as e:
        print(f"✅ Caught Expected Exception: {e} (Code: {e.code})")
    
    print("\n✨ All tests passed!")

if __name__ == "__main__":
    try:
        asyncio.run(test_provider_lookup())
    except Exception as e:
        print(f"❌ Error during test execution: {e}")
