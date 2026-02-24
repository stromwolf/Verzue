import os, json, logging
from PIL import Image
from .optimizer import ImageOptimizer
from app.core.logger import logger 

class ImageStitcher:
    @staticmethod
    def stitch_folder(input_dir, output_dir, strip_height=15000, episode_id=None):
        ImageOptimizer.deduplicate(input_dir)
        if not os.path.exists(output_dir): os.makedirs(output_dir)
        
        # Load unique seeds
        math_path = os.path.join(input_dir, "math.json")
        seeds_map = {}
        if os.path.exists(math_path):
            try:
                data = json.load(open(math_path))
                # Support both Mecha (y) and Jumptoon (seed) structures
                if data and 'seed' in data[0]:
                    seeds_map = {item['file']: item.get('seed') for item in data}
            except: pass

        math_data_mecha = []
        if os.path.exists(math_path):
            try:
                math_data = json.load(open(math_path))
                if math_data and 'y' in math_data[0]:
                    math_data_mecha = math_data
            except: pass

        processed_pages = []
        current_y = 0

        # --- STRATEGY A: COORDINATE-PERFECT (Mecha API) ---
        if math_data_mecha:
            logger.info("🧵 [Stitcher] Using coordinate-perfect math.")
            math_data_mecha.sort(key=lambda x: x.get('y', 0))
            for entry in math_data_mecha:
                path = os.path.join(input_dir, entry['file'])
                if os.path.exists(path):
                    img = Image.open(path).convert("RGB")
                    processed_pages.append({'img': img, 'y': entry['y']})

        # --- STRATEGY B: SEQUENTIAL (Jumptoon/Fallback) ---
        if not processed_pages:
            logger.info(f"🧵 [Stitcher] Sequential processing. Unscramble={'ON' if (episode_id or seeds_map) else 'OFF'}")
            raw_files = sorted([f for f in os.listdir(input_dir) if f.lower().endswith(('.png', '.webp', '.jpg'))])
            for f in raw_files:
                path = os.path.join(input_dir, f)
                # Priority: Page-Specific Seed > Global Seed (episode_id)
                seed = seeds_map.get(f) or episode_id
                try:
                    if seed:
                        img = ImageOptimizer.unscramble_jumptoon_v2(path, seed)
                    else:
                        img = Image.open(path).convert("RGB")
                    
                    if img:
                        processed_pages.append({'img': img, 'y': current_y})
                        current_y += img.height
                except: continue

        if not processed_pages: return

        # Assembly logic
        max_w = max(p['img'].width for p in processed_pages)
        current_strip, strip_idx, current_h = [], 1, 0
        start_y = processed_pages[0]['y']

        for p in processed_pages:
            img, rel_y = p['img'], p['y'] - start_y - (current_h)
            if current_h + img.height > strip_height and current_strip:
                ImageStitcher._save_strip(current_strip, max_w, current_h, output_dir, strip_idx)
                strip_idx += 1; current_strip, current_h = [], 0
                rel_y = p['y'] - start_y
            
            current_strip.append((img, current_h))
            current_h += img.height

        if current_strip:
            ImageStitcher._save_strip(current_strip, max_w, current_h, output_dir, strip_idx)

    @staticmethod
    def _save_strip(imgs, w, h, out, idx):
        canvas = Image.new('RGB', (w, h), (255, 255, 255))
        for img, y in imgs: canvas.paste(img, ((w - img.width)//2, y)); img.close()
        canvas.save(os.path.join(out, f"{idx:02d}.jpg"), "JPEG", quality=95, optimize=True, exif=b"")
