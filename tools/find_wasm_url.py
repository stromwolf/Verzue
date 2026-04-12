import re
import os

repo_path = r'E:\Code Files\Verzue Bot'
piccoma_dir = os.path.join(repo_path, 'Piccoma')

files = [
    'piccoma.com_static_web_js_viewer_pcm_web_viewer_v30.0.js.txt',
    'piccoma.com_static_web_js_viewer_pcm_web_viewer_react_v30.0.js.txt',
    'piccoma.com_static_web_js_viewer__s.min.js.txt'
]

# Look for patterns that fetch assets or WASM
fetch_patterns = [
    r'fetch\(',
    r'\.wasm',
    r'instantiate',
    r'WebAssembly',
    r'ArrayBuffer',
    r'Uint8Array',
    r'Module\['
]

for filename in files:
    file_path = os.path.join(piccoma_dir, filename)
    if not os.path.exists(file_path):
        continue
    
    print(f'\n--- Searching {filename} ---')
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    for pattern in fetch_patterns:
        matches = list(re.finditer(pattern, content, re.IGNORECASE))
        if matches:
            print(f'Found {len(matches)} matches for "{pattern}"')
            for m in matches[:10]: # Check context for first 10 matches
                s = max(0, m.start()-150)
                e = min(len(content), m.end()+150)
                print(f'  @ {m.start()}: context: {content[s:e]}\n')
