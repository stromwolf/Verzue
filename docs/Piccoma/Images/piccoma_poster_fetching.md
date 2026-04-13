# Piccoma Poster Fetching Technical Overview

This document provides a detailed technical overview of how the Piccoma provider within the Verzue codebase identifies, extracts, and processes series posters for display in the Discord UI.

## Overview

The process of fetching a series poster involves navigating to the Piccoma product page, extracting the relevant image URL from the HTML structure, and applying a transformation to ensure the image displays reliably across different platforms (specifically Discord) without being blocked by hotlink protection.

## Technical Details

### 1. Target Endpoint
The primary source for series posters is the product details page:
- **URL Pattern**: `https://piccoma.com/web/product/{series_id}`
- **Authentication**: While some metadata is public, the provider typically uses an authenticated session (via `pksid` cookie) to ensure consistent access and avoid bot-detection triggers.

### 2. Extraction Logic
The provider uses **BeautifulSoup4** to parse the HTML and locates the poster using a series of CSS selectors designed to handle various versions of the Piccoma UI layout.

**Core Selectors:**
- `.PCM-productThumb_img`
- `.PCM-productThum_img`
- `.PCM-productThumb img`
- `.PCOM-productCover img`

The extraction is handled in `PiccomaProvider.get_series_info` within [provider.py](file:///e:/Code%20Files/Verzue/app/providers/platforms/piccoma/provider.py):

```python
thumb_img = soup.select_one('.PCM-productThumb_img, .PCM-productThum_img, .PCM-productThumb img, .PCOM-productCover img')
image_url = self._format_poster_url(thumb_img['src'] if thumb_img else None)
```

### 3. Poster URL Formatting
Extracted URLs are passed to `_format_poster_url` in [helpers.py](file:///e:/Code%20Files/Verzue/app/providers/platforms/piccoma/helpers.py). This method performs two critical functions:

#### A. Protocol Normalization
If the extracted URL starts with `//` (protocol-relative), it is prepended with `https:`.

#### B. Proxy Transformation (wsrv.nl)
Piccoma and KakaoCDN often implement aggressive hotlink protection or may not render correctly in Discord embeds. To solve this, Verzue proxies these images through `wsrv.nl` (Image-accelerating cache).

**Proxy Logic:**
If the domain contains `piccoma.com`, `piccoma-static.com`, `piccoma.jp`, or `kakaocdn.net`, the URL is transformed as follows:

- **Base Proxy**: `https://wsrv.nl/`
- **Parameters**: 
    - `w=600`: Sets the width to 600px for optimal resolution in Discord.
    - `fit=cover`: Ensures the image fills the dimensions without distortion.
- **Encoding**: The original URL is URL-encoded before being appended.

**Example Transformation:**
- *Original*: `https://piccoma.com/path/to/poster.jpg`
- *Transformed*: `https://wsrv.nl/?url=https%3A%2F%2Fpiccoma.com%2Fpath%2Fto%2Fposter.jpg&w=600&fit=cover`

### 4. Implementation Location

The logic is split between two main files in the Piccoma provider module:

| Component | Responsibility | File Path |
|:---|:---|:---|
| **Extraction** | Selecting the correct HTML element on the product page. | [provider.py](file:///e:/Code%20Files/Verzue/app/providers/platforms/piccoma/provider.py) |
| **Formatting** | Proxying and protocol normalization via `wsrv.nl`. | [helpers.py](file:///e:/Code%20Files/Verzue/app/providers/platforms/piccoma/helpers.py) |

## Discovery Flow Fallback
During the "Discovery" process (detecting new series), the provider also fetches posters from list items. It uses a similar heuristic approach in `PiccomaDiscovery.get_new_series_list`:

1. Identifies the `img` tag within the list item.
2. Calls `_format_poster_url` to apply the same proxy transformation.
3. If no image is found in the current item, it defaults to a `None` or "Unknown" state until scraped directly.

## Summary for Developers
To update the poster fetching logic:
- If Piccoma changes their HTML structure, update the selectors in `provider.py`.
- If images stop loading in Discord, check the `wsrv.nl` proxy configuration in `helpers.py`.
