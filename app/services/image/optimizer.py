import os
import hashlib
import logging
from PIL import Image

logger = logging.getLogger("ImageOptimizer")

class JumptoonLCG:
    def __init__(self, seed_string):
        self.state = sum(ord(char) for char in str(seed_string)) & 0xFFFFFFFF

    def next(self):
        self.state = (1664525 * self.state + 1013904223) & 0xFFFFFFFF
        return self.state

class ImageOptimizer:
    # Jumptoon Scanning Constants
    JUMPTOON_BLOCK_L = 51
    JUMPTOON_SPLIT_W = 20
    JUMPTOON_PADDING_W = 15

    @staticmethod
    def get_jumptoon_effective_width(img_w: int) -> int:
        """
        Calculates what the width of a Jumptoon image WOULD BE after unscrambling.
        If the image is already narrow (clean), returns the original width.
        """
        # Scrambled images are always >= 1000px wide for Jumptoon v2.
        # They are also always a multiple of 51.
        if img_w >= 1000 and img_w % ImageOptimizer.JUMPTOON_BLOCK_L == 0:
            num_blocks = img_w // ImageOptimizer.JUMPTOON_BLOCK_L
            return num_blocks * ImageOptimizer.JUMPTOON_SPLIT_W
        return img_w

    @staticmethod
    def unscramble_jumptoon_v2(img_path, seed_string):
        try:
            with Image.open(img_path) as src:
                src = src.convert("RGB")
                w, h = src.size
                
                # Check if it's actually scrambled (width multiple of 51 and >= 1000)
                if w < 1000 or (w % ImageOptimizer.JUMPTOON_BLOCK_L != 0):
                    return src.copy()

                num_blocks = w // ImageOptimizer.JUMPTOON_BLOCK_L
                
                # 1. Generate Shuffle
                d = list(range(num_blocks))
                lcg = JumptoonLCG(seed_string)
                for i in range(num_blocks, 1, -1):
                    target = lcg.next() % i
                    d[target], d[i - 1] = d[i - 1], d[target]

                # 2. Generate Inverse Map
                p = [0] * num_blocks
                for original_pos, scrambled_pos in enumerate(d):
                    p[scrambled_pos] = original_pos

                # 3. Reconstruct
                canvas = Image.new("RGB", (num_blocks * ImageOptimizer.JUMPTOON_SPLIT_W, h))
                for i in range(num_blocks):
                    src_x = p[i] * ImageOptimizer.JUMPTOON_BLOCK_L + ImageOptimizer.JUMPTOON_PADDING_W
                    dest_x = i * ImageOptimizer.JUMPTOON_SPLIT_W
                    strip = src.crop((src_x, 0, src_x + ImageOptimizer.JUMPTOON_SPLIT_W, h))
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