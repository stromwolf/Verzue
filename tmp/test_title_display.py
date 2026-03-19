
import sys
import os
from unittest.mock import MagicMock

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Mock things we don't want to load
from app.bot.common.view import UniversalDashboard

def test_title_display():
    bot = MagicMock()
    bot.task_queue.browser_service.inc_session = MagicMock()
    
    import asyncio
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    # Case 1: No Title Override
    ctx_data_1 = {
        'url': 'https://jumptoon.com/series/1',
        'title': 'Original Title',
        'original_title': 'Original Title',
        'chapters': [],
        'image_url': 'http://img.png',
        'series_id': 'S1',
        'req_id': 'REQ1',
        'user': MagicMock()
    }
    view1 = UniversalDashboard(bot, ctx_data_1, 'jumptoon')
    payload1 = view1.build_v2_payload()
    header1 = payload1[0]['components'][0]['components'][0]['content']
    print(f"CASE 1 (No Override):\n{header1}")
    assert "## Original Title" in header1
    assert "Original Title\nOriginal Title" not in header1
    
    # Case 2: Title Override Active
    ctx_data_2 = {
        'url': 'https://jumptoon.com/series/2',
        'title': 'Custom English Title',
        'original_title': 'Original Korean Title',
        'chapters': [],
        'image_url': 'http://img2.png',
        'series_id': 'S2',
        'req_id': 'REQ2',
        'user': MagicMock()
    }
    view2 = UniversalDashboard(bot, ctx_data_2, 'jumptoon')
    payload2 = view2.build_v2_payload()
    header2 = payload2[0]['components'][0]['components'][0]['content']
    print(f"\nCASE 2 (Override Active):\n{header2}")
    assert "## Custom English Title\nOriginal Korean Title" in header2
    assert "**Total Pages:**" in header2

    print("\n✅ Verification passed!")

if __name__ == "__main__":
    try:
        test_title_display()
    except Exception as e:
        print(f"❌ Verification failed: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
