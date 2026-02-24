import time
import os
import json
import logging
import re
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

from config.settings import Settings
from app.scrapers.base import BaseScraper
from app.services.browser.driver import BrowserService

logger = logging.getLogger("MechaWebScraper")

class MechaWebScraper(BaseScraper):
    def __init__(self, browser_service: BrowserService):
        self.browser = browser_service

    def get_series_info(self, url: str):
        """
        Metadata via Browser (Fallback). 
        Note: We primarily use the API Scraper for UI speed.
        """
        logger.info(f"[Web] Fetching metadata: {url}")
        
        # Ensure we are in Desktop mode for the list view
        self.browser.enable_mobile(False)
        self.browser.driver.get(f"{url.split('?')[0]}?page=1")
        time.sleep(2)

        # 1. Extract Title
        try:
            title_el = self.browser.driver.find_element(By.CSS_SELECTOR, "div.p-bookInfo_title h1")
            title = title_el.text.strip()
        except:
            title = self.browser.driver.title

        # 2. Extract Chapters
        chapters = []
        try:
            items = self.browser.driver.find_elements(By.CSS_SELECTOR, "li.p-chapterList_item")
            for item in items:
                if "p-chapterList_item-info" in item.get_attribute("class"): continue
                
                link_el = item.find_element(By.CSS_SELECTOR, "a.p-btn-chapter")
                c_url = link_el.get_attribute("href")
                cid = c_url.split('/')[-1]
                
                # Get clean Number (e.g. 001話)
                no_el = item.find_element(By.CSS_SELECTOR, "dt.p-chapterList_no")
                number_text = self.browser.driver.execute_script("return arguments[0].firstChild.textContent.trim()", no_el)
                
                name_el = item.find_element(By.CSS_SELECTOR, "dd.p-chapterList_name")
                title_text = name_el.text.strip()

                chapters.append({
                    'id': cid,
                    'number_text': number_text,
                    'title_text': title_text,
                    'title': f"{number_text} {title_text}",
                    'url': c_url
                })
        except Exception as e:
            logger.error(f"Web Metadata Error: {e}")

        # Extract Series ID
        series_id = url.split('/')[-1].split('?')[0]

        return title, len(chapters), chapters, None, series_id

    def scrape_chapter(self, task, output_dir):
        """
        Full Browser Scraper. 
        Only used if task.is_smartoon is False (Manual override).
        """
        logger.info(f"[Web] Scraping {task.title}...")
        
        self.browser.enable_mobile(True)
        self.browser.driver.get(task.url)
        time.sleep(3)

        # 1. Handle Purchase/Read
        self._handle_interaction()

        # 2. Wait for Viewer
        logger.info("   ⏳ Waiting for images to load...")
        try:
            WebDriverWait(self.browser.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div[class*='VerticalViewer'], div[class*='PageContainer']"))
            )
        except:
            self.browser.driver.save_screenshot(os.path.join(output_dir, "error.png"))
            raise Exception("Viewer failed to load. Check points or login.")

        # 3. Smart Scroll Loop
        scraped_hashes = set()
        math_data = []
        viewport_h = self.browser.driver.execute_script("return window.innerHeight")
        
        while True:
            images = self.browser.driver.find_elements(By.CSS_SELECTOR, "img[src*='blob']")
            for img in images:
                src = img.get_attribute('src')
                if not src or src in scraped_hashes: continue
                
                data = self.browser.fetch_blob(src)
                if data:
                    rect = self.browser.driver.execute_script("return arguments[0].getBoundingClientRect();", img)
                    scroll_y = self.browser.driver.execute_script("return window.scrollY;")
                    abs_y = rect['y'] + scroll_y
                    
                    fname = f"{int(abs_y):010d}.png"
                    with open(os.path.join(output_dir, fname), "wb") as f:
                        f.write(data)
                    
                    scraped_hashes.add(src)
                    math_data.append({'file': fname, 'y': abs_y})

            prev_y = self.browser.driver.execute_script("return window.scrollY")
            self.browser.driver.execute_script(f"window.scrollBy(0, {viewport_h * 0.75})")
            time.sleep(Settings.SCROLL_PAUSE)
            if self.browser.driver.execute_script("return window.scrollY") == prev_y:
                break

        with open(os.path.join(output_dir, "math.json"), "w") as f:
            json.dump(math_data, f)
            
        return output_dir

    def _handle_interaction(self):
        """Clicks 'Buy' or 'Read' buttons based on page state."""
        # 1. Check for Insufficient Points
        notes = self.browser.driver.find_elements(By.CSS_SELECTOR, "div.p-buyConfirm_note")
        for note in notes:
            if "ポイント" in note.text and "不足" in note.text:
                raise Exception("❌ Insufficient Points! Please top up.")

        # 2. Sequential Clicker
        for i in range(3):
            # Check if we are already in the reader
            html = self.browser.driver.page_source
            if "VerticalViewer" in html or "PageContainer" in html:
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
                btns = self.browser.driver.find_elements(By.CSS_SELECTOR, sel)
                if btns and btns[0].is_enabled() and btns[0].is_displayed():
                    label = btns[0].get_attribute("value") or btns[0].text
                    logger.info(f"   👆 Phase 3: Clicking '{label}'")
                    self.browser.driver.execute_script("arguments[0].click();", btns[0])
                    time.sleep(3)
                    clicked = True; break
            
            if not clicked: break