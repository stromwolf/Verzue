import re
import os

repo_path = r'E:\Code Files\Verzue Bot'
piccoma_dir = os.path.join(repo_path, 'Piccoma')

files = [
    'piccoma.com_static_web_js_viewer_pcm_web_viewer_v30.0.js.txt',
    'piccoma.com_static_web_js_viewer_pcm_web_viewer_react_v30.0.js.txt',
    'piccoma.com_static_web_js_viewer__s.min.js.txt'
]

patterns = [
    r'window\.dd',
    r'\.dd\(',
    r'WebAssembly\.instantiate',
    r'fetch\(',
    r'\bwasm\b',
    r'\.wasm'
]

for filename in files:
    file_path = os.path.join(piccoma_dir, filename)
    if not os.path.exists(file_path):
        continue
    
    print(f'\n--- Searching {filename} ---')
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    for pattern in patterns:
        matches = list(re.finditer(pattern, content, re.IGNORECASE))
        if matches:
            print(f'Found {len(matches)} matches for "{pattern}"')
            for m in matches[:5]: # Show first 5 matches per pattern
                s = max(0, m.start()-100)
                e = min(len(content), m.end()+100)
                print(f'  @ {m.start()}: context: {content[s:e]}\n')
