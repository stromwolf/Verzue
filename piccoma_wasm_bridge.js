import fs from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

const seed = process.argv[2];
if (!seed) process.exit(1);

try {
    // The WASM uses `new Function('return this')()` to find the global object.
    // In strict-mode ES modules this returns undefined, causing an `unreachable` trap.
    // Patch it before loading the module.
    const origFunction = globalThis.Function;
    globalThis.Function = new Proxy(origFunction, {
        construct(target, args) {
            const fn = new target(...args);
            return new Proxy(fn, {
                apply(target, thisArg, argList) {
                    const result = target.apply(thisArg, argList);
                    // `new Function('return this')()` returns undefined in ESM strict mode
                    return result === undefined ? globalThis : result;
                }
            });
        }
    });

    // Minimal btoa/atob for the WASM glue
    if (!globalThis.btoa) {
        globalThis.btoa = s => Buffer.from(s, 'binary').toString('base64');
        globalThis.atob = s => Buffer.from(s, 'base64').toString('binary');
    }

    // Mock Window so instanceof Window check passes
    class Window {}
    globalThis.Window = Window;
    // Make globalThis itself pass the instanceof check
    Object.setPrototypeOf(globalThis, Window.prototype);

    const wasmBuffer = fs.readFileSync(path.join(__dirname, 'diamond_bg.wasm'));

    const { initSync, dd } = await import('./diamond.js');
    initSync(wasmBuffer);

    const result = dd(seed);
    process.stdout.write(result);
    process.exit(0);
} catch (err) {
    process.stderr.write((err.stack || err.message) + '\n');
    process.exit(1);
}
