import re
import json
import logging
import asyncio
import os
import threading
import urllib.parse
from bs4 import BeautifulSoup
from io import BytesIO

try:
    from app.lib.pycasso import Canvas
except ImportError:
    Canvas = None

logger = logging.getLogger("PiccomaDRM")

class PiccomaDRM:
    def __init__(self, provider):
        self.provider = provider
        self.logger = logger
        # S-GRADE: Thread-safe lock to prevent pycasso's global state race condition
        self.unscramble_lock = threading.Lock()

    def _extract_pdata(self, html: str) -> dict | None:
        """S-Grade: Heuristic extraction of pData (image manifest) from Piccoma viewer page."""
        try:
            match = re.search(r'var\s+pData\s*=\s*({.*?});', html, re.DOTALL)
            if match:
                return json.loads(match.group(1))
            
            if 'pData' in html:
                start = html.find('pData')
                brace_start = html.find('{', start)
                brace_count = 0
                for i in range(brace_start, len(html)):
                    if html[i] == '{': brace_count += 1
                    elif html[i] == '}': brace_count -= 1
                    if brace_count == 0:
                        return json.loads(html[brace_start:i+1])
        except Exception: pass
        return None

    async def _dd_transform(self, seed: str) -> str:
        """
        Transforms the seed using the Diamond DRM WASM module via Node.js bridge.
        S-Grade: Async execution to prevent event-loop blocking.
        """
        try:
            # Paths to bridge and node
            # Correct pathing: 4 levels up from app/providers/platforms/piccoma
            bridge_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../piccoma_wasm_bridge.js"))
            
            # Run the bridge asynchronously
            proc = await asyncio.create_subprocess_exec(
                "node", bridge_path, seed,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            stdout, stderr = await proc.communicate()
            
            if proc.returncode == 0:
                transformed_seed = stdout.decode().strip()
                if transformed_seed:
                    self.logger.info(f"Seed transform success: {seed} -> {transformed_seed}")
                    return transformed_seed
            
            self.logger.warning(f"Seed transform failed: {stderr.decode().strip()}")
            return seed
            
        except Exception as e:
            self.logger.error(f"Error during seed transform: {e}")
            return seed

    def _extract_pdata_heuristic(self, html_text):
        """S+ Refinement: DRM Heuristic Recovery."""
        # Heuristic 1: NEXT_DATA
        soup = BeautifulSoup(html_text, 'html.parser')
        next_data = soup.select_one('script#__NEXT_DATA__')
        if next_data:
            try:
                data = json.loads(next_data.string)
                pdata = data.get('props', {}).get('pageProps', {}).get('initialState', {}).get('viewer', {}).get('pData')
                if pdata: return pdata
            except: pass

        # Heuristic 2: Legacy _pdata_ global
        match = re.search(r'var\s+_pdata_\s*=\s*(.*?)\s*(?:var\s+|</script>|;)', html_text, re.DOTALL)
        if match:
            content = match.group(1)
            try:
                return json.loads(content)
            except:
                paths = re.findall(r"['\"]?path['\"]?\s*:\s*['\"](.*?)['\"]", content)
                if paths:
                    logger.info(f"[Piccoma] Manifest recovered via regex fallback: {len(paths)} images.")
                    return {'img': [{'path': p} for p in paths]}
            
        # Heuristic 4: Modern PC Smartoon (episodeDetail)
        if next_data:
            try:
                n_data = json.loads(next_data.string)
                manifest = n_data.get('props', {}).get('pageProps', {}).get('episodeDetail', {}).get('manifest', {})
                images = manifest.get('images', [])
                if images:
                    pdata = {'img': [{'path': img.get('path')} for img in images if img.get('path')]}
                    logger.info(f"✨ [Piccoma Heuristic] Success via episodeDetail hierarchy! ({len(images)} images)")
                    return pdata
            except: pass

        # Heuristic 5: Recursive Deep Regex Scan
        img_matches = re.findall(r'["\']path["\']\s*:\s*["\'](https?://[^"\']+\.(?:jpg|png|webp|jpeg)[^"\']*)["\']', html_text)
        if img_matches:
            pdata_list = [{'path': m} for m in img_matches if '/seed' in m or '/img' in m]
            if pdata_list:
                logger.info(f"✨ [Piccoma Heuristic] Success via Deep Regex Pattern! ({len(pdata_list)} images)")
                return {'img': pdata_list}
            
        return None


    def _calculate_seed(self, url, region):
        """Mirror of Piccoma viewer get_checksum + get_seed JS logic.

        Steps (matching viewer source exactly):
          1. get_checksum: split base URL by '/', take SECOND-to-last segment
             JS: url.split('/').slice(-2)[0]
             e.g. //pcm.kakaocdn.net/.../WRDNKD40TMLSEIMSI@YMMD/i00001.jpg -> 'WRDNKD40TMLSEIMSI@YMMD'
          2. get_seed: digit-sum expires, right-rotate checksum by (sum % len)
        """
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)

        if region == "fr":
            checksum = qs.get('q', [''])[0]
        else:
            path_only = parsed.path.rstrip('/')
            segments = [s for s in path_only.split('/') if s]
            checksum = segments[-2] if len(segments) >= 2 else segments[-1] if segments else ""

        if not checksum:
            return ""

        expires = qs.get('expires', [''])[0]
        if expires:
            digit_sum = sum(int(d) for d in expires if d.isdigit())
            shift = digit_sum % len(checksum)
            if shift:
                checksum = checksum[-shift:] + checksum[:-shift]

        return checksum

    async def _download_robust(self, session, img_data, idx, out_dir, region):
        """S+ Verbatim 100% Mirror of pyccoma's Scraper.download logic."""
        url = img_data['path']
        if not url.startswith('http'): url = 'https:' + url
        
        seed = self._calculate_seed(url, region)
        
        res = await session.get(url, timeout=30)
        res.raise_for_status()
        out_path = f"{out_dir}/page_{idx:03d}.png"
        
        is_valid_seed = bool(seed)

        if is_valid_seed:
            if not Canvas:
                logger.warning(f"[Piccoma] 🛑 CANNOT UNSCRAMBLE: Canvas (pycasso) library not loaded. Page {idx} will remain scrambled.")
                with open(out_path, "wb") as f: f.write(res.content)
                return

            try:
                final_seed = await self._dd_transform(seed)
                
                def unscramble():
                    with self.unscramble_lock:
                        img_io = BytesIO(res.content)
                        canvas = Canvas(img_io, (50, 50), final_seed)
                        return canvas.export(mode="unscramble", format="png").getvalue()
                
                content = await asyncio.to_thread(unscramble)
                with open(out_path, "wb") as f: f.write(content)
            except Exception as e:
                logger.error(f"[Piccoma] Unscramble error (V3 Seed: {seed}): {e}")
                with open(out_path, "wb") as f: f.write(res.content)
        else:
            with open(out_path, "wb") as f: f.write(res.content)
