import os
import sys
import json
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

from app.services.group_manager import get_interested_groups
from config.settings import Settings

def test_get_interested_groups():
    # URL for "King's Sprint"
    url = "https://jumptoon.com/series/JT00132"
    
    print(f"Testing for URL: {url}")
    interested = get_interested_groups(url)
    
    print("Interested groups found:")
    for group, title in interested:
        print(f"- {group}: {title}")
    
    # Expected: Timeless ("King's Sprint") and Verzue ("King Sprint")
    found_groups = {g[0] for g in interested}
    
    if "Timeless" in found_groups and "Verzue" in found_groups:
        print("\n✅ Multi-group interest detection works!")
    else:
        print("\n❌ Multi-group interest detection failed!")
        print(f"Found groups: {found_groups}")
        sys.exit(1)

if __name__ == "__main__":
    Settings.ensure_dirs()
    test_get_interested_groups()
