
import sys
import os
from unittest.mock import MagicMock

# Add project root to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Import the class but we will monkeypatch it for testing
from app.bot.common.view import UniversalDashboard

def test_logic():
    # We want to test UniversalDashboard.build_v2_payload logic 
    # but skip the __init__ which has background tasks
    
    # Mock instance
    view = MagicMock(spec=UniversalDashboard)
    
    # Shared state
    view.max_page = 1
    view.total_chapters = 10
    view.selected_indices = set()
    view.processing_mode = False
    view.all_chapters = []
    view.page = 1
    view.per_page = 10
    view.image_url = None
    view.req_id = "REQ-123"
    view.series_id = "S-123"
    view.color = 0x2b2d31
    view.phases = {}

    # Bind the real method to our mock
    view.build_v2_payload = UniversalDashboard.build_v2_payload.__get__(view, UniversalDashboard)

    # Case 1: No title override (titles match)
    view.title = "Solo Leveling"
    view.original_title = "Solo Leveling"
    payload = view.build_v2_payload()
    header = payload[0]['components'][0]['content'] # Since image_url is None
    print(f"Case 1 Header:\n{header}")
    assert "## Solo Leveling" in header
    assert "Solo Leveling\nSolo Leveling" not in header
    
    # Case 2: Title override (titles differ)
    view.title = "Custom English Name"
    view.original_title = "Original Korean Name"
    payload = view.build_v2_payload()
    header = payload[0]['components'][0]['content']
    print(f"\nCase 2 Header:\n{header}")
    assert "## Custom English Name\nOriginal Korean Name" in header
    assert "**Total Pages:** 1 | **Total Chapters:** 10" in header

    print("\n✅ Verification passed!")

if __name__ == "__main__":
    test_logic()
