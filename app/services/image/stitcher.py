import os, json, logging
from PIL import Image
from app.services.image.optimizer import ImageOptimizer
from .smart_detector import find_best_cut
from app.core.logger import logger 
from config.settings import Settings

class ImageStitcher:
    @staticmethod
    def stitch_folder(input_dir: str, output_dir: str, max_slice_height: int = None, target_width: int = 720, episode_id=None, req_id=None, service_name="Jumptoon"):
        """
        Standard SmartStitch (All-in-Memory) implementation for maximum speed.
        
        Loads all images, combines them into one large canvas, and then slices
        them in one pass using vectorized detection.
        """
        if max_slice_height is None:
            max_slice_height = getattr(Settings, "STITCH_HEIGHT", 13000)
        
        ImageOptimizer.deduplicate(input_dir)
        if not os.path.exists(output_dir): os.makedirs(output_dir)

        # ─── STEP 1: PREPARE ALL IMAGES (Sequential) ───
        # Note: We load them all into memory for maximum speed as requested.
        prepared_images = []
        image_boundaries = []
        current_y = 0
        
        logger.info(f"[Stitcher] Mode: All-in-Memory (Sequential). Unscramble={'ON' if (episode_id) else 'OFF'}")
        files = sorted([f for f in os.listdir(input_dir) if f.lower().endswith(('.png', '.jpg', '.webp'))])
        
        for i, f in enumerate(files):
            if i % 10 == 0 or i == len(files) - 1:
                logger.debug(f"[Stitcher] Loading image {i+1}/{len(files)}...")
            
            path = os.path.join(input_dir, f)
            try:
                # We no longer use seeds_map or math.json
                seed = episode_id
                
                if seed:
                    # 🟢 SAFE UNSCRAMBLE: Pass seed. optimizer.py now handles already-clean images gracefully.
                    img = ImageOptimizer.unscramble_jumptoon_v2(path, seed)
                else:
                    img = Image.open(path).convert("RGB")
                
                if not img: continue
                
                img_w, img_h = img.size
                
                effective_w = ImageOptimizer.get_jumptoon_effective_width(img_w) if seed else img_w
                
                sw = target_width
                sh = int(img_h * (target_width / effective_w))
                
                if img.width != sw or img.height != sh:
                    img = img.resize((sw, sh), Image.Resampling.LANCZOS)
                
                prepared_images.append((img, current_y))
                current_y += sh
                image_boundaries.append(current_y)
            except Exception as e:
                logger.error(f"Failed to load {f}: {e}")
                continue

        if not prepared_images: return
        logger.info(f"[Stitcher] Loaded {len(prepared_images)} images into memory buffer.")

        # ─── STEP 2: COMBINE INTO LARGE CANVAS ───
        total_h = image_boundaries[-1] if image_boundaries else 0
        logger.info(f"[Stitcher] Creating large canvas: {target_width}x{total_h}px")
        combined_img = Image.new('RGB', (target_width, total_h), (255, 255, 255))
        
        for img, y in prepared_images:
            combined_img.paste(img, (0, y))
            img.close() # Free source image memory
        prepared_images.clear()

        # ─── STEP 3: DETECT & SLICE (One Pass) ───
        slice_idx = 0
        curr_y = 0
        buffer_zone = 4000 # Search window beyond target_height
        
        while curr_y < total_h:
            slice_idx += 1
            
            # If we're near the end, just take the rest
            if curr_y + max_slice_height >= total_h:
                cut_y = total_h
                cut_type = "Final"
            else:
                # Find best cut in the curr_y + target -> curr_y + target + buffer zone
                search_start = curr_y + max_slice_height
                search_end = min(search_start + buffer_zone, total_h)
                
                cut_y = find_best_cut(combined_img, search_start, search_end, 
                                     image_boundaries=image_boundaries)
                
                if cut_y is None or cut_y <= curr_y:
                    cut_y = search_start
                    cut_type = "Hard"
                else:
                    cut_type = f"Smart (+{cut_y - search_start}px)"

            # Crop and save
            slice_path = os.path.join(output_dir, f"{slice_idx:02d}.jpg")
            slice_img = combined_img.crop((0, curr_y, target_width, cut_y))
            # 🟢 Save as JPG with quality 95 as requested
            slice_img.save(slice_path, "JPEG", quality=95)
            
            # 🟢 Standardized Progress Bar
            if req_id:
                if 'progress' not in locals():
                    from app.core.logger import ProgressBar
                    progress = ProgressBar(req_id, "Stitching", service_name.capitalize(), total_h)
                
                progress.update(cut_y)

            logger.debug(f"[Stitcher] Saved slice {slice_idx:02d}.jpg: {target_width}x{cut_y-curr_y} ({cut_type})")
            slice_img.close()
            
            curr_y = cut_y
            if curr_y >= total_h: break

        if req_id and 'progress' in locals():
            progress.finish()

        combined_img.close()
        logger.info(f"[Stitcher] Done: {slice_idx} slices saved.")
        return output_dir
