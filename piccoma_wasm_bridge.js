
import fs from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

async function main() {
    const seed = process.argv[2];
    if (!seed) process.exit(1);

    try {
        class Window {
            constructor() {
                this.window = this;
                this.self = this;
                this.globalThis = this;
                this.top = this;
                this.parent = this;
                this.location = { 
                    href: 'https://piccoma.com/web/viewer/s/71365/6214975',
                    origin: 'https://piccoma.com',
                    protocol: 'https:',
                    host: 'piccoma.com',
                    hostname: 'piccoma.com',
                    pathname: '/web/viewer/s/71365/6214975',
                    search: '', hash: ''
                };
                this.navigator = { 
                    userAgent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    platform: 'Win32', languages: ['ja-JP', 'ja'], onLine: true
                };
                this.document = { 
                    location: this.location,
                    createElement: () => ({ style: {}, setAttribute: () => {}, appendChild: () => {} }),
                    documentElement: {}, body: { style: {} }, readyState: 'complete',
                    URL: 'https://piccoma.com/web/viewer/s/71365/6214975'
                };
                this.screen = { width: 1920, height: 1080 };
                this.performance = { now: () => Date.now() };
            }
            btoa(s) { return Buffer.from(s, 'binary').toString('base64'); }
            atob(s) { return Buffer.from(s, 'base64').toString('binary'); }
        }

        const mockWin = new Window();
        globalThis.Window = Window;
        
        const props = {
            window: mockWin, self: mockWin, top: mockWin, parent: mockWin, globalThis: mockWin,
            location: mockWin.location, navigator: mockWin.navigator, document: mockWin.document,
            screen: mockWin.screen, performance: mockWin.performance, btoa: mockWin.btoa.bind(mockWin),
            atob: mockWin.atob.bind(mockWin)
        };
        for (const [k, v] of Object.entries(props)) {
            Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true });
        }
        global.global = global;

        // Trace and intercept imports
        const diamond = await import('./diamond.js');
        const originalInit = diamond.default;
        
        // We need to intercept the init call because it creates the imports
        // But the glue code is an ES module, we can't easily intercept __wbg_get_imports.
        // Instead, we will wrap the WebAssembly.Instance constructor temporarily.
        const OriginalInstance = WebAssembly.Instance;
        WebAssembly.Instance = class extends OriginalInstance {
            constructor(module, imports) {
                const tracedImports = { ...imports };
                if (tracedImports.wbg) {
                    for (const [key, fn] of Object.entries(tracedImports.wbg)) {
                        if (typeof fn === 'function') {
                            tracedImports.wbg[key] = function(...args) {
                                // process.stderr.write(`[CALL] ${key}(${args.join(', ')})\n`);
                                const ret = fn.apply(this, args);
                                // process.stderr.write(`[RET] ${key} -> ${ret}\n`);
                                return ret;
                            };
                        }
                    }
                }
                super(module, tracedImports);
            }
        };

        const wasmPath = path.join(__dirname, 'diamond_bg.wasm');
        const wasmBuffer = fs.readFileSync(wasmPath);
        
        await originalInit(wasmBuffer);
        
        const result = diamond.dd(seed);
        process.stdout.write(result);
        process.exit(0);
    } catch (err) {
        process.stderr.write(err.stack || err.message + "\n");
        process.exit(1);
    }
}

main();
