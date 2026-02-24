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
    @staticmethod
    def unscramble_jumptoon_v2(img_path, seed_string):
        try:
            with Image.open(img_path) as src:
                src = src.convert("RGB")
                w, h = src.size
                if w < 1000: return src.copy() # Resolution Guard

                split_w, padding_w, block_l = 20, 15, 51
                num_blocks = w // block_l
                
                # 1. Generate Shuffle
                d = list(range(num_blocks))
                lcg = JumptoonLCG(seed_string)
                for i in range(num_blocks, 1, -1):
                    target = lcg.next() % i
                    d[target], d[i - 1] = d[i - 1], d[target]

                # 2. Generate Inverse Map (THE TEST_3 LOGIC)
                p = [0] * num_blocks
                for original_pos, scrambled_pos in enumerate(d):
                    p[scrambled_pos] = original_pos

                # 3. Reconstruct
                canvas = Image.new("RGB", (num_blocks * split_w, h))
                for i in range(num_blocks):
                    src_x = p[i] * block_l + padding_w
                    dest_x = i * split_w
                    strip = src.crop((src_x, 0, src_x + split_w, h))
                    canvas.paste(strip, (dest_x, 0))
                return canvas
        except: return None

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