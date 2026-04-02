import os
import sys
import itertools
from io import BytesIO
from PIL import Image

# Add root for imports
sys.path.append(os.getcwd())
from app.lib.pycasso import Canvas

def dd_transform(input_string):
    result_bytearray = bytearray()
    for index, byte in enumerate(bytes(input_string, 'utf-8')):
        if index < 3:
            byte = byte + (1 - 2 * (byte % 2))
        elif 2 < index < 6 or index == 8:
            pass
        elif index < 10:
            byte = byte + (1 - 2 * (byte % 2))
        elif 12 < index < 15 or index == 16:
            byte = byte + (1 - 2 * (byte % 2))
        elif index == len(input_string) - 1 or index == len(input_string) - 2:
            byte = byte + (1 - 2 * (byte % 2))
        result_bytearray.append(byte)
    return str(result_bytearray, 'utf-8')

def run_matrix(image_path, chk_raw, expires):
    print(f"🧪 Running 16-Mode MATRIX on: {image_path}")
    print(f"🔑 Base Seed: {chk_raw}")
    print(f"⏳ Expires: {expires}")

    output_dir = "matrix_offline_results"
    os.makedirs(output_dir, exist_ok=True)

    with open(image_path, "rb") as f:
        img_bytes = f.read()

    # Matrix Combinations
    for rotate_dir, use_dd, mode, exp_order in itertools.product(["R", "L", "N"], [True, False], ["S", "U"], ["N", "R"]):
        label = f"ROT-{rotate_dir}_DD-{use_dd}_MODE-{mode}_EXP-{exp_order}"
        
        # Calculate seed for this variant
        chk = chk_raw
        if rotate_dir != "N":
            exp_str = str(expires) if exp_order == "N" else str(expires)[::-1]
            for num in exp_str:
                if num.isdigit() and int(num) != 0:
                    shift = int(num)
                    if rotate_dir == "R":
                        chk = chk[-shift:] + chk[:-shift]
                    else: # Left Rotate
                        chk = chk[shift:] + chk[:shift]
        
        final_seed = dd_transform(chk) if use_dd else chk
        p_mode = "scramble" if mode == "S" else "unscramble"
        
        try:
            canvas = Canvas(BytesIO(img_bytes), (50, 50), final_seed)
            out = canvas.export(mode=p_mode, format="png").getvalue()
            with open(os.path.join(output_dir, f"{label}.png"), "wb") as f:
                f.write(out)
            print(f"✅ Generated: {label}.png")
        except Exception as e:
            print(f"❌ Failed {label}: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: python unscramble_matrix.py <image_path> <chk_raw> <expires>")
        print("Example: python unscramble_matrix.py downloads/test.jpg 5F7PYJHEMI0TVES@SOE199 1775066400")
        sys.exit(1)
    
    run_matrix(sys.argv[1], sys.argv[2], sys.argv[3])
