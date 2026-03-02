import os
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
import urllib3

# Suppress the insecure request warnings since we are bypassing SSL verification
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class CSSScraper:
    def __init__(self, url: str, output_dir: str = "css_dump"):
        self.url = url
        self.output_dir = output_dir
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        self.soup = None

        # Create output directory if it doesn't exist
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)

    def fetch_page(self):
        print(f"Analyzing styles for: {self.url}")
        try:
            # Added verify=False
            response = requests.get(self.url, headers=self.headers, verify=False, timeout=15)
            response.raise_for_status()
            self.soup = BeautifulSoup(response.text, 'html.parser')
        except requests.RequestException as e:
            print(f"Error fetching page: {e}")

    def download_external_css(self):
        """Finds all <link rel='stylesheet'> and downloads the CSS files."""
        if not self.soup:
            return
            
        stylesheets = self.soup.find_all("link", rel="stylesheet")
        print(f"Found {len(stylesheets)} external stylesheets.")

        for index, link in enumerate(stylesheets):
            href = link.get("href")
            if not href:
                continue
                
            # Make sure the URL is absolute
            css_url = urljoin(self.url, href)
            
            try:
                print(f"Downloading CSS: {css_url}")
                css_response = requests.get(css_url, headers=self.headers, verify=False, timeout=15)
                css_response.raise_for_status()
                
                filename = f"style_{index + 1}.css"
                filepath = os.path.join(self.output_dir, filename)
                
                with open(filepath, 'w', encoding='utf-8') as f:
                    f.write(css_response.text)
                    
            except requests.RequestException as e:
                print(f"Failed to download {css_url}: {e}")

    def save_inline_styles(self):
        """Finds all <style> tags embedded in the HTML and saves them."""
        if not self.soup:
            return
            
        style_tags = self.soup.find_all("style")
        if not style_tags:
            print("No inline <style> tags found.")
            return
            
        print(f"Found {len(style_tags)} inline style blocks.")
        filepath = os.path.join(self.output_dir, "inline_styles.css")
        
        with open(filepath, 'w', encoding='utf-8') as f:
            for index, tag in enumerate(style_tags):
                f.write(f"/* --- Inline Style Block {index + 1} --- */\n")
                f.write(tag.string if tag.string else "")
                f.write("\n\n")
        print(f"Saved inline styles to {filepath}")

if __name__ == "__main__":
    print("=== CSS Scraper ===")
    target_url = input("Enter the URL to scrape: ").strip()
    
    if target_url:
        # Auto-add https:// if you forgot to type it
        if not target_url.startswith('http'):
            target_url = 'https://' + target_url
            
        css_scraper = CSSScraper(target_url, output_dir="scraped_styles")
        css_scraper.fetch_page()
        css_scraper.download_external_css()
        css_scraper.save_inline_styles()
        print("CSS Extraction complete.")
    else:
        print("No URL provided. Exiting.")