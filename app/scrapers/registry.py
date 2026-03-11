import logging
from app.core.logger import logger
from app.scrapers.mecha.web import MechaWebScraper
from app.scrapers.mecha.api import MechaApiScraper
from app.scrapers.jumptoon.api import JumptoonApiScraper
from app.scrapers.kakaopage.api import KakaoApiScraper
from app.scrapers.acqq.api import AcqqApiScraper
from app.scrapers.piccoma.api import PiccomaApiScraper
from app.scrapers.kuaikan.api import KuaikanApiScraper

from app.services.browser.unlocker import BatchUnlocker

class ScraperRegistry:
    """
    The Central Router for all scraping services.
    Initializes all engines once and routes tasks based on URL or Flags.
    """

    def __init__(self, browser_service):
        """
        Args:
            browser_service: Shared Selenium instance for web-based scraping/unlocking.
        """
        self.browser = browser_service
        
        # --- INITIALIZE ALL ENGINES ONCE (Startup Efficiency) ---
        logger.info("[Registry] Igniting Scraping Engines...")
        
        # MechaComic Engines
        self.web_scraper = MechaWebScraper(browser_service)
        self.api_scraper = MechaApiScraper(browser_service)
        
        # Next-Gen API Engines
        self.jumptoon = JumptoonApiScraper()
        self.kakao = KakaoApiScraper()
        self.piccoma = PiccomaApiScraper()
        self.kuaikan = KuaikanApiScraper()
        self.acqq = AcqqApiScraper(browser_service)

        # Shared Batch Unlocker (Soft Affinity & 3-Tab Support)
        self.unlocker = BatchUnlocker(browser_service)
        
        logger.info("[Registry] All Engines Online.")

    def get_scraper(self, url: str, is_smartoon: bool = False):
        """
        Determines the best scraper for the job.
        
        Args:
            url (str): The target URL.
            is_smartoon (bool): Priority flag to use API over Selenium.
            
        Returns:
            BaseScraper: The specific scraper instance.
        """
        url_lower = url.lower()

        # 1. JUMPTOON ROUTING
        if "jumptoon.com" in url_lower:
            logger.info("[Registry] 🗺️ Routing to Jumptoon Scraper")
            return self.jumptoon

        # 2. KAKAO ROUTING (Handles both webtoon.kakao and page.kakao)
        if "kakao.com" in url_lower:
            logger.info("[Registry] 🗺️ Routing to Kakao Scraper")
            return self.kakao

        # 3. PICCOMA ROUTING
        if "piccoma.com" in url_lower:
            logger.info("[Registry] 🗺️ Routing to Piccoma Scraper")
            return self.piccoma

        # 4. TENCENT AC.QQ ROUTING
        if "qq.com" in url_lower:
            logger.info(" 🗺️ Routing to Tencent AC.QQ Scraper")
            return self.acqq

        # 5. KUAIKAN ROUTING
        if "kuaikanmanhua.com" in url_lower:
            logger.info("[Registry] 🗺️ Routing to Kuaikan Scraper")
            return self.kuaikan

        # 6. MECHACOMIC ROUTING
        if "mechacomic.jp" in url_lower:
            # We now prefer API (High-Speed Async) for all Mecha browsing by default.
            # Web engine (Playwright) is only used as an explicit fallback or for difficult handshakes.
            if is_smartoon:
                logger.info("[Registry] 🗺️ Routing to Mecha API Engine (Forced)")
                return self.api_scraper
            else:
                logger.info("[Registry] 🗺️ Routing to Mecha API Engine (Standard)")
                return self.api_scraper

        raise ValueError(f"No scraper registered for URL: {url}")
