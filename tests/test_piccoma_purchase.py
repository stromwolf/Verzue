
import asyncio
import logging
import sys
import os

# Add project root to path
sys.path.append(os.getcwd())

from app.scrapers.piccoma.api import PiccomaApiScraper
from app.models.chapter import TaskStatus

class MockTask:
    def __init__(self, url):
        self.url = url
        self.episode_id = url.split('/')[-1]
        self.chapter_str = "Test"
        self.req_id = "TEST-123"
        self.status = TaskStatus.QUEUED

logging.basicConfig(level=logging.INFO)

async def test_purchase():
    scraper = PiccomaApiScraper()
    # Replace with a real locked chapter URL for manual testing
    test_url = "https://piccoma.com/web/viewer/35913/1112845" 
    task = MockTask(test_url)
    
    print(f"Testing purchase for: {test_url}")
    success, cookies = await asyncio.to_thread(scraper.fast_purchase, task)
    
    if success:
        print("✅ Purchase successful!")
        # print(f"Cookies: {cookies}")
    else:
        print("❌ Purchase failed.")

if __name__ == "__main__":
    asyncio.run(test_purchase())
