import os
import sys
import time
import shutil
from PIL import Image

# Ensure the app directory is in the path so we can import our services
sys.path.append(os.getcwd())

try:
    from app.services.image.stitcher import ImageStitcher
    from app.core.logger import logger
    import logging
    # Set logging to INFO to see the stitcher's progress
    logging.basicConfig(level=logging.INFO, format='%(message)s')
except ImportError:
    print("Error: Could not find 'app' module. Please run this script from the project root directory.")
    sys.exit(1)

def run_test():
    print("=== Verzue SmartStitch Tester ===")
    
    # 1. Get Input Directory
    default_input = r"E:\Code Files\Verzue Bot\downloads\raw_だからあなたはお終いです_5"
    input_dir = input(f"Enter raw image directory path (default: {default_input}): ").strip() or default_input
    
    if not os.path.exists(input_dir):
        print(f"Error: Path does not exist: {input_dir}")
        return

    # 2. Get Episode ID (for unscrambling)
    default_seed = "S00183:5"
    seed = input(f"Enter Series ID:Episode ID for unscrambling (e.g. S00183:5, leave blank if not needed): ").strip()
    if not seed and "jumptoon" in input_dir.lower():
        seed = default_seed
        print(f"Using default seed for Jumptoon: {seed}")

    # 3. Setup Output Directory
    output_dir = os.path.join(os.getcwd(), "downloads", "manual_test_output")
    if os.path.exists(output_dir):
        shutil.rmtree(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    print(f"\n🚀 Starting Stitching...")
    print(f"   Input:  {input_dir}")
    print(f"   Output: {output_dir}")
    print(f"   Range:  12,000px -> 16,000px\n")

    start_time = time.time()
    try:
        # Run the stitcher
        # Note: target_width is 720 by default, max_slice_height is 12000
        ImageStitcher.stitch_folder(input_dir, output_dir, max_slice_height=12000, episode_id=seed)
        
        elapsed = time.time() - start_time
        print(f"\n✅ Stitching Complete in {elapsed:.2f}s!")
        
        # 4. Display Results
        files = sorted([f for f in os.listdir(output_dir) if f.endswith('.jpg')])
        print(f"\nGenerated {len(files)} slices:")
        for f in files:
            path = os.path.join(output_dir, f)
            with Image.open(path) as img:
                print(f"  - {f}: {img.size[0]}x{img.size[1]}")
        
        print(f"\nFiles are located in: {output_dir}")
        
    except Exception as e:
        print(f"\n❌ Error during stitching: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    run_test()
