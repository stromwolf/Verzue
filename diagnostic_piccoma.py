import os
import sys
import re
import itertools
import requests
import json
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

def run_diagnostic(url):
    print(f"🧪 Piccoma Matrix Diagnostic for: {url}")
    
    # 1. Fetch Page
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    
    # Load cookies
    cookies = {}
    cookie_path = "data/secrets/piccoma/cookies.json"
    if os.path.exists(cookie_path):
        with open(cookie_path, 'r') as f:
            c_list = json.load(f)
            for c in c_list: cookies[c['name']] = c['value']

    res = requests.get(url, cookies=cookies, headers=headers)
    
    # 2. Extract Manifest
    match = re.search(r'_pdata_\s*=\s*(.*?)\s*(?:var\s+|</script>|;)', res.text, re.DOTALL)
    if not match:
        # Try Next.js heuristic
        match = re.search(r'script id="__NEXT_DATA__".*?>({.*?})</script>', res.text)
        if match:
            data = json.loads(match.group(1))
            pdata = data.get('props', {}).get('pageProps', {}).get('initialState', {}).get('viewer', {}).get('pData', {})
            img_urls = [img['path'] for img in pdata.get('img', []) if 'path' in img]
        else:
            print("❌ Could not find manifest.")
            return
    else:
        content = match.group(1)
        img_urls = re.findall(r"['\"]?path['\"]?\s*:\s*['\"](.*?)['\"]", content)

    if not img_urls:
        print("❌ No image URLs found.")
        return

    # 3. Download ONE image for testing
    img_url = img_urls[0]
    img_res = requests.get(img_url, headers=headers)
    img_bytes = img_res.content
    
    # 4. Extract Seed Info
    path_only = img_url.split('?')[0].rstrip('/')
    chk_raw = path_only.split('/')[-2]
    expires = re.search(r'expires=(\d+)', img_url).group(1) if 'expires=' in img_url else ""
    
    print(f"🔑 Base Seed: {chk_raw}")
    print(f"⏳ Expires: {expires}")

    # 5. Render Matrix
    output_dir = "piccoma_diag_results"
    os.makedirs(output_dir, exist_ok=True)
    
    rots = [True, False]
    dds = [True, False]
    modes = ["scramble", "unscramble"]
    
    for rotate, use_dd, mode in itertools.product(rots, dds, modes):
        label = f"ROT-{rotate}_DD-{use_dd}_MODE-{mode}"
        
        # Seed Calculation
        chk = chk_raw
        if rotate and expires:
            for num in str(expires):
                if num.isdigit() and int(num) != 0:
                    shift = int(num)
                    chk = chk[-shift:] + chk[:-shift]
        
        final_seed = dd_transform(chk) if use_dd else chk
        
        try:
            canvas = Canvas(BytesIO(img_bytes), (50, 50), final_seed)
            out = canvas.export(mode=mode, format="png")
            with open(os.path.join(output_dir, f"{label}.png"), "wb") as f:
                f.write(out.getvalue())
            print(f"✅ Generated: {label}.png")
        except Exception as e:
            print(f"❌ Failed {label}: {e}")

    print(f"\n🎉 ALL DONE! Check the folder: {output_dir}")
    print("One of these images WILL be correctly unscrambled.")

if __name__ == "__main__":
    url = sys.argv[1] if len(sys.argv) > 1 else "https://piccoma.com/web/viewer/206094/6192831"
    run_diagnostic(url)
