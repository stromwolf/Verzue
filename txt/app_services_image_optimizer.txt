import os
import hashlib
import logging
from PIL import Image

logger = logging.getLogger("ImageOptimizer")

class JumptoonLCG:
    def __init__(self, seed_val):
        # 🟢 EXACT JS MATCH: (1664525 * state + 0x3c6ef35f) % 0x100000000
        self.state = int(seed_val) & 0xFFFFFFFF

    def next(self):
        self.state = (1664525 * self.state + 0x3C6EF35F) & 0xFFFFFFFF
        return self.state

class ImageOptimizer:
    # Jumptoon Scramble Configurations from JS (page-4cac8ba2cf272a09.js)
    CONFIGS = {
        "V1": {"splitWidth": 12, "paddingWidth": 3, "blankWidth": 3},
        "V2": {"splitWidth": 20, "paddingWidth": 15, "blankWidth": 1},
    }

    @staticmethod
    def calculate_jumptoon_seed(series_id, episode_number):
        """
        🟢 DEEP SEED STRATEGY: ih(`${es}:${er}`)
        Sums character code points of the string 'seriesId:episodeNumber'.
        """
        seed_str = f"{series_id}:{episode_number}"
        return sum(ord(c) for c in seed_str)

    @staticmethod
    def get_jumptoon_effective_width(img_w: int) -> int:
        """
        Calculates what the width of a Jumptoon image WOULD BE after unscrambling.
        Used for UI calculations before actual download.
        """
        conf = ImageOptimizer.CONFIGS["V2"]
        l_block = conf["splitWidth"] + conf["blankWidth"] + 2 * conf["paddingWidth"]
        
        if img_w >= 1000 and img_w % l_block == 0:
            num_blocks = img_w // l_block
            return num_blocks * conf["splitWidth"]
        return img_w

    @staticmethod
    def unscramble_jumptoon_v2(img_path, seed_val, version="V2", requested_width=None):
        """
        🟢 EXACT RECONSTRUCTION OF JS LOGIC
        Reconstructs images scrambled with the mesh-based padded atlas method.
        """
        try:
            with Image.open(img_path) as src:
                src = src.convert("RGB")
                raw_w, raw_h = src.size
                
                conf = ImageOptimizer.CONFIGS.get(version, ImageOptimizer.CONFIGS["V2"])
                a = conf["splitWidth"]
                n = conf["blankWidth"]
                o = conf["paddingWidth"]
                l_block = a + n + 2 * o # 51 for V2
                
                logger.log(5, f"🔍 [Jumptoon] Unscrambling: {os.path.basename(img_path)} | Raw: {raw_w}x{raw_h} | Seed: {seed_val} | Version: {version} | ReqWidth: {requested_width}")
                # Debug save: See what we are dealing with

                if not os.path.exists("downloads/debug_raw.png"):
                    src.save("downloads/debug_raw.png")

                # Safety check for non-scrambled or invalid images
                if raw_w < 1000 or (raw_w % l_block != 0):
                    logger.debug(f"🔍 [Jumptoon] Skipping unscramble: Width {raw_w} not suitable for {version} (mult of {l_block} required)")
                    return src.copy()

                num_blocks = raw_w // l_block
                c = int(requested_width) % a if requested_width else 0
                
                logger.log(5, f"⚙️ [Jumptoon] Params: num_blocks={num_blocks}, c={c}, l_block={l_block}")


                # 1. Fisher-Yates Shuffle Logic (d)
                # d contains the shuffled indices
                d = list(range(num_blocks))
                s_limit = num_blocks
                if c != 0:
                    s_limit -= 1 # Exclude remainder block from shuffle if present
                
                lcg = JumptoonLCG(seed_val)
                for i in range(s_limit, 1, -1):
                    target = lcg.next() % i
                    d[target], d[i - 1] = d[i - 1], d[target]

                # 2. Inversion Map (p)
                # Maps destination position back to source block index
                p = [0] * num_blocks
                for i, original_pos in enumerate(d):
                    p[original_pos] = i # p[d[e]] = e in JS

                # 3. Canvas Reconstruction
                final_w = num_blocks * a + c
                canvas = Image.new("RGB", (final_w, raw_h))
                
                for i in range(num_blocks):
                    # Strip source: p[i] * l_block + o (skip left padding)
                    # Strip destination: i * a
                    src_x = p[i] * l_block + o
                    dest_x = i * a
                    strip = src.crop((src_x, 0, src_x + a, raw_h))
                    canvas.paste(strip, (dest_x, 0))
                
                if c > 0:
                    # Handle the final remainder block
                    src_x = num_blocks * l_block + o
                    dest_x = num_blocks * a
                    strip = src.crop((src_x, 0, src_x + c, raw_h))
                    canvas.paste(strip, (dest_x, 0))
                    
                return canvas
        except Exception as e:
            logger.error(f"Unscramble failed: {e}")
            return None

    @staticmethod
    def deduplicate(input_dir):
        hashes = set()
        files = sorted([f for f in os.listdir(input_dir) if f.lower().endswith(('.png', '.webp', '.jpg', '.jpeg'))])
        for f in files:
            path = os.path.join(input_dir, f)
            try:
                with open(path, "rb") as img_file:
                    file_hash = hashlib.md5(img_file.read()).hexdigest()
                if file_hash in hashes: os.remove(path)
                else: hashes.add(file_hash)
            except: pass