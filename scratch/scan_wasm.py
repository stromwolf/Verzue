
import struct

def scan_wasm_imports(file_path):
    with open(file_path, 'rb') as f:
        magic = f.read(4)
        if magic != b'\0asm':
            print("Not a WASM file")
            return
        
        version = f.read(4)
        print(f"WASM Version: {struct.unpack('<I', version)[0]}")
        
        while True:
            section_id_bytes = f.read(1)
            if not section_id_bytes:
                break
            section_id = section_id_bytes[0]
            
            # Read section size (LEB128)
            size = 0
            shift = 0
            while True:
                b = f.read(1)[0]
                size |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            
            if section_id == 2: # Import section
                print(f"Found Import Section (size {size})")
                # Parse imports...
                # For now just skip
            
            f.seek(size, 1)

print("Scanning diamond_bg.wasm imports...")
# scan_wasm_imports('diamond_bg.wasm')

# Actually, I'll just use a library if available or just use strings
with open('diamond_bg.wasm', 'rb') as f:
    data = f.read()
    import re
    # Look for module names (usually at the start of import entries)
    # Imports are usually (module, name) pairs
    matches = re.findall(rb'[a-z0-9_]{3,}', data)
    for m in matches[:100]:
        if b'wbg' in m or b'env' in m:
            print(m.decode())
