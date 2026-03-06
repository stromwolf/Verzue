import os, json, logging
from PIL import Image
from .optimizer import ImageOptimizer
from .smart_detector import find_best_cut
from app.core.logger import logger 

class ImageStitcher:
    @staticmethod
    def stitch_folder(input_dir: str, output_dir: str, max_slice_height: int = 12000, target_width: int = 720, episode_id=None):
        """
        Standard SmartStitch (All-in-Memory) implementation for maximum speed.
        
        Loads all images, combines them into one large canvas, and then slices
        them in one pass using vectorized detection.
        """
        ImageOptimizer.deduplicate(input_dir)
        if not os.path.exists(output_dir): os.makedirs(output_dir)

        # ─── STEP 1: LOAD METADATA ───
        math_path = os.path.join(input_dir, "math.json")
        math_data_raw = []
        seeds_map = {}
        if os.path.exists(math_path):
            try:
                with open(math_path, 'r') as f:
                    math_data_raw = json.load(f)
                if math_data_raw and 'seed' in math_data_raw[0]:
                    seeds_map = {item['file']: item.get('seed') for item in math_data_raw}
            except Exception as e:
                logger.error(f"Error loading math.json: {e}")

        # ─── STEP 2: LOAD & PREPARE ALL IMAGES ───
        # Note: We load them all into memory for maximum speed as requested.
        prepared_images = []
        image_boundaries = []
        current_y = 0
        
        # Strategy A: Mecha coordinate-perfect
        if math_data_raw and 'y' in math_data_raw[0]:
            logger.info("[Stitcher] Mode: All-in-Memory (Coordinate-Perfect)")
            math_data_raw.sort(key=lambda x: x.get('y', 0))
            
            ref_width = 0
            for entry in math_data_raw:
                p = os.path.join(input_dir, entry['file'])
                if os.path.exists(p):
                    with Image.open(p) as img:
                        ref_width = img.width
                    break
            if not ref_width: ref_width = target_width
            scale_factor = target_width / ref_width
            start_y = math_data_raw[0]['y']
            
            for entry in math_data_raw:
                path = os.path.join(input_dir, entry['file'])
                if not os.path.exists(path): continue
                
                img = Image.open(path).convert("RGB")
                img_w, img_h = img.size
                sw = target_width
                sh = int(img_h * (target_width / img_w))
                abs_y = int((entry['y'] - start_y) * scale_factor)
                
                if sw != img_w or sh != img_h:
                    img = img.resize((sw, sh), Image.Resampling.LANCZOS)
                
                prepared_images.append((img, abs_y))
                image_boundaries.append(abs_y + sh)
        
        # Strategy B: Sequential (Jumptoon/Fallback)
        else:
            logger.info(f"[Stitcher] Mode: All-in-Memory (Sequential). Unscramble={'ON' if (episode_id or seeds_map) else 'OFF'}")
            files = sorted([f for f in os.listdir(input_dir) if f.lower().endswith(('.png', '.jpg', '.webp'))])
            
            for i, f in enumerate(files):
                if i % 10 == 0 or i == len(files) - 1:
                    logger.info(f"   [Stitcher] Loading image {i+1}/{len(files)}...")
                
                path = os.path.join(input_dir, f)
                try:
                    seed = seeds_map.get(f) or episode_id
                    if seed:
                        img = ImageOptimizer.unscramble_jumptoon_v2(path, seed)
                    else:
                        img = Image.open(path).convert("RGB")
                    
                    if not img: continue
                    
                    img_w, img_h = img.size
                    
                    # Jumptoon v2 width correction
                    effective_w = img_w
                    if seed and img_w >= 1000:
                        effective_w = (img_w // 51) * 20
                    
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

        # ─── STEP 3: COMBINE INTO LARGE CANVAS ───
        total_h = image_boundaries[-1] if image_boundaries else 0
        logger.info(f"   [Stitcher] Creating large canvas: {target_width}x{total_h}px")
        combined_img = Image.new('RGB', (target_width, total_h), (255, 255, 255))
        
        for img, y in prepared_images:
            combined_img.paste(img, (0, y))
            img.close() # Free source image memory
        prepared_images.clear()

        # ─── STEP 4: DETECT & SLICE (One Pass) ───
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
            slice_img.save(slice_path, "JPEG", quality=90, optimize=True)
            logger.info(f"   [Stitcher] Slice {slice_idx:02d}: {target_width}x{cut_y-curr_y} ({cut_type})")
            slice_img.close()
            
            curr_y = cut_y
            if curr_y >= total_h: break

        combined_img.close()
        logger.info(f"[Stitcher] Done: {slice_idx} slices saved.")
        return output_dir
