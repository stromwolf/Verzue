
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

// Mock globals for diamond.js
class MockWindow {
    constructor() {
        this.window = this;
        this.self = this;
        this.location = {
            href: 'https://piccoma.com/web/viewer/s/71365/6214975'
        };
    }
    btoa(s) {
        return Buffer.from(s, 'binary').toString('base64');
    }
}

const mockWin = new MockWindow();
global.Window = MockWindow;
global.window = mockWin;
global.self = mockWin;
global.location = mockWin.location;
global.globalThis = mockWin;

// Mock WebAssembly.instantiateStreaming if needed (diamond.js uses fetch if input is string)
// But we can pass the buffer directly.

import init, { dd } from '../diamond.js';

async function run() {
    console.log("Loading WASM...");
    const wasmBuffer = fs.readFileSync(path.join(__dirname, '../diamond_bg.wasm'));
    console.log("Initializing WASM...");
    await init(wasmBuffer);
    
    const testSeed = process.argv[2] || "test_seed";
    console.log(`Running dd("${testSeed}")...`);
    const result = dd(testSeed);
    console.log(`Result: ${result}`);
}

run().catch(console.error);
