import re
import os

repo_path = r'E:\Code Files\Verzue Bot'
piccoma_dir = os.path.join(repo_path, 'Piccoma')

files = [
    'piccoma.com_static_web_js_viewer_pcm_web_viewer_v30.0.js.txt',
    'piccoma.com_static_web_js_viewer_pcm_web_viewer_react_v30.0.js.txt',
    'piccoma.com_static_web_js_viewer__s.min.js.txt'
]

wasm_patterns = [
    r'\.wasm',
    r'instantiate',
    r'WebAssembly',
    r'fetch',
    r'init'
]

for filename in files:
    file_path = os.path.join(piccoma_dir, filename)
    if not os.path.exists(file_path):
        print(f'File not found: {file_path}')
        continue
    
    print(f'\n--- Analyzing {filename} ---')
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    for pattern in wasm_patterns:
        matches = list(re.finditer(pattern, content, re.IGNORECASE))
        if matches:
            print(f'Found {len(matches)} matches for "{pattern}"')
        for m in matches:
            # Print a larger context to find the URL or logic
            s = max(0, m.start()-150)
            e = min(len(content), m.end()+150)
            print(f'Match for "{pattern}" at {m.start()}:')
            print(f'CONTEXT: {content[s:e]}\n')
