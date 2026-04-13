# Piccoma Image Unscrambling: Technical Overview

This document provides a comprehensive technical breakdown of how Piccoma's image scrambling and unscrambling process is implemented within the Verzue provider.

---

## 1. Overview
Piccoma uses a block-based shuffling technique to scramble its manga images. Instead of encrypting the pixel data itself, the image is divided into a grid of blocks (typically 50x50 pixels), which are then reordered according to a permutation derived from a per-image "seed."

The unscrambling process consists of four main phases:
1. **Manifest Extraction**: Identifying the image URLs and metadata.
2. **Seed Derivation**: Extracting and processing a base seed from the image URL.
3. **Diamond DRM Transform**: Using a WASM module to transform the base seed into a final decryption key.
4. **Reconstruction**: Dividing the scrambled image into blocks and remapping them to their original positions.

---

## 2. Manifest Extraction (pData)
The image manifest, internally referred to as `pData`, contains the list of scrambled image URLs. Piccoma's viewer page uses various structures depending on the platform (Web, Mobile) and region. Verzue uses a heuristic-based extraction method to find this data:

- **Next.js Data**: Searching for `initialState.viewer.pData` or `episodeDetail.manifest.images` inside the `script#__NEXT_DATA__` tag.
- **Legacy Globals**: Searching for the `var _pdata_` or `var pData` global variables using regex.
- **Deep Regex Scan**: A fallback that searches for any JSON objects containing `"path": "https://..."` keys that include keywords like `/seed` or `/img`.

---

## 3. Seed Derivation
Each image URL contains metadata required to derive the unscrambling seed.

### Base Seed Extraction
The "checksum" or base seed is extracted from the image URL:
- **Standard**: The second-to-last segment of the URL path.
  - *Example*: `.../WRDNKD40TMLSEIMSI@YMMD/i00001.jpg` -> `WRDNKD40TMLSEIMSI@YMMD`
- **France (FR)**: Extracted from the `q` query parameter.

### Seed Rotation
The checksum is then "shifted" using a right-rotation algorithm based on the `expires` timestamp found in the URL query string:
1. Sum all numeric digits in the `expires` string (e.g., `1712999999` -> `sum(1,7,1,2,...)`).
2. Calculate the shift amount: `shift = digit_sum % len(checksum)`.
3. Right-rotate the checksum by this amount.

---

## 4. Diamond DRM (WASM) Transformation
The rotated seed obtained above is not the final key. It must be transformed using the **Diamond DRM** module, which Piccoma delivers as a WebAssembly (WASM) binary (`diamond_bg.wasm`) and a JavaScript wrapper (`diamond.js`).

### The Node.js Bridge
Since the Diamond DRM module contains environment checks (e.g., verifying it's running in a browser with a `Window` object), Verzue executes it via a Node.js bridge. This bridge mocks the browser environment:
- **Environment Mocks**: Defines `globalThis.Window` and sets `globalThis.location` to `https://piccoma.com/`.
- **Function Proxy**: Implements a Proxy on `globalThis.Function` to intercept internal calls and ensure they return the expected global context.
- **WASM Initialization**: Calls `initSync(wasmBuffer)` followed by the transformation function `dd(seed)`.

The output of `dd(seed)` is the **Final Seed** used for the actual image reconstruction.

---

## 5. Image Reconstruction (Pycasso)
The final stage uses a library named `pycasso` to perform the visual reconstruction.

### Grid Slicing
The scrambled image is logically divided into a 50x50 pixel grid. Each grid cell is called a "part" or "slice."

### PRNG Shuffling
1. **Seeding**: A Pseudo-Random Number Generator (PRNG) is initialized using the **Final Seed**.
2. **Permutation Generation**: A list of indices representing every block in the grid is shuffled using a Fisher-Yates-style algorithm, driven by the PRNG.
   - For a 50x50 grid on an image, the number of parts is `ceil(width/50) * ceil(height/50)`.
3. **Mapping**: The shuffle algorithm provides a map: `Original Position <-> Scrambled Position`.

### Pixel Manipulation (Canvas)
Using the Python Imaging Library (PIL/Pillow), the process:
1. Creates a new blank "Canvas" of the same dimensions as the scrambled image.
2. Iterates through the shuffled indices.
3. Crops a 50x50 block from the **Scrambled Position** in the source image.
4. Pastes that block into its **Original Position** on the new Canvas.
5. Exports the final unscrambled image as a PNG/JPEG.

---

## 6. Technical Components
- **`app/providers/platforms/piccoma/drm.py`**: The coordinator. Handles manifest parsing, seed rotation, and managing the Node.js bridge.
- **`app/lib/pycasso/`**: The core image manipulation library.
  - `unscramble.py`: Handles high-level Canvas operations and block remapping.
  - `shuffleseed.py`: Implements the seed-based shuffling logic.
  - `prng.py`: Implements the `seedrandom` PRNG used by the shuffler.
- **`data/drm/`**: Local cache for the `diamond.js` and `diamond_bg.wasm` assets.
