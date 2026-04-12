
import sys
import os
import logging

# Mock necessary parts to test PiccomaWASM
sys.path.append(os.getcwd())
from app.providers.platforms.piccoma import PiccomaWASM

logging.basicConfig(level=logging.DEBUG)

def test_final_wasm_integration():
    wasm_path = "Piccoma/diamond_bg.wasm"
    if not os.path.exists(wasm_path):
        print(f"❌ WASM file missing at {wasm_path}. Skipping test.")
        return

    try:
        engine = PiccomaWASM(wasm_path)
        test_seed = "abcdefg123456"
        final_seed = engine.dd(test_seed)
        
        print(f"Original Seed: {test_seed}")
        print(f"Final Seed:    {final_seed}")
        
        if final_seed != test_seed and len(final_seed) > 0:
            print("✅ WASM Transformation detected and returned data.")
        else:
            print("❌ WASM Transformation returned same seed or empty data.")
            
    except Exception as e:
        print(f"❌ Integration test failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_final_wasm_integration()
