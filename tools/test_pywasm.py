
import pywasm
import os
import struct

def test_pywasm_final_api():
    print("Testing pywasm 2.2.1 Final Correct API")
    wasm_path = "Piccoma/diamond_bg.wasm"
    
    runtime = pywasm.Runtime()
    
    # helper to create host functions
    def add_mock(mod, name, args_types, ret_types, py_func):
        ftype = pywasm.core.FuncType(
            [getattr(pywasm.core.ValType, t)() for t in args_types],
            [getattr(pywasm.core.ValType, t)() for t in ret_types]
        )
        extern = runtime.allocate_func_host(ftype, py_func)
        if mod not in runtime.imports: runtime.imports[mod] = {}
        runtime.imports[mod][name] = extern

    # Mocks for wbg (Piccoma V30 requirements)
    # Most of these are strings or objects that we don't strictly need for dd()
    m = "wbg"
    add_mock(m, '__wbindgen_object_drop_ref', ['i32'], [], lambda x: None)
    add_mock(m, '__wbg_instanceof_Window_cde2416cf5126a72', ['i32'], ['i32'], lambda x: 0)
    add_mock(m, '__wbg_location_61ca61017633c753', ['i32'], ['i32'], lambda x: 0)
    add_mock(m, '__wbg_btoa_396932eb505ec155', ['i32', 'i32', 'i32'], [], lambda a,b,c: None)
    add_mock(m, '__wbg_newnoargs_ccdcae30fd002262', ['i32', 'i32'], ['i32'], lambda a,b: 0)
    add_mock(m, '__wbg_call_669127b9d730c650', ['i32', 'i32'], ['i32'], lambda a,b: 0)
    add_mock(m, '__wbindgen_string_get', ['i32', 'i32'], [], lambda a,b: None)
    add_mock(m, '__wbindgen_object_clone_ref', ['i32'], ['i32'], lambda x: x)
    add_mock(m, '__wbg_self_3fad056edded10bd', [], ['i32'], lambda: 0)
    add_mock(m, '__wbg_window_a4f46c98a61d4089', [], ['i32'], lambda: 0)
    add_mock(m, '__wbg_globalThis_17eff828815f7d84', [], ['i32'], lambda: 0)
    add_mock(m, '__wbg_global_46f939f6541643c5', [], ['i32'], lambda: 0)
    add_mock(m, '__wbindgen_is_undefined', ['i32'], ['i32'], lambda x: 0)
    add_mock(m, '__wbg_toString_2c5d5b612e8bdd61', ['i32'], ['i32'], lambda x: 0)
    add_mock(m, '__wbindgen_debug_string', ['i32', 'i32'], [], lambda a,b: None)
    add_mock(m, '__wbindgen_throw', ['i32', 'i32'], [], lambda a,b: None)
    
    try:
        instance = runtime.instance_from_file(wasm_path)
        print("✅ Successfully instantiated via runtime.instance_from_file.")
        
        # Test dd call
        test_seed = "test_seed_123"
        seed_bytes = test_seed.encode('utf-8')
        
        ptr = runtime.invocate(instance, '__wbindgen_malloc', [len(seed_bytes)])[0]
        memory = runtime.exported_memory(instance, 'memory').data
        memory[ptr : ptr + len(seed_bytes)] = seed_bytes
        
        ret_ptr = runtime.invocate(instance, '__wbindgen_add_to_stack_pointer', [-16])[0]
        runtime.invocate(instance, 'dd', [ret_ptr, ptr, len(seed_bytes)])
        
        res_mem = memory[ret_ptr : ret_ptr + 8]
        res_ptr, res_len = struct.unpack("<II", res_mem)
        
        final_seed = bytes(memory[res_ptr : res_ptr + res_len]).decode('utf-8')
        print(f"✅ dd call successful. Result seed: {final_seed}")
        
    except Exception as e:
        print(f"❌ pywasm final test failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_pywasm_final_api()
