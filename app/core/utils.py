import re
import logging

logger = logging.getLogger("CoreUtils")

def get_service_name(url: str) -> str:
    """Returns standardized platform name for folder paths."""
    url = url.lower()
    if "mechacomic.jp" in url: return "Mecha"
    if "jumptoon.com" in url: return "Jumptoon"
    if "piccoma.com" in url: return "Piccoma"
    if "kakao.com" in url: return "Kakao"
    if "qq.com" in url: return "Tencent"
    if "kuaikanmanhua.com" in url: return "Kuaikan"
    return "Unknown"

def extract_series_id(url: str) -> str | None:
    """Extracts Series ID from URL across all supported platforms."""
    # 1. Jumptoon (e.g. /series/JT00132)
    jt_match = re.search(r'series/(JT\d+)', url)
    if jt_match: return jt_match.group(1)
    
    # 2. Piccoma (e.g. /product/12345 or /product/sh/12345)
    pic_match = re.search(r'product/(?:sh/)?(\d+)', url)
    if pic_match: return pic_match.group(1)
    
    # 3. Mecha Comic (e.g. /books/123456)
    mecha_match = re.search(r'books/(\d+)', url)
    if mecha_match: return mecha_match.group(1)
    
    # 4. Kakao Page (e.g. /content/12345)
    kakao_match = re.search(r'content/(\d+)', url)
    if kakao_match: return kakao_match.group(1)
    
    # 5. Kuaikan (e.g. /topic/1234/)
    kuaikan_match = re.search(r'topic/(\d+)', url)
    if kuaikan_match: return kuaikan_match.group(1)
    
    # 6. Tencent (e.g. /id/1234)
    tencent_match = re.search(r'/id/(\d+)', url)
    if tencent_match: return tencent_match.group(1)

    return None
