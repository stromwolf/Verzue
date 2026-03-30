import asyncio
from curl_cffi import requests as curl_requests
import sys
import os

# Add root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import Settings

async def test_proxy():
    proxy = Settings.get_proxy()
    print(f"🔍 Testing Proxy: {proxy}")
    
    if not proxy:
        print("❌ No proxy configured in Settings!")
        return

    try:
        # We'll use a simple IP echo service
        async with curl_requests.AsyncSession(impersonate="chrome120", proxy=proxy) as s:
            res = await s.get("https://api.ipify.org?format=json", timeout=15)
            if res.status_code == 200:
                detected_ip = res.json().get("ip")
                print(f"✅ Success! Detected IP: {detected_ip}")
                if detected_ip in proxy:
                    print("🎯 Proxy is working correctly!")
                else:
                    print("⚠️ Proxy returned a different IP? (Could be residential rotation)")
            else:
                print(f"❌ Proxy returned status code: {res.status_code}")
                print(f"Response: {res.text}")
    except Exception as e:
        print(f"❌ Proxy Test Failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_proxy())
