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
    # Jumptoon Scramble Configurations from JS (page-634888...)
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
        If the image is already narrow (clean), returns the original width.
        """
        conf = ImageOptimizer.CONFIGS["V2"]
        l_block = conf["splitWidth"] + conf["blankWidth"] + 2 * conf["paddingWidth"]
        
        # Scrambled images are always >= 1000px wide for Jumptoon v2.
        # They are also always a multiple of 51.
        if img_w >= 1000 and img_w % l_block == 0:
            num_blocks = img_w // l_block
            return num_blocks * conf["splitWidth"]
        return img_w

    @staticmethod
    def unscramble_jumptoon_v2(img_path, seed_val, version="V2", requested_width=None):
        """
        🟢 EXACT RECONSTRUCTION OF JS LOGIC
        - img_path: Path to the scrambled image
        - seed_val: Integer sum of code points (Deep Seed)
        - version: "V1" or "V2"
        - requested_width: The width from the manifest (used for remainder calc)
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
                
                # 🟢 SAFETY: If the image is already narrow or not a multiple of block size, 
                # return it as-is.
                if raw_w < 1000 or (raw_w % l_block != 0):
                    return src.copy()

                num_blocks = raw_w // l_block
                
                # If requested_width isn't provided, we can't do the remainder block 'c' 
                # exactly as JS does. However, for most Jumptoon images, it's 0.
                if requested_width is None:
                    # Best effort: assume no remainder if width is exactly mul of 51
                    c = 0 
                else:
                    c = int(requested_width) % a

                # 1. Fisher-Yates Shuffle (d)
                d = list(range(num_blocks))
                s_limit = num_blocks
                if c != 0:
                    s_limit -= 1 # Exclude remainder block from shuffle
                
                lcg = JumptoonLCG(seed_val)
                for i in range(s_limit, 1, -1):
                    target = lcg.next() % i
                    d[target], d[i - 1] = d[i - 1], d[target]

                # 2. Inversion Map (p)
                # JS: p = Array.from({length: u}); for (let e of d) p[d[e]] = e;
                p = [0] * num_blocks
                for original_pos, result_pos in enumerate(d):
                    p[result_pos] = original_pos

                # 3. Reconstruct
                # Final width should be num_blocks * a + c
                final_w = num_blocks * a + c
                canvas = Image.new("RGB", (final_w, raw_h))
                
                for i in range(num_blocks):
                    # Strip source: p[i] * l_block + o
                    # Strip dest: i * a
                    src_x = p[i] * l_block + o
                    dest_x = i * a
                    strip = src.crop((src_x, 0, src_x + a, raw_h))
                    canvas.paste(strip, (dest_x, 0))
                
                if c > 0:
                    # Remainder block: src = num_blocks * l_block + o, dest = num_blocks * a
                    src_x = num_blocks * l_block + o
                    dest_x = num_blocks * a
                    strip = src.crop((src_x, 0, src_x + c, raw_h))
                    canvas.paste(strip, (dest_x, 0))
                    
                src.close()
                return canvas
        except Exception as e:
            logger.error(f"Unscramble failed: {e}")
            return None
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