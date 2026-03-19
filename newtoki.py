import asyncio
import os
import re
import sys
import argparse
from urllib.parse import urlparse

# Force UTF-8 for terminal output
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except (AttributeError, Exception):
        pass

from bs4 import BeautifulSoup
from curl_cffi import requests

class NewtokiScraper:
    def __init__(self, concurrency=5, cookies=None, chapter_delay=1.0, image_delay=0.1):
        # Store delay settings
        self.chapter_delay = chapter_delay
        self.image_delay = image_delay

        # Using chrome120 for better compliance with modern sites
        self.session = requests.AsyncSession(impersonate="chrome120")
        self.impersonated_ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        
        # Load cookies from file if not provided as argument
        if not cookies:
            cookie_path = os.path.join("newtoki", "cookies.txt")
            if os.path.exists(cookie_path):
                with open(cookie_path, "r", encoding="utf-8") as f:
                    lines = [line.strip() for line in f.readlines() if line.strip() and not line.strip().startswith('#')]
                    if lines:
                        cookies = "; ".join(lines)
                        print(f"[INFO] Loaded cookies from {cookie_path}")
        
        # Load custom User-Agent if file exists
        ua_path = os.path.join("newtoki", "user_agent.txt")
        custom_ua_found = False
        if os.path.exists(ua_path):
            with open(ua_path, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f.readlines() if line.strip() and not line.strip().startswith('#')]
                if lines:
                    custom_ua = lines[0]
                    # Robustness: strip "User-Agent: " prefix if the user included it
                    if custom_ua.lower().startswith("user-agent: "):
                        custom_ua = custom_ua[12:].strip()
                        
                    self.session.headers.update({"User-Agent": custom_ua})
                    print(f"[INFO] Loaded custom User-Agent from {ua_path}")
                    custom_ua_found = True
        
        if not custom_ua_found:
            print(f"[WARNING] No custom User-Agent found in {ua_path}!")
            print(f"[INFO] Using default User-Agent: {self.session.headers.get('User-Agent')}")

        if cookies:
            # Clean up cookie string (strip "Cookie: " prefix if present)
            if cookies.lower().startswith("cookie: "):
                cookies = cookies[8:].strip()
                
            # Check for critical cf_clearance cookie
            if "cf_clearance" not in cookies:
                print("[WARNING] The 'cf_clearance' cookie is missing from your cookies.txt!")
            if "PHPSESSID" not in cookies:
                print("[NOTE] 'PHPSESSID' is missing. You might need to copy ALL cookies from your browser.")
            
            # Check for Newtoki hash session cookie (usually alphanumeric 32 chars)
            if not any(len(k) == 32 and k.isalnum() for k in cookies.split(';')):
                print("[WARNING] Site-specific session cookie (e.g. e1192af...) is missing!")
                print("[TIP] In the Network tab, copy THE ENTIRE 'Cookie' header value.")

            # Parse cookies string (key1=val1; key2=val2)
            cookie_dict = {}
            for cookie in cookies.split(';'):
                if '=' in cookie:
                    k, v = cookie.strip().split('=', 1)
                    cookie_dict[k] = v
            self.session.cookies.update(cookie_dict)
            
        # Parse UA version to sync Sec-Ch-Ua
        active_ua = self.session.headers.get("User-Agent")
        ua_version = "120"
        if active_ua and "Chrome/" in active_ua:
            match = re.search(r"Chrome/(\d+)", active_ua)
            if match:
                ua_version = match.group(1)

        # Only update the necessary headers, let impersonate handle the rest of the fingerprint
        self.session.headers.update({
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Cache-Control": "max-age=0",
            "Sec-Ch-Ua": f'"Not_A Brand";v="8", "Chromium";v="{ua_version}", "Google Chrome";v="{ua_version}"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Windows"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
            "DNT": "1"
        })
        self.semaphore = asyncio.Semaphore(concurrency)

    async def close(self):
        await self.session.close()

    def decode_html_data(self, hex_str):
        try:
            parts = [p for p in hex_str.split('.') if p.strip()]
            return "".join([chr(int(p, 16)) for p in parts])
        except Exception as e:
            print(f"Error decoding hex: {e}")
            return ""

    async def get_series_info(self, url, debug=False):
        print(f"[INFO] Fetching series info from {url}...")
        domain = "{0.scheme}://{0.netloc}".format(urlparse(url))
        try:
            response = await self.session.get(url, headers={"Referer": domain}, timeout=30)
        except Exception as e:
            print(f"[ERROR] Request failed: {e}")
            if "curl: (43)" in str(e):
                print("[TIP] This is often a TLS/HTTP2 error. Attempting fallback...")
                # Try creating a new session without impersonation as fallback
                fallback_session = requests.AsyncSession()
                fallback_session.cookies.update(self.session.cookies)
                fallback_session.headers.update(self.session.headers)
                try:
                    response = await fallback_session.get(url, headers={"Referer": domain}, timeout=30)
                    await fallback_session.close()
                except Exception as e2:
                    print(f"[ERROR] Fallback also failed: {e2}")
                    await fallback_session.close()
                    return None
            else:
                return None
        if response.status_code != 200:
            print(f"[ERROR] Failed to fetch series info. Status: {response.status_code}")
            if debug:
                print(f"[DEBUG] Active Cookies: {self.session.cookies.get_dict()}")
                print(f"[DEBUG] Active Headers: {self.session.headers}")
                debug_file = "debug_output.html"
                with open(debug_file, "w", encoding="utf-8") as f:
                    f.write(response.text)
                print(f"[DEBUG] Blocked page content saved to {debug_file}. Open it in your browser to see what happened.")
            
            if response.status_code == 403:
                print("[TIP] You may need to refresh your clearance cookie or ensure your User-Agent matches.")
                if debug:
                    print(f"[DEBUG] Active Cookies: {self.session.cookies.get_dict()}")
                    print(f"[DEBUG] Active Headers: {self.session.headers}")
            return None

        html = response.text
        soup = BeautifulSoup(html, 'html.parser')
        
        # Title
        title = "Unknown"
        meta_title = soup.find('meta', property='og:title')
        if meta_title:
            title = meta_title['content'].split('>')[0].strip()
        
        # Thumbnail
        thumb = ""
        meta_thumb = soup.find('meta', property='og:image')
        if meta_thumb:
            thumb = meta_thumb['content']
            
        # Chapters
        chapters = []
        # Support both the older structure and the one the user provided
        chapter_items = soup.select('ul.list-body li.list-item')
        for item in chapter_items:
            link = item.find('a', class_='item-subject')
            if link:
                full_text = link.get_text(strip=True)
                # Filter out comment counts if present (usually inside <span> or <b>)
                for unwanted in link.find_all(['span', 'b']):
                    unwanted.decompose()
                
                ch_title = link.get_text(strip=True)
                ch_url = link['href']
                ch_id = ch_url.split('/')[-1].split('?')[0]
                
                # Get index: try data-index first, then wr-num class
                index = item.get('data-index')
                if not index:
                    num_div = item.find('div', class_='wr-num')
                    if num_div:
                        index = num_div.get_text(strip=True)
                
                chapters.append({
                    'title': ch_title,
                    'url': ch_url,
                    'id': ch_id,
                    'index': index or '?'
                })
        
        # Newtoki usually lists newest first, we reverse to keep chronological order for range selection
        chapters.reverse()
        
        return {
            'title': title,
            'thumbnail': thumb,
            'chapters': chapters
        }

    async def get_chapter_images(self, url, referer=None, debug=False):
        print(f"[INFO] Fetching chapter images from {url}...")
        # Use provided referer or domain fallback
        ref_header = referer if referer else "{0.scheme}://{0.netloc}".format(urlparse(url))
        
        # When moving from series to chapter, site is same-origin
        headers = {
            "Referer": ref_header,
            "Sec-Fetch-Site": "same-origin"
        }
        
        try:
            response = await self.session.get(url, headers=headers, timeout=30)
        except Exception as e:
            print(f"[ERROR] Failed to fetch chapter page: {e}")
            return []

        if response.status_code != 200:
            print(f"[ERROR] Failed to fetch chapter. Status: {response.status_code}")
            if debug:
                print(f"[DEBUG] Active Cookies: {self.session.cookies.get_dict()}")
                print(f"[DEBUG] Active Headers: {self.session.headers}")
                debug_file = "debug_chapter_output.html"
                with open(debug_file, "w", encoding="utf-8") as f:
                    f.write(response.text)
                print(f"[DEBUG] Blocked chapter content saved to {debug_file}.")
            return []

        html = response.text
        # Handle both single and double quotes
        hex_blocks = re.findall(r"html_data\s*\+?=\s*['\"]([^'\"]+)['\"];", html)
        full_hex = "".join(hex_blocks)
        
        if not full_hex:
            if debug:
                print("[DEBUG] No html_data found via regex. Content preview:")
                print(html[:1000])
            
            if "captcha" in html.lower() or "캡챠" in html:
                print("[ERROR] Access Denied: You are being blocked by a Captcha.")
                print("[TIP] Open a chapter in your browser, solve the Captcha, and refresh your cookies.txt.")
            else:
                print("[ERROR] No image data found in chapter page.")
            return []

        decoded_html = self.decode_html_data(full_hex)
        if not decoded_html:
            print(f"[ERROR] Failed to decode html_data for {url}")
            return []

        if debug:
            print(f"[DEBUG] Decoded HTML preview: {decoded_html[:500]}")

        soup = BeautifulSoup(decoded_html, 'html.parser')
        
        images = []
        for img in soup.find_all('img'):
            src = ""
            # Newtoki uses randomized data- attributes for the real image URL
            for attr, value in img.attrs.items():
                if attr.startswith('data-') and isinstance(value, str) and value.startswith('http'):
                    src = value
                    break
            
            if not src:
                src = img.get('src', '')
            
            if src and src.startswith('http') and 'loading-image' not in src:
                images.append(src)
        
        if debug:
            print(f"[DEBUG] Found {len(images)} images.")
                
        return images

    async def download_image(self, url, path):
        async with self.semaphore:
            if self.image_delay > 0:
                await asyncio.sleep(self.image_delay)
            try:
                # Add referer for images to avoid hotlink protection
                domain = "{0.scheme}://{0.netloc}".format(urlparse(url))
                response = await self.session.get(url, headers={"Referer": domain})
                if response.status_code == 200:
                    with open(path, 'wb') as f:
                        f.write(response.content)
                    return True
                else:
                    print(f"[ERROR] Failed to download {url}. Status: {response.status_code}")
            except Exception as e:
                print(f"[ERROR] Exception downloading {url}: {e}")
            return False

    async def download_chapter(self, chapter, base_folder, series_url=None, debug=False):
        if self.chapter_delay > 0:
            import random
            jitter = random.uniform(0.5, 1.5)
            wait = self.chapter_delay * jitter
            print(f"[INFO] Waiting {wait:.1f}s (Rate Limit/Human Jitter)...")
            await asyncio.sleep(wait)
            
        print(f"\n[INFO] Downloading chapter: {chapter['title']}")
        # Use series URL as referer for chapter pages
        referer = series_url if series_url else "{0.scheme}://{0.netloc}".format(urlparse(chapter['url']))
        images = await self.get_chapter_images(chapter['url'], referer=referer, debug=debug)
        if not images:
            print(f"[WARNING] No images found for {chapter['title']}")
            return

        # Sanitize folder name
        safe_title = "".join([c for c in chapter['title'] if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
        ch_folder = os.path.join(base_folder, safe_title)
        os.makedirs(ch_folder, exist_ok=True)

        tasks = []
        for i, img_url in enumerate(images):
            ext = img_url.split('.')[-1].split('?')[0]
            if len(ext) > 4 or not ext: ext = 'jpg' 
            filename = f"{i+1:03d}.{ext}"
            filepath = os.path.join(ch_folder, filename)
            tasks.append(self.download_image(img_url, filepath))

        results = await asyncio.gather(*tasks)
        success_count = sum(1 for r in results if r)
        print(f"[INFO] Finished {chapter['title']}: {success_count}/{len(images)} images downloaded.")

async def main():
    parser = argparse.ArgumentParser(description="Standalone Newtoki Scraper")
    parser.add_argument("url", help="Series URL")
    parser.add_argument("--start", type=int, help="Start chapter index (1-based)")
    parser.add_argument("--end", type=int, help="End chapter index (1-based)")
    parser.add_argument("--concurrency", type=int, default=10, help="Download concurrency (default: 10)")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between chapters in seconds (default: 1.0)")
    parser.add_argument("--img-delay", type=float, default=0.1, help="Delay between individual images in seconds (default: 0.1)")
    parser.add_argument("--output", default="downloads", help="Output directory (default: downloads)")
    parser.add_argument("--cookies", help="Cloudflare cookies (cf_clearance=...; other=...)")
    parser.add_argument("--debug", action="store_true", help="Print debug information")

    args = parser.parse_args()

    scraper = NewtokiScraper(
        concurrency=args.concurrency, 
        cookies=args.cookies,
        chapter_delay=args.delay,
        image_delay=args.img_delay
    )
    try:
        series = await scraper.get_series_info(args.url, debug=args.debug)
        if not series:
            return

        print(f"\nSeries: {series['title']}")
        print(f"Total Chapters: {len(series['chapters'])}\n")

        # Always list chapters first
        for i, ch in enumerate(series['chapters']):
            print(f"[{ch['index']}] - {ch['title']}")

        # Determine download range
        to_download = []
        if args.start is not None or args.end is not None:
            # Fallback to sequential range if flags are used
            start_idx = max(0, (args.start or 1) - 1)
            end_idx = args.end if args.end is not None else len(series['chapters'])
            end_idx = min(end_idx, len(series['chapters']))
            to_download = series['chapters'][start_idx:end_idx]
        else:
            print("\n[INFO] No range specified via arguments.")
            user_input = input("Enter your chapter (e.g. 1-5, 6) or press Enter to skip: ").strip()
            if not user_input:
                print("[INFO] No chapters selected. Exiting.")
                return
            
            # Map of displayed index string to chapter object
            # We use string to avoid issues with non-numeric indices if they exist
            index_map = {str(ch['index']): ch for ch in series['chapters']}
            
            try:
                selected_indices = set()
                parts = [p.strip() for p in user_input.split(',')]
                for part in parts:
                    if '-' in part:
                        s, e = map(int, part.split('-'))
                        for i in range(s, e + 1):
                            selected_indices.add(str(i))
                    else:
                        selected_indices.add(part)
                
                # Maintain order from the original list
                for ch in series['chapters']:
                    if str(ch['index']) in selected_indices:
                        to_download.append(ch)
            except ValueError:
                print("[ERROR] Invalid input format. Expected numbers or ranges like '1-5, 8'.")
                return

        if not to_download:
            print("[INFO] No chapters selected for download.")
            return

        print(f"\n[INFO] Proceeding to download {len(to_download)} chapters...")

        # Sanitize base folder
        safe_series_title = "".join([c for c in series['title'] if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
        base_folder = os.path.join(args.output, safe_series_title)
        os.makedirs(base_folder, exist_ok=True)

        for chapter in to_download:
            await scraper.download_chapter(chapter, base_folder, series_url=args.url, debug=args.debug)

    finally:
        await scraper.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python newtoki.py [Series_URL]")
        sys.exit(1)
    asyncio.run(main())
