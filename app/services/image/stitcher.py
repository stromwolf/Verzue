import os, json, logging
from PIL import Image
from .optimizer import ImageOptimizer
from app.core.logger import logger 

class ImageStitcher:
    @staticmethod
    def stitch_folder(input_dir: str, output_dir: str, max_slice_height: int = 15000, target_width: int = 720, episode_id=None):
        """
        Ultra-Low RAM Stitcher with Mecha/Jumptoon support.
        Calculates canvas sizes mathematically before loading pixels into RAM.
        """
        ImageOptimizer.deduplicate(input_dir)
        if not os.path.exists(output_dir): os.makedirs(output_dir)

        # 🟢 STEP 1: LOAD METADATA
        math_path = os.path.join(input_dir, "math.json")
        math_data_raw = []
        seeds_map = {}
        if os.path.exists(math_path):
            try:
                with open(math_path, 'r') as f:
                    math_data_raw = json.load(f)
                # Check for Jumptoon seeds
                if math_data_raw and 'seed' in math_data_raw[0]:
                    seeds_map = {item['file']: item.get('seed') for item in math_data_raw}
            except Exception as e:
                logger.error(f"Error loading math.json: {e}")

        # 🟢 STEP 2: THE MATH PASS
        # Determine absolute positions and scaled sizes without loading pixels fully.
        processed_meta = []
        
        # Strategy A: Mecha coordinate-perfect
        if math_data_raw and 'y' in math_data_raw[0]:
            logger.info("🧵 [Stitcher] Math Pass: Using coordinate-perfect data.")
            math_data_raw.sort(key=lambda x: x.get('y', 0))
            
            # Find the reference width for scaling. 
            # We probe the first existing image to determine the scale factor.
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
                
                with Image.open(path) as img:
                    img_w, img_h = img.size
                
                sw = target_width
                sh = int(img_h * (target_width / img_w))
                abs_y = int((entry['y'] - start_y) * scale_factor)
                
                processed_meta.append({
                    "path": path,
                    "filename": entry['file'],
                    "abs_y": abs_y,
                    "scaled_w": sw,
                    "scaled_h": sh,
                    "unscramble": False
                })
        
        # Strategy B: Sequential (Jumptoon/Fallback)
        else:
            logger.info(f"🧵 [Stitcher] Math Pass: Sequential. Unscramble={'ON' if (episode_id or seeds_map) else 'OFF'}")
            files = sorted([f for f in os.listdir(input_dir) if f.lower().endswith(('.png', '.jpg', '.webp'))])
            current_abs_y = 0
            for f in files:
                path = os.path.join(input_dir, f)
                try:
                    with Image.open(path) as img:
                        img_w, img_h = img.size
                    
                    # For Jumptoon, width changes after unscrambling.
                    seed = seeds_map.get(f) or episode_id
                    
                    # Jumptoon v2 reduces width (block_l=51 -> split_w=20)
                    if seed and img_w >= 1000:
                        num_blocks = img_w // 51
                        img_w = num_blocks * 20
                    
                    sw = target_width
                    sh = int(img_h * (target_width / img_w))
                    
                    processed_meta.append({
                        "path": path,
                        "filename": f,
                        "abs_y": current_abs_y,
                        "scaled_w": sw,
                        "scaled_h": sh,
                        "seed": seed,
                        "unscramble": bool(seed)
                    })
                    current_abs_y += sh
                except Exception:
                    continue

        if not processed_meta: return

        # 🟢 STEP 3: PRE-CALCULATE SLICES
        slices = []
        current_slice_items = []
        slice_start_y = 0
        
        for item in processed_meta:
            item_bottom_rel = (item['abs_y'] + item['scaled_h']) - slice_start_y
            
            if item_bottom_rel > max_slice_height and current_slice_items:
                slice_h = max(i['abs_y'] + i['scaled_h'] for i in current_slice_items) - slice_start_y
                slices.append({
                    "items": current_slice_items,
                    "height": slice_h,
                    "start_y": slice_start_y
                })
                current_slice_items = [item]
                slice_start_y = item['abs_y']
            else:
                current_slice_items.append(item)

        if current_slice_items:
            slice_h = max(i['abs_y'] + i['scaled_h'] for i in current_slice_items) - slice_start_y
            slices.append({
                "items": current_slice_items,
                "height": slice_h,
                "start_y": slice_start_y
            })

        # 🟢 STEP 4: LAZY RENDER PASS
        for idx, s_data in enumerate(slices):
            slice_filename = f"{idx+1:02d}.jpg"
            slice_path = os.path.join(output_dir, slice_filename)
            
            canvas = Image.new('RGB', (target_width, s_data['height']), (255, 255, 255))
            
            for item in s_data['items']:
                try:
                    # Load & Process Image
                    if item.get('unscramble'):
                        img = ImageOptimizer.unscramble_jumptoon_v2(item['path'], item['seed'])
                    else:
                        img = Image.open(item['path']).convert("RGB")
                    
                    if not img: continue
                    
                    # Resize to target width while keeping ratio
                    resized = img.resize((item['scaled_w'], item['scaled_h']), Image.Resampling.LANCZOS)
                    
                    # Paste relative to slice start
                    paste_y = item['abs_y'] - s_data['start_y']
                    canvas.paste(resized, (0, paste_y))
                    
                    img.close()
                    del resized
                except Exception as e:
                    logger.error(f"Failed to process image {item['filename']}: {e}")

            # Save Slice
            canvas.save(slice_path, "JPEG", quality=90, optimize=True)
            canvas.close()
            del canvas
            logger.info(f"   🧵 Saved Slice {slice_filename} ({target_width}x{s_data['height']})")

        return output_dir
