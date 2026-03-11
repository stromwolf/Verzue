import os
import json
import logging
import re
import asyncio
from playwright.async_api import Page
from config.settings import Settings
from app.scrapers.base import BaseScraper

logger = logging.getLogger("MechaWebScraper")

class MechaWebScraper(BaseScraper):
    def __init__(self, browser_service):
        self.browser = browser_service

    async def get_series_info(self, url: str):
        """
        Metadata via Browser (Fallback). 
        """
        logger.info(f"[Web] Fetching metadata: {url}")
        
        page = await self.browser.get_new_page()
        try:
            await page.goto(f"{url.split('?')[0]}?page=1", wait_until="domcontentloaded")
            await page.wait_for_timeout(2000)

            # 1. Extract Title
            title = await page.title()
            title_el = page.locator("div.p-bookInfo_title h1")
            if await title_el.is_visible():
                title = await title_el.inner_text()

            # 2. Extract Chapters
            chapters = []
            items = await page.locator("li.p-chapterList_item").all()
            for item in items:
                # Skip info items
                cls = await item.get_attribute("class")
                if "p-chapterList_item-info" in cls: continue
                
                link_el = item.locator("a.p-btn-chapter")
                c_url = await link_el.get_attribute("href")
                cid = c_url.split('/')[-1]
                
                # Get clean Number (e.g. 001話)
                no_el = item.locator("dt.p-chapterList_no")
                number_text = await page.evaluate("el => el.firstChild.textContent.trim()", await no_el.element_handle())
                
                name_el = item.locator("dd.p-chapterList_name")
                title_text = (await name_el.inner_text()).strip()

                chapters.append({
                    'id': cid,
                    'number_text': number_text,
                    'title_text': title_text,
                    'title': f"{number_text} {title_text}",
                    'url': f"https://mechacomic.jp{c_url}" if c_url.startswith('/') else c_url
                })
            
            # 3. Determine Total Chapter Count (10 per page)
            total_chapters = len(chapters)
            try:
                count_el = page.locator("div.p-search_chapterNo span").first
                if await count_el.is_visible():
                    text = await count_el.inner_text()
                    m = re.search(r'(\d+)', text)
                    if m: total_chapters = int(m.group(1))
            except: pass

            series_id = url.split('/')[-1].split('?')[0]
            return title.strip(), total_chapters, chapters, None, series_id

        finally:
            await page.close()

    async def fetch_more_chapters(self, base_url: str, target_page: int, seen_ids: set, skip_pages: list = None):
        """Fetches additional pages using the browser."""
        skip_pages = skip_pages or []
        logger.info(f"[Web] Fetching more chapters up to page {target_page}...")
        
        new_chapters = []
        page = await self.browser.get_new_page()
        try:
            for p_num in range(2, target_page + 1):
                if p_num in skip_pages: continue
                
                await page.goto(f"{base_url.split('?')[0]}?page={p_num}", wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                
                items = await page.locator("li.p-chapterList_item").all()
                p_chaps = []
                for item in items:
                    cls = await item.get_attribute("class")
                    if "p-chapterList_item-info" in cls: continue
                    
                    link_el = item.locator("a.p-btn-chapter")
                    c_url = await link_el.get_attribute("href")
                    cid = c_url.split('/')[-1]
                    if cid in seen_ids: continue
                    
                    no_el = item.locator("dt.p-chapterList_no")
                    number_text = await page.evaluate("el => el.firstChild.textContent.trim()", await no_el.element_handle())
                    name_el = item.locator("dd.p-chapterList_name")
                    title_text = (await name_el.inner_text()).strip()
                    
                    seen_ids.add(cid)
                    p_chaps.append({
                        'id': cid, 'number_text': number_text, 'title_text': title_text,
                        'title': f"{number_text} {title_text}",
                        'url': f"https://mechacomic.jp{c_url}" if c_url.startswith('/') else c_url
                    })
                
                if not p_chaps: break
                new_chapters.extend(p_chaps)
                
            return new_chapters
        finally:
            await page.close()

    async def scrape_chapter(self, task, output_dir):
        """
        Full Browser Scraper. 
        """
        logger.info(f"[Web] Scraping {task.title}...")
        
        page = await self.browser.get_new_page()
        try:
            # Set viewport for mobile-like view if needed
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.goto(task.url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            # 1. Handle Purchase/Read
            await self._handle_interaction(page)

            # 2. Wait for Viewer
            logger.info("   ⏳ Waiting for images to load...")
            try:
                await page.wait_for_selector("div[class*='VerticalViewer'], div[class*='PageContainer']", timeout=15000)
            except:
                await page.screenshot(path=os.path.join(output_dir, "error.png"))
                raise Exception("Viewer failed to load. Check points or login.")

            # 3. Smart Scroll Loop
            scraped_hashes = set()
            math_data = []
            viewport_h = 844
            
            while True:
                images = await page.locator("img[src*='blob']").all()
                for img in images:
                    src = await img.get_attribute('src')
                    if not src or src in scraped_hashes: continue
                    
                    data = await self.browser.fetch_blob(page, src)
                    if data:
                        # Get absolute Y position
                        box = await img.bounding_box()
                        scroll_y = await page.evaluate("window.scrollY")
                        abs_y = box['y'] + scroll_y
                        
                        fname = f"{int(abs_y):010d}.png"
                        with open(os.path.join(output_dir, fname), "wb") as f:
                            f.write(data)
                        
                        scraped_hashes.add(src)
                        math_data.append({'file': fname, 'y': abs_y})

                prev_y = await page.evaluate("window.scrollY")
                await page.evaluate(f"window.scrollBy(0, {viewport_h * 0.75})")
                await asyncio.sleep(0.5) 
                
                curr_y = await page.evaluate("window.scrollY")
                if curr_y == prev_y:
                    break

            with open(os.path.join(output_dir, "math.json"), "w") as f:
                json.dump(math_data, f)
                
            return output_dir
        finally:
            await page.close()

    async def _handle_interaction(self, page: Page):
        """Clicks 'Buy' or 'Read' buttons based on page state."""
        # 1. Check for Insufficient Points
        notes = await page.locator("div.p-buyConfirm_note").all()
        for note in notes:
            text = await note.inner_text()
            if "ポイント" in text and "不足" in text:
                raise Exception("❌ Insufficient Points! Please top up.")

        # 2. Sequential Clicker
        for i in range(3):
            # Check if we are already in the reader
            content = await page.content()
            if "VerticalViewer" in content or "PageContainer" in content:
                return

            selectors = [
                "input.c-btn-read-end",          # 'Read' (読む) inside confirm page
                "input.js-bt_buy_and_download",  # 'Buy' (50ptで購入)
                "button.btn-purchase",           # Standard Purchase
                "a.btn-read",                    # Free Chapter button
                "a#commit_purchase"              # Modal confirm
            ]
            
            clicked = False
            for sel in selectors:
                button = page.locator(sel).first
                if await button.is_visible(timeout=2000):
                    label = await button.get_attribute("value") or await button.inner_text()
                    logger.info(f"   👆 Phase 3: Clicking '{label.strip()}'")
                    await button.click()
                    await page.wait_for_timeout(3000)
                    clicked = True; break
            
            if not clicked: break