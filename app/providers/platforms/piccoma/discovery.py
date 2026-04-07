import re
import json
import logging
import asyncio
from bs4 import BeautifulSoup

logger = logging.getLogger("PiccomaProvider.Discovery")

class PiccomaDiscovery:
    """
    Extracted Discovery Logic for Piccoma (JP) new series lists.
    """
    def __init__(self, provider):
        self.provider = provider

    async def get_new_series_list(self) -> list[dict]:
        """Scrapes the 'New' series via the Theme API for Piccoma (JP)."""
        base_url = "https://piccoma.com"
        auth_session = await self.provider._get_authenticated_session(".piccoma.com")
        new_series = []
        
        try:
            # 1. Try Direct Theme Page Scrape (Tier 1)
            theme_url = f"{base_url}/web/theme/product/list/398316/N"
            res = await auth_session.get(theme_url, timeout=15)
            logger.info(f"[Piccoma] Theme Page Response: {res.status_code}")
            
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, 'html.parser')
                # Check for products in the initial HTML
                items = soup.select('li, .PCM-productList1_item, .PCM-product, .PCM-productTile')
                for item in items:
                    link = item.select_one('a')
                    if not link: continue
                    href = link.get('href', '')
                    if '/web/product/' not in href: continue
                    sid_match = re.search(r'/web/product/(\d+)', href)
                    if not sid_match: continue
                    sid = sid_match.group(1)
                    title_elem = item.select_one('.PCM-product_title, .PCM-productList1_title, .PCM-productTile_title, dt, span')
                    title = title_elem.get_text(strip=True) if title_elem else ""
                    
                    # 🟢 FIX: Define img_elem for poster extraction
                    img_elem = item.select_one('img')
                    poster = self.provider._format_poster_url(img_elem.get('src') if img_elem else None)
                    if not title or title == "Unknown": continue
                    
                    if any(s['series_id'] == sid for s in new_series): continue
                    new_series.append({
                        "series_id": sid, "title": title, "poster_url": poster, "url": f"{base_url}{href}"
                    })
                logger.info(f"[Piccoma] Tier 1 (Theme Page) found {len(new_series)} series.")
            
            # 2. Try API for Paginated Data (Tier 2)
            # Only if we found nothing or want more
            if not new_series:
                for p_id in [1, 0]:
                    api_url = f"{base_url}/web/next_page/list?result_id=398316&list_type=T&sort_type=N&page_id={p_id}"
                    headers = {'X-Requested-With': 'XMLHttpRequest', 'Referer': theme_url}
                    try:
                        res = await auth_session.get(api_url, headers=headers, timeout=15)
                        if res.status_code != 200: continue
                        ctype = res.headers.get('Content-Type', '').lower()
                        if 'application/json' in ctype or res.text.strip().startswith('{'):
                            data = res.json()
                            raw_data = data.get('data', data)
                            products = []
                            if isinstance(raw_data, list):
                                products = raw_data
                            elif isinstance(raw_data, dict):
                                products = raw_data.get('products', raw_data.get('list', []))
                            
                            if products and isinstance(products, list):
                                for item in products:
                                    if not isinstance(item, dict): continue
                                    sid = str(item.get('id', item.get('product_id', '')))
                                    if not sid: continue
                                    title = item.get('title', item.get('product_name', 'Unknown'))
                                    poster = self.provider._format_poster_url(item.get('img', item.get('image', item.get('cover_x1'))))
                                    if any(s['series_id'] == sid for s in new_series): continue
                                    new_series.append({
                                        "series_id": sid, "title": title, "poster_url": poster, "url": f"{base_url}/web/product/{sid}"
                                    })
                        
                        if new_series: 
                            logger.info(f"[Piccoma] Tier 2 (Paginated API) total: {len(new_series)} series.")
                            break
                    except Exception as e:
                        logger.debug(f"[Piccoma Discovery] API Trial failed: {e}")
                        continue

            # 3. Final Fallback: General New Page (Tier 3)
            if not new_series:
                res = await auth_session.get(f"{base_url}/web/list/new/all", timeout=15)
                if res.status_code == 200:
                    soup = BeautifulSoup(res.text, 'html.parser')
                    for item in soup.select('.PCM-productList1_item, .PCM-product'):
                        link = item.select_one('a')
                        if not link: continue
                        href = link.get('href', '')
                        sid_match = re.search(r'/web/product/(\d+)', href)
                        if not sid_match: continue
                        sid = sid_match.group(1)
                        title_elem = item.select_one('.PCM-productList1_title, .PCM-product_title')
                        title = title_elem.get_text(strip=True) if title_elem else "Unknown"
                        
                        # 🟢 FIX: Define img_elem for poster extraction in T3 fallback
                        img_elem = item.select_one('img')
                        poster = self.provider._format_poster_url(img_elem.get('src') if img_elem else None)
                        if any(s['series_id'] == sid for s in new_series): continue
                        new_series.append({
                            "series_id": sid, "title": title, "poster_url": poster, "url": f"{base_url}{href}"
                        })
            
            logger.info(f"[Piccoma] Discovery finished. Found {len(new_series)} series.")
            return new_series
        except Exception as e:
            logger.error(f"[Piccoma] Fatal error in new series discovery: {e}")
            return []
