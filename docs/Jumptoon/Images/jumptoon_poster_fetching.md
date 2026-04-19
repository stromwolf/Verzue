# Jumptoon Series Poster Fetching

This document provides a technical overview of how the Jumptoon provider extracts and optimizes series posters from Jumptoon. The system is designed to provide high-resolution, high-quality images for use in the Verzue UI (Discord embeds and internal records).

## Overview

Jumptoon serves series posters primarily via their dedicated asset CDN (`assets.jumptoon.com`). Unlike some other providers, Jumptoon embeds metadata for its UI components directly into the HTML within Next.js hydration scripts and React Server Component (RSC) streams.

The provider uses a two-tiered extraction strategy to ensure high reliability:
1.  **Hydrated Metadata Extraction**: Extracting precise image URLs from embedded JSON objects.
2.  **Meta Tag Fallback**: Using standard `og:image` metadata as a backup.

---

## 1. Discovery Mechanisms

#### 🟢 Priority 1: JSON-Heuristic & Component Props
The provider searches for specific keys typically used by Jumptoon's "Hero" or "Thumbnail" components. It specifically prioritizes the `assets.jumptoon.com` domain and requires a `.png` extension to avoid capturing generic UI assets or episode thumbnails.

```python
# Regex pattern used in JumptoonProvider.get_series_info
# 1. Primary: Canonical square poster thumbnail
re.search(r'"seriesThumbnailV2ImageUrl"\s*:\s*"(https://assets\.jumptoon\.com/series/[^"]+\.png)"', clean_html)

# 2. Secondary Fallback: Hero banner image
re.search(r'"seriesHeroImageUrl"\s*:\s*"(https://assets\.jumptoon\.com/series/[^"]+\.png)"', clean_html)
```

#### 🟡 Priority 2: Meta Tag Fallback
If the JSON-based extraction fails, the system falls back to the Open Graph image tag. However, it explicitly ignores generic static assets (placeholders) hosted on `static.jumptoon.com`.

```python
# Fallback logic
og_match = re.search(r'<meta[^>]+(?:property|name)="og:image"[^>]+content="(https:[^"]+)"', html_content, re.I)
if og_match:
    candidate = og_match.group(1)
    if "static.jumptoon.com" not in candidate:
        image_url = candidate
```

### B. New Series Discovery (get_new_series_list)
Jumptoon uses Next.js App Router, which stores data in `__next_f` script tags as RSC strings. These strings are heavily escaped.

The provider:
1.  Identifies all unique Jumptoon Series IDs (e.g., `JT12345`) using `re.findall(r'JT\d+', html_content)`.
2.  Dynamically constructs a regex to find the `src` attribute associated with that specific ID.

```python
# Regex pattern used in JumptoonProvider.get_new_series_list
image_pattern = rf'\\"src\\":\\"(https://assets.jumptoon.com/series/{sid}/[^\\"]+\.(?:png|jpg|webp))\\"'
```

---

## 2. High-Resolution Optimization

Once a source URL is identified, the provider applies "S-Grade" optimizations to ensure maximum visual quality for the end-user.

### URL Normalization
Many extracted URLs come with existing query parameters (e.g., specific widths or formats used by the web browser). These are removed to get a clean base URL.

```python
if "?" in image_url: 
    image_url = image_url.split("?")[0]
```

### High-Res Parameter Injection
The provider appends modern image format and extreme resolution parameters. This instructs the Jumptoon CDN to serve the highest available quality, which is then cached by the Verzue system or Discord's media proxy.

| Parameter | Value | Description |
| :--- | :--- | :--- |
| `auto` | `avif-webp` | Requests modern, high-efficiency image formats with better transparency and color depth. |
| `width` | `3840` | Requests a 4K-ready width. The CDN will serve the largest available source image up to this width. |

**Final Optimized URL Example:**
`https://assets.jumptoon.com/series/JT12345/hero.webp?auto=avif-webp&width=3840`

---

## 3. Technical Implementation Details

The implementation is located in [jumptoon.py](file:///e:/Code%20Files/Verzue/app/providers/platforms/jumptoon.py).

### Key Extraction Methods
- `get_series_info(url, fast=False)`: Main entry point for series metadata.
- `get_new_series_list()`: Used for automated discovery of newly released series.

### Regular Expressions used for Image Detection
| Target | Regex |
| :--- | :--- |
| **JSON Props (Primary)** | `"seriesThumbnailV2ImageUrl"\s*:\s*"(https://assets\.jumptoon\.com/series/[^"]+\.png)"` |
| **JSON Props (Hero)** | `"seriesHeroImageUrl"\s*:\s*"(https://assets\.jumptoon\.com/series/[^"]+\.png)"` |
| **RSC Stream** | `\\"src\\":\\"(https://assets.jumptoon.com/series/{sid}/[^\\"]+\.(?:png|jpg|webp))\\"` |
| **OG Meta** | `<meta[^>]+(?:property|name)="og:image"[^>]+content="(https:[^"]+)"` |

---

## 4. Summary of Benefits

1.  **Visual Excellence**: By requesting `width=3840`, the system ensures posters look premium even on high-DPI displays.
2.  **Reliability**: Multi-tiered extraction (JSON -> Meta) handles changes in Jumptoon's frontend architecture.
3.  **Efficiency**: By targeting the core asset CDN (`assets.jumptoon.com`), the provider avoids low-quality placeholders or UI icons.
4.  **Consistency**: Automatically handles both standard series pages and the Next.js RSC-based "New" list.
