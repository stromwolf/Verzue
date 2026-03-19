import asyncio
import sys
import os

# Add the project root to sys.path
sys.path.append(os.getcwd())

from app.providers.platforms.mecha import MechaProvider
from unittest.mock import AsyncMock, MagicMock, patch
from bs4 import BeautifulSoup

async def test_fetch_more_chapters():
    print("Testing fetch_more_chapters...")
    provider = MechaProvider()
    
    # Mock _get_authenticated_session
    mock_session = AsyncMock()
    provider._get_authenticated_session = AsyncMock(return_value=mock_session)
    
    # Mock session.get response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.text = '<html><li class="p-chapterList_item"><input name="chapter_ids[]" value="123"><dt class="p-chapterList_no">001話</dt><dd class="p-chapterList_name">Test Chapter</dd></li></html>'
    mock_session.get.return_value = mock_response
    
    seen_ids = set()
    chapters = await provider.fetch_more_chapters("https://mechacomic.jp/books/123", 1, seen_ids)
    
    print(f"Parsed Chapters: {chapters}")
    assert len(chapters) == 1
    assert chapters[0]['id'] == "123"
    assert "001話 Test Chapter" in chapters[0]['title']
    print("fetch_more_chapters test passed!")

async def test_check_chapter_access():
    print("\nTesting _check_chapter_access...")
    provider = MechaProvider()
    
    mock_session = AsyncMock()
    
    # Test case 1: Standard regex match
    mock_response1 = MagicMock()
    mock_response1.text = 'var viewer_url = "https://mechacomic.jp/viewer?contents_vertical=abc";'
    mock_session.get.return_value = mock_response1
    
    url = await provider._check_chapter_access(mock_session, "123")
    print(f"Access URL 1: {url}")
    assert url == "https://mechacomic.jp/viewer?contents_vertical=abc"
    
    # Test case 2: Fallback regex match with escaped slashes
    mock_response2 = MagicMock()
    mock_response2.text = '{"url": "https:\\/\\/mechacomic.jp\\/viewer?contents_vertical=xyz"}'
    mock_session.get.return_value = mock_response2
    
    url = await provider._check_chapter_access(mock_session, "123")
    print(f"Access URL 2: {url}")
    assert url == "https://mechacomic.jp/viewer?contents_vertical=xyz"
    
    print("_check_chapter_access tests passed!")

if __name__ == "__main__":
    asyncio.run(test_fetch_more_chapters())
    asyncio.run(test_check_chapter_access())
