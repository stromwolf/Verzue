import os
import cv2
import numpy as np
from PIL import Image
from tqdm import tqdm

# --- Configuration ---
WATERMARK_BASE = "Newtoki Watermark"
ORIGINAL_BASE = "Original"

# Output folders for training
OUT_WM = "Train_Input_Watermarked"
OUT_ORIG = "Train_Output_Clean"

TARGET_WIDTH = 720
SLICE_HEIGHT = 1024

def get_images_in_dir(directory):
    files = [f for f in os.listdir(directory) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp'))]
    return sorted(files)

def stitch_images(image_paths):
    images = []
    total_height = 0
    max_width = 0
    
    for path in image_paths:
        img = cv2.imread(path)
        if img is None: continue
        images.append(img)
        total_height += img.shape[0]
        max_width = max(max_width, img.shape[1])
    
    if not images: return None
    
    # Create canvas
    canvas = np.zeros((total_height, max_width, 3), dtype=np.uint8)
    curr_y = 0
    for img in images:
        h, w = img.shape[:2]
        canvas[curr_y:curr_y+h, :w] = img
        curr_y += h
    return canvas

def process():
    os.makedirs(OUT_WM, exist_ok=True)
    os.makedirs(OUT_ORIG, exist_ok=True)

    # Find chapters (leaf folders containing images)
    chapters = []
    for root, dirs, files in os.walk(WATERMARK_BASE):
        has_images = any(f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')) for f in files)
        if has_images:
            # A folder is a 'leaf' if none of its subdirectories contain images
            is_leaf = True
            for d in dirs:
                subdir = os.path.join(root, d)
                if any(any(f.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')) for f in fs) for r, ds, fs in os.walk(subdir)):
                    is_leaf = False
                    break
            if is_leaf:
                chapters.append(root)

    print(f"[INFO] Found {len(chapters)} potential chapters (leaf folders).")

    pair_count = 0
    for wm_folder in chapters:
        rel_path = os.path.relpath(wm_folder, WATERMARK_BASE)
        orig_folder = os.path.join(ORIGINAL_BASE, rel_path)
        
        if not os.path.exists(orig_folder):
            # Try searching for the folder name if relative path doesn't match exactly
            folder_name = os.path.basename(wm_folder)
            found_orig = False
            for r, d, f in os.walk(ORIGINAL_BASE):
                if folder_name in r:
                    orig_folder = r
                    found_orig = True
                    break
            if not found_orig:
                print(f"[SKIP] No matching Original folder for {wm_folder}")
                continue

        # 1. Stitch Watermarked Pages
        wm_files = [os.path.join(wm_folder, f) for f in get_images_in_dir(wm_folder)]
        full_wm = stitch_images(wm_files)
        if full_wm is None: continue

        # 2. Stitch/Find Original (Assuming they might also be split or just one file)
        orig_files = [os.path.join(orig_folder, f) for f in get_images_in_dir(orig_folder)]
        full_orig = stitch_images(orig_files)
        if full_orig is None: continue

        # 3. Resize both to TARGET_WIDTH
        def resize_to_width(img, target_w):
            h, w = img.shape[:2]
            target_h = int(h * (target_w / w))
            return cv2.resize(img, (target_w, target_h), interpolation=cv2.INTER_AREA)

        full_wm_720 = resize_to_width(full_wm, TARGET_WIDTH)
        full_orig_720 = resize_to_width(full_orig, TARGET_WIDTH)

        # 4. Slice both
        min_h = min(full_wm_720.shape[0], full_orig_720.shape[0])
        num_slices = min_h // SLICE_HEIGHT
        
        chapter_slug = rel_path.replace(os.sep, "_").replace(" ", "_")
        
        for i in range(num_slices):
            y_start = i * SLICE_HEIGHT
            y_end = y_start + SLICE_HEIGHT
            
            slice_wm = full_wm_720[y_start:y_end, :]
            slice_orig = full_orig_720[y_start:y_end, :]
            
            fname = f"{chapter_slug}_part_{i:03d}.jpg"
            cv2.imwrite(os.path.join(OUT_WM, fname), slice_wm)
            cv2.imwrite(os.path.join(OUT_ORIG, fname), slice_orig)
            pair_count += 1

    print(f"[SUCCESS] Created {pair_count} aligned training pairs.")

if __name__ == "__main__":
    try:
        from google.colab import drive
        print("[INFO] Running in Colab. Mounting Drive...")
        drive.mount('/content/drive', force_remount=True)
        
        # Newtoki subfolder path provided by user
        DRIVE_ROOT = "/content/drive/My Drive/Newtoki"
        WATERMARK_BASE = os.path.join(DRIVE_ROOT, "Newtoki Watermark")
        ORIGINAL_BASE = os.path.join(DRIVE_ROOT, "Original")
        OUT_WM = os.path.join(DRIVE_ROOT, "Train_Input_Watermarked")
        OUT_ORIG = os.path.join(DRIVE_ROOT, "Train_Output_Clean")
        
        os.makedirs(OUT_WM, exist_ok=True)
        os.makedirs(OUT_ORIG, exist_ok=True)
    except: pass
    
    process()
