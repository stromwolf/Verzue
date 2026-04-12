
import re

def extract_strings(file_path):
    with open(file_path, 'rb') as f:
        data = f.read()
    
    # Simple regex for ASCII strings >= 4 chars
    matches = re.findall(rb'[ -~]{4,}', data)
    for m in matches:
        try:
            print(m.decode())
        except:
            pass

extract_strings('diamond_bg.wasm')
