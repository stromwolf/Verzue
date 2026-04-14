from app.core.logger import logger
from app.scrapers.mecha.web import MechaWebScraper
from app.scrapers.mecha.api import MechaApiScraper
from app.scrapers.jumptoon.api import JumptoonApiScraper
from app.scrapers.kakaopage.api import KakaoApiScraper

class ScraperRegistry:
    def __init__(self, browser_service):
        self.browser = browser_service
        self.web_scraper = MechaWebScraper(browser_service)
        self.api_scraper = MechaApiScraper()
        self.jumptoon = JumptoonApiScraper()
        self.kakao = KakaoApiScraper()

    def get_scraper(self, url: str, is_smartoon: bool = False):
        url_lower = url.lower()
        
        if "jumptoon.com" in url_lower:
            return self.jumptoon
            
        if "kakao.com" in url_lower:
            return self.kakao
            
        if is_smartoon:
            return self.api_scraper
        
        return self.web_scraper