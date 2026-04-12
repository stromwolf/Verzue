import re

with open(r'E:\Code Files\Verzue Bot\Piccoma\piccoma.com_static_web_js_viewer_pcm_web_viewer_v30.0.js.txt', 'r', encoding='utf-8') as f:
    content = f.read()

print(f"window.dd found at: {content.find('window.dd')}")
print(f"wasmError found at: {content.find('wasmError')}")
print(f"IMP_WASM found at: {content.find('IMP_WASM')}")
print(f"seedrandom found at: {content.find('seedrandom')}")
print(f"shuffleSeed found at: {content.find('shuffleSeed')}")
print(f"unscrambleImg found at: {content.find('unscrambleImg')}")

# Show contexts around those finds
for term in ['seedrandom', 'shuffleSeed', 'unscrambleImg', 'IMP_WASM']:
    idx = content.find(term)
    if idx >= 0:
        s = max(0, idx - 80)
        e = min(len(content), idx + len(term) + 80)
        print(f"\n--- {term} at {idx} ---")
        print(content[s:e])
