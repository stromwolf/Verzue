# Piccoma Poster Transformation for Discord UI

This document provides a technical overview of how the Piccoma provider extracts, transforms, and optimizes series posters for high-reliability display within the Discord UI components used by the Verzue notification system.

## Overview

Piccoma serves series thumbnails (posters) via multiple CDNs (`piccoma.com`, `piccoma-static.com`, `kakaocdn.net`). These assets are protected by anti-hotlinking measures (Referer checks) and can often fail to render in Discord embeds or require specific dimensions for the "Premium V2" layout.

To ensure consistent and high-quality visuals, the Piccoma provider utilizes a **proxy-based transformation layer** before passing URLs to the Discord notification builder.

## The Transformation Workflow

The core logic resides in `PiccomaHelpers._format_poster_url`, which acts as a unified entry point for all poster URLs extracted during discovery or series scanning.

### 1. Data Extraction (Source)

The provider extracts the raw poster URL from two primary sources:
- **Direct HTML Scrape**: Using BeautifulSoup selectors like `.PCM-productThumb_img` or `.PCOM-productCover img`.
- **API Response**: Extracting from `NEXT_DATA` JSON structures or internal theme APIs (e.g., fields like `img`, `image`, or `cover_x1`).

### 2. URL Refinement

Before transformation, the utility performs basic cleanup:
- Ensures the URL is absolute (prefixes `//` with `https:`).
- Filters for known Piccoma/Kakao CDN domains.

### 3. Proxy Transformation (`wsrv.nl`)

If the URL belongs to a restricted CDN, it is proxied through **wsrv.nl**, an open-source image cache and resizer. This provides several critical benefits:

- **Referer Bypass**: Discord's image proxy fails when CDNs block requests without a `piccoma.com` Referer. `wsrv.nl` acts as an intermediary that Discord can reliably cache.
- **Latency Reduction**: `wsrv.nl` uses worldwide caching, reducing the time it takes for Discord to "pull" the image.
- **Dynamic Resizing**: The image is requested with specific parameters to fit the Discord UI.

```python
# Implementation in app/providers/platforms/piccoma/helpers.py
def _format_poster_url(self, url: str | None) -> str | None:
    if not url: return None
    if url.startswith('//'): url = 'https:' + url
    
    # Consistent Proxying for Discord Embed reliability
    if any(domain in url for domain in ['piccoma.com', 'piccoma-static.com', 'piccoma.jp', 'kakaocdn.net']):
        return f"https://wsrv.nl/?url={urllib.parse.quote(url)}&w=600&fit=cover"
    return url
```

## Optimization Parameters

The transformation applies specific query parameters to the proxy URL:

| Parameter | Value | Description |
| :--- | :--- | :--- |
| `w` | `600` | Sets the width to 600 pixels. This is the optimal width for Discord's large media components. |
| `fit` | `cover` | Ensures the image fills the 600px width properly, cropping excess height if necessary to maintain a clean appearance. |
| `url` | (encoded) | The original CDN URL is percent-encoded to prevent breaking the proxy request. |

## Discord UI Integration

The resulting "viewing poster" URL is passed to the `notification_builder.py`, where it is placed into a **Media Gallery (Type 12)** component.

### Layout Implementation
In the "Premium V2" Discord layout, posters are treated as primary visual anchors. The system uses a specific component structure:

```json
{
    "type": 12, // Media Gallery Component
    "items": [
        {
            "media": {
                "url": "https://wsrv.nl/?url=...&w=600&fit=cover"
            }
        }
    ]
}
```

By setting the width to 600px via the proxy, we ensure that the image remains crisp and fills the horizontal space of the Discord message container, creating a premium "viewing" experience for the user.

## Summary of Benefits

1.  **Reliability**: Elimination of "broken image" icons in Discord embeds due to CDN blocking.
2.  **Consistency**: Uniform width across all Piccoma series notifications regardless of source image size.
3.  **Speed**: Faster image rendering in the Discord client thanks to the proxy's edge caching.
4.  **Resilience**: The stateless nature of the transformation means no local image processing or storage is required on the VPS.
