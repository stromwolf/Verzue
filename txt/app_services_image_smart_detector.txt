"""
Vectorized SmartStitch Pixel Comparison Detector

Optimized for speed using numpy vectorized operations.
Eliminates Python loops for row scanning, making it ~100x faster.
"""
import numpy as np
from PIL import Image

def find_best_cut(canvas: Image.Image, min_y: int, max_y: int,
                  image_boundaries: list[int] = None,
                  sensitivity: int = 70, scan_step: int = 1,
                  ignorable_pixels: int = 5) -> int | None:
    """
    Finds the absolute best row to slice the canvas at.
    """
    canvas_w, canvas_h = canvas.size
    min_y = max(0, min(min_y, canvas_h - 1))
    max_y = max(min_y + 1, min(max_y, canvas_h))
    
    # Crop window inclusive of max_y.
    window = canvas.crop((0, min_y, canvas_w, min(max_y + 1, canvas_h)))
    pixels = np.array(window.convert('L'), dtype=np.uint8)
    window_h = pixels.shape[0]
    window.close()

    if window_h < 1: return None

    # Step 1: Calculate ALL row scores at once (Mean Absolute Difference)
    # Using int16 for diff to prevent underflow wrap-around
    left, right = ignorable_pixels, (pixels.shape[1] - ignorable_pixels)
    if right <= left + 1: left, right = 0, pixels.shape[1]
    
    diffs = np.abs(np.diff(pixels[:, left:right].astype(np.int16), axis=1))
    scores = diffs.mean(axis=1) # shape: (window_h,)
    
    threshold = int(255 * (1 - (sensitivity / 100)))

    # Step 2: Find the absolute cleanest score available in the window
    min_s = scores.min()
    
    # Sensitivity fallback: if even the cleanest row is too noisy, fail
    if min_s > threshold:
        return None

    # Step 3: Identify all rows that share this best score (within epsilon)
    # These are our "Top Tier" candidates.
    top_tier_indices = np.where(scores <= min_s + 0.01)[0]
    
    # Step 4: Prioritize Image Boundaries
    # Filter boundaries to only those in the current search window
    valid_bounds = [b for b in (image_boundaries or []) if min_y <= b <= max_y]
    
    if valid_bounds:
        # Check which of our top-tier rows are at or near a boundary
        # We allow a ±1px tolerance for rounding
        top_tier_abs_y = top_tier_indices + min_y
        
        # Vectorized check: which top_tier_abs_y are in valid_bounds (with ±1)
        # For simplicity with small valid_bounds list, we can just check:
        best_boundary_y = -1
        for b in valid_bounds:
            # Is this boundary (or its neighbor) in the top tier?
            # We want the LARGEST boundary that is also Top Tier.
            match = top_tier_abs_y[(top_tier_abs_y >= b - 1) & (top_tier_abs_y <= b + 1)]
            if match.size > 0:
                best_boundary_y = max(best_boundary_y, b)
        
        if best_boundary_y != -1:
            return best_boundary_y

    # Step 5: Fallback to the largest Y among tied top-tier candidates
    # This maximizes slice height when no clear boundary is found.
    return int(top_tier_indices.max() + min_y)
